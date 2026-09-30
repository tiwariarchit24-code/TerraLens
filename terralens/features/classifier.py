"""Per-date land-cover probability classifier (LightGBM).

Classes: water, trees, other_veg, bare, built.
Features: six surface-reflectance bands, NDVI, MNDWI, NDBI, BSI, visible brightness and
a 3x3 NIR standard deviation (simple texture).

Training labels come from ESA WorldCover 2021 refined PER DATE with index rules, because
a static map label is wrong for part of the year (cropland is bare soil in the dry
season, reservoir beds are exposed in April). Only pixels from DEV blocks are used for
training; TEST blocks give the held-out accuracy. The probabilities are model outputs,
not ground truth, and the classifier's version and hash travel with every product.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage

from .. import audit, provenance
from ..aoi import get_aoi
from ..config import ROOT, path, settings
from ..db import J, conn, q, q1
from ..hashing import sha256_file, sha256_json
from ..jobs.queue import enqueue
from ..raster.cog import write_cog
from ..raster.grid import tiles_for_aoi
from ..raster.indices import IDX, all_indices
from ..raster.standardise import from_uint16
from ..storage import project_relative

log = logging.getLogger("terralens.classifier")
CLASSES = ["water", "trees", "other_veg", "bare", "built"]
FEATURES = ["B02", "B03", "B04", "B08", "B11", "B12", "ndvi", "mndwi", "ndbi", "bsi", "brightness", "nir_std3"]
MODEL_DIR = "classifier"
PROB_SCALE = 250          # stored probability = p * 250 (uint8); 255 = no data


def features(refl: np.ndarray) -> np.ndarray:
    idx = all_indices(refl)
    nir = np.nan_to_num(refl[IDX["B08"]], nan=0.0)
    m = ndimage.uniform_filter(nir, 3)
    m2 = ndimage.uniform_filter(nir * nir, 3)
    std3 = np.sqrt(np.clip(m2 - m * m, 0, None))
    bright = (refl[IDX["B02"]] + refl[IDX["B03"]] + refl[IDX["B04"]]) / 3.0
    f = np.stack([refl[i] for i in range(6)] + [idx["ndvi"], idx["mndwi"], idx["ndbi"], idx["bsi"], bright, std3])
    return f.reshape(len(FEATURES), -1).T.astype(np.float32)


def refine_labels(wc: np.ndarray, refl: np.ndarray) -> np.ndarray:
    """WorldCover class -> per-date label (-1 = not used for training)."""
    idx = all_indices(refl)
    ndvi, mndwi = idx["ndvi"], idx["mndwi"]
    lab = np.full(wc.shape, -1, np.int8)
    water = (wc == 80)
    lab[water & (mndwi > 0.0) & (ndvi < 0.2)] = 0
    lab[water & (mndwi < -0.2) & (ndvi < 0.2)] = 3            # exposed reservoir / river bed
    trees = np.isin(wc, (10, 95))
    lab[trees & (ndvi >= 0.35)] = 1
    veg = np.isin(wc, (20, 30, 40, 90))
    lab[veg & (ndvi >= 0.4)] = 2
    lab[veg & (ndvi <= 0.2) & (mndwi < 0)] = 3                 # fallow / harvested fields are bare on that date
    lab[(wc == 50) & (ndvi < 0.3) & (mndwi < 0)] = 4
    lab[(wc == 60) & (ndvi < 0.25) & (mndwi < 0)] = 3
    return lab


def _split_mask(aoi_id: str, grid: dict, split: str) -> np.ndarray:
    """Pixel mask of tile cores belonging to `split` (cores do not overlap)."""
    m = np.zeros((grid["height"], grid["width"]), bool)
    rows = {r["tile_id"]: r["split"] for r in q("SELECT tile_id, split FROM grid_tiles WHERE aoi_id=%s", (aoi_id,))}
    core = int(settings()["grid"]["tile_stride_px"])
    off = (int(settings()["grid"]["tile_size_px"]) - core) // 2
    for t in tiles_for_aoi(grid):
        if rows.get(t.tile_id) == split:
            r0, c0 = t.window.row_off + off, t.window.col_off + off
            m[r0:r0 + core, c0:c0 + core] = True
    return m


def build_samples(split: str, per_class_per_obs: int = 400, max_obs_per_aoi: int = 14, seed: int = 42):
    rng = np.random.default_rng(seed)
    X, y, meta = [], [], []
    for a in q("SELECT id FROM aois ORDER BY id"):
        aoi_id = a["id"]
        wcf = path("gis") / aoi_id / "worldcover.tif"
        if not wcf.exists():
            log.warning("no WorldCover for %s; skipped", aoi_id)
            continue
        with rasterio.open(wcf) as ds:
            wc = ds.read(1)
        grid = get_aoi(aoi_id)["grid"]
        sm = _split_mask(aoi_id, grid, split)
        obs = q("""SELECT id, cog_path, mask_path, acquired_at FROM observations WHERE aoi_id=%s AND usable
                   AND usable_fraction >= 0.7 ORDER BY acquired_at""", (aoi_id,))
        if len(obs) > max_obs_per_aoi:   # spread across the year: seasons matter for the per-date labels
            obs = [obs[i] for i in np.linspace(0, len(obs) - 1, max_obs_per_aoi).round().astype(int)]
        for o in obs:
            with rasterio.open(ROOT / o["cog_path"]) as ds:
                refl = from_uint16(ds.read())
            with rasterio.open(ROOT / o["mask_path"]) as ds:
                ok = ds.read(1) == 0
            lab = refine_labels(wc, refl)
            lab[~(ok & sm)] = -1
            F = None
            for k in range(len(CLASSES)):
                ids = np.flatnonzero(lab.ravel() == k)
                if ids.size == 0:
                    continue
                ids = rng.choice(ids, min(per_class_per_obs, ids.size), replace=False)
                if F is None:
                    F = features(refl)
                X.append(F[ids])
                y.append(np.full(ids.size, k, np.int8))
                meta.append((aoi_id, o["id"], k, int(ids.size)))
    if not X:
        raise RuntimeError("no training samples: are observations processed and WorldCover staged?")
    return np.concatenate(X), np.concatenate(y), meta


def train(version: str = "1.0.0") -> dict:
    import lightgbm as lgb
    from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

    t0 = dt.datetime.now(dt.timezone.utc)
    Xtr, ytr, mtr = build_samples("dev")
    Xte, yte, mte = build_samples("test", seed=7)
    counts = np.bincount(ytr, minlength=len(CLASSES))
    w = (len(ytr) / (len(CLASSES) * np.maximum(counts, 1)))[ytr]
    params = {"objective": "multiclass", "num_class": len(CLASSES), "learning_rate": 0.08, "num_leaves": 31,
              "min_data_in_leaf": 40, "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1,
              "lambda_l2": 1.0, "seed": 42, "deterministic": True, "num_threads": 8, "verbose": -1}
    booster = lgb.train(params, lgb.Dataset(Xtr, ytr, weight=w, feature_name=FEATURES), num_boost_round=300)
    pred = booster.predict(Xte).argmax(1)
    cm = confusion_matrix(yte, pred, labels=list(range(len(CLASSES))))
    metrics = {"test_accuracy": float(accuracy_score(yte, pred)),
               "test_macro_f1": float(f1_score(yte, pred, average="macro")),
               "per_class_f1": dict(zip(CLASSES, f1_score(yte, pred, average=None, labels=list(range(len(CLASSES)))).round(4).tolist())),
               "confusion_matrix": {"labels": CLASSES, "rows_true_cols_pred": cm.tolist()},
               "n_train": int(len(ytr)), "n_test": int(len(yte)),
               "train_class_counts": dict(zip(CLASSES, counts.tolist())),
               "note": "Test labels are WorldCover-2021-derived, refined per date; they measure agreement with a weak reference, not ground truth."}
    d = path("models") / MODEL_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"lgbm_landcover_v{version}.txt"
    booster.save_model(str(f))
    sha = sha256_file(f)
    dataset_id = "ds-" + sha256_json({"train": mtr, "labels": "worldcover2021+per-date-rules-v1"})[:12]
    model_id = f"clf-lgbm-landcover-v{version}@{sha[:12]}"
    meta = {"id": model_id, "classes": CLASSES, "features": FEATURES, "prob_scale": PROB_SCALE,
            "training_dataset_id": dataset_id, "label_source": "ESA WorldCover 2021 v200 (CC BY 4.0) refined per date by index rules",
            "split": "train on dev blocks, evaluate on test blocks", "params": params, "metrics": metrics,
            "trained_at": t0.isoformat()}
    (d / f"lgbm_landcover_v{version}.json").write_text(json.dumps(meta, indent=1))
    with conn() as c:
        c.execute("UPDATE models SET status='inactive' WHERE kind='classifier' AND status='active'")
        c.execute("""INSERT INTO models (id, kind, name, version, source, license, file_path, sha256, input_spec, params, status, metrics)
                     VALUES (%s,'classifier','LightGBM land-cover (5 classes)',%s,'trained locally by TerraLens','project',%s,%s,%s,%s,'active',%s)
                     ON CONFLICT (id) DO UPDATE SET status='active', metrics=EXCLUDED.metrics""",
                  (model_id, version, project_relative(f), sha, J({"features": FEATURES, "classes": CLASSES}),
                   J({"lightgbm": params, "training_dataset_id": dataset_id}), J(metrics)))
        provenance.record("model", model_id, "train_classifier", [{"type": "training_samples", "id": dataset_id, "sha256": None}],
                          f, params={"metrics": metrics}, started_at=t0, c=c)
        audit.append("model.trained", "model", model_id, {"metrics": {k: metrics[k] for k in ("test_accuracy", "test_macro_f1")}}, c=c)
    log.info("classifier %s trained: acc=%.3f macroF1=%.3f", model_id, metrics["test_accuracy"], metrics["test_macro_f1"])
    return {"model_id": model_id, **metrics}


_cache: dict = {}


def active_model():
    import lightgbm as lgb
    m = q1("SELECT * FROM models WHERE kind='classifier' AND status='active' ORDER BY registered_at DESC LIMIT 1")
    if not m:
        return None, None
    if _cache.get("id") != m["id"]:
        f = ROOT / m["file_path"]
        if sha256_file(f) != m["sha256"]:
            raise RuntimeError(f"classifier checksum mismatch for {m['id']}")
        _cache.update(id=m["id"], booster=lgb.Booster(model_file=str(f)))
    return m, _cache["booster"]


def predict_proba(booster, refl: np.ndarray) -> np.ndarray:
    H, W = refl.shape[1:]
    F = features(refl)
    valid = ~np.isnan(F).any(1)
    P = np.zeros((F.shape[0], len(CLASSES)), np.float32)
    if valid.any():
        P[valid] = booster.predict(F[valid], num_threads=8)
    P = P.T.reshape(len(CLASSES), H, W)
    P[:, ~valid.reshape(H, W)] = np.nan
    return P


def classify_observation(obs_id: int) -> dict:
    m, booster = active_model()
    if m is None:
        return {"skipped": "no active classifier (run `terralens train-classifier`)"}
    o = q1("SELECT * FROM observations WHERE id=%s", (obs_id,))
    if o["classifier_model_id"] == m["id"] and o["classprob_path"] and (ROOT / o["classprob_path"]).exists():
        return {"skipped": "already classified with the active model"}
    with rasterio.open(ROOT / o["cog_path"]) as ds:
        refl = from_uint16(ds.read())
        tr, crs = ds.transform, ds.crs.to_string()
    P = predict_proba(booster, refl)
    enc = np.where(np.isnan(P), 255, np.clip(np.round(P * PROB_SCALE), 0, PROB_SCALE)).astype(np.uint8)
    out = Path(str(ROOT / o["cog_path"]).replace("/cog/", "/features/").replace("_refl.tif", "_classprob.tif"))
    write_cog(out, enc, tr, crs, nodata=255, descriptions=CLASSES, tags={"model": m["id"], "scale": f"p = value / {PROB_SCALE}"},
              resampling="nearest")
    sha = sha256_file(out)
    # tile class fractions on usable pixels
    with rasterio.open(ROOT / o["mask_path"]) as ds:
        ok = ds.read(1) == 0
    hard = np.where(np.isnan(P).any(0), -1, np.nan_to_num(P).argmax(0))
    grid = get_aoi(o["aoi_id"])["grid"]
    with conn() as c:
        with c.cursor() as cur:
            rows = []
            for t in tiles_for_aoi(grid):
                sl = (slice(t.window.row_off, t.window.row_off + 128), slice(t.window.col_off, t.window.col_off + 128))
                hh = hard[sl][ok[sl]]
                if hh.size:
                    fr = np.bincount(hh[hh >= 0], minlength=len(CLASSES)) / max(hh.size, 1)
                    rows.append((J(dict(zip(CLASSES, fr.round(4).tolist()))), t.tile_id, obs_id))
            cur.executemany("UPDATE tile_observations SET class_fractions=%s WHERE tile_id=%s AND observation_id=%s", rows)
        c.execute("UPDATE observations SET classprob_path=%s, classprob_sha256=%s, classifier_model_id=%s WHERE id=%s",
                  (project_relative(out), sha, m["id"], obs_id))
        provenance.record("class_probabilities", obs_id, "classify",
                          [{"type": "observation", "id": obs_id, "sha256": o["cog_sha256"]}], out, model_ids=[m["id"]], c=c)
        enqueue("embed_observation", {"observation_id": obs_id}, dedupe_key=f"embed:{obs_id}", c=c)
        enqueue("change_update", {"aoi_id": o["aoi_id"], "trigger": f"observation {obs_id}"}, dedupe_key=f"change:{o['aoi_id']}", c=c)
    return {"observation_id": obs_id, "model": m["id"]}
