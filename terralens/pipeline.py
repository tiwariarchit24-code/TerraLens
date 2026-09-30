"""Per-scene processing (worker side). Idempotent: re-running a scene rewrites the same
products and upserts the same rows. Expensive work happens here, once, so queries
only read stored products."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

from . import audit, provenance
from .aoi import get_aoi
from .config import ROOT, path, settings, thresholds
from .db import J, conn, q, q1
from .hashing import sha256_file
from .ingest.adapters import by_name
from .jobs.queue import enqueue
from .raster import coreg, normalize
from .raster.cog import write_cog
from .raster.grid import tiles_for_aoi
from .raster.indices import BANDS, IDX, all_indices
from .raster.quality import EDGE, quality_mask
from .raster.standardise import from_uint16, standardise, to_uint16
from .storage import project_relative
from .version import PIPELINE_VERSION

log = logging.getLogger("terralens.pipeline")
DISPLAY = {"bands": ["B04", "B03", "B02"], "min": 0.0, "max": 0.3}


def obs_key(scene: dict) -> str:
    return f"{scene['acquired_at']:%Y%m%dT%H%M%S}_{scene['platform']}_{scene['id']}"


def product_paths(aoi_id: str, key: str) -> dict[str, Path]:
    return {"cog": path("cog") / aoi_id / f"{key}_refl.tif", "mask": path("masks") / aoi_id / f"{key}_mask.tif",
            "classprob": path("features") / aoi_id / f"{key}_classprob.tif",
            "chip": path("chips") / aoi_id / f"{key}_rgb.png"}


def rgb_image(refl: np.ndarray, lo: float = DISPLAY["min"], hi: float = DISPLAY["max"]) -> np.ndarray:
    """Fixed linear stretch (never per-image auto-stretch)."""
    rgb = np.stack([refl[IDX[b]] for b in DISPLAY["bands"]], -1)
    out = np.clip((rgb - lo) / (hi - lo), 0, 1)
    out = np.nan_to_num(out, nan=0.0)
    return (out * 255).astype(np.uint8)


def load_obs_refl(obs: dict) -> np.ndarray:
    with rasterio.open(ROOT / obs["cog_path"]) as ds:
        return from_uint16(ds.read())


def load_obs_mask(obs: dict) -> np.ndarray:
    with rasterio.open(ROOT / obs["mask_path"]) as ds:
        return ds.read(1)


def _dem(aoi_id: str) -> np.ndarray | None:
    f = path("gis") / aoi_id / "dem.tif"
    if not f.exists():
        return None
    with rasterio.open(f) as ds:
        a = ds.read(1).astype(np.float32)
        a[a == ds.nodata] = np.nan
    return None if np.isnan(a).all() else np.nan_to_num(a, nan=float(np.nanmedian(a)))


def process_scene(scene_id: int, aoi_id: str) -> dict:
    t0 = dt.datetime.now(dt.timezone.utc)
    scene = q1("SELECT * FROM scenes WHERE id=%s", (scene_id,))
    if scene is None:
        raise ValueError(f"scene {scene_id} not found")
    if scene["status"] == "superseded":
        return {"skipped": "superseded"}
    thr = thresholds()
    aoi = get_aoi(aoi_id)
    grid = aoi["grid"]
    with conn() as c:
        c.execute("UPDATE scenes SET status='processing' WHERE id=%s", (scene_id,))

    ov = scene["radiometric"].get("override")
    meta = by_name(scene["adapter"]).read(ROOT / scene["raw_path"], {"radiometric": ov} if ov else None)
    std = standardise(meta.bands, scene["radiometric"], grid, int(thr["quality"]["saturation_dn"]),
                      settings()["pipeline"]["resample_20m"])
    mask, qstats = quality_mask(std.refl, std.scl, std.nodata, std.saturated, scene["sun_azimuth"],
                                scene["sun_elevation"], grid["gsd"], thr["quality"], _dem(aoi_id))
    refl = std.refl

    # ---------------- co-registration against the AOI reference observation (Gate 2 data)
    rt = thr["registration"]
    ref_obs = q1("SELECT * FROM observations WHERE id=%s", (aoi["reference_obs_id"],)) if aoi["reference_obs_id"] else None
    reg = {"status": None, "shift": None, "residual": None, "peak_ratio": None, "reason": None, "ref_obs_id": None}
    norm_params = None
    usable_px = mask == 0
    if ref_obs is None:
        if qstats["usable_fraction"] >= 0.6:
            reg.update(status="reference", shift=[0.0, 0.0], residual=0.0, reason="first sufficiently clear observation becomes the AOI reference")
        else:
            reg.update(status="suspect", reason="no reference observation yet and this observation is too cloudy to become one")
    else:
        ref_refl = load_obs_refl(ref_obs)
        ref_ok = load_obs_mask(ref_obs) == 0
        est = coreg.estimate_shift(ref_refl[IDX["B08"]], refl[IDX["B08"]], ref_ok, usable_px)
        status, reason = coreg.decide(est, rt)
        reg.update(status=status, shift=est["shift"], residual=est["residual"], peak_ratio=est["peak_ratio"],
                   reason=reason, ref_obs_id=ref_obs["id"], n_windows=est["n_windows"])
        if status == "corrected":
            refl = coreg.apply_shift(refl, est["shift"], order=1, cval=np.nan)
            mask = coreg.apply_shift(mask, est["shift"], order=0, cval=1).astype(np.uint8)
        if status in ("ok", "corrected") and est["residual"] is not None and est["residual"] > rt["residual_edge_suppress_px"]:
            edges = coreg.strong_edges(ref_refl[IDX["B08"]], int(rt["edge_band_px"]))
            mask[edges & (mask == 0)] |= EDGE
        usable_px = mask == 0
        if status in ("ok", "corrected"):
            norm_params = normalize.fit(ref_refl, refl, usable_px & ref_ok)

    usable_fraction = float(usable_px.mean())
    usable, why = True, None
    if usable_fraction < thr["quality"]["obs_min_usable_fraction"]:
        usable, why = False, f"only {usable_fraction:.1%} of the AOI is usable (cloud/shadow/haze/no-data)"
    elif reg["status"] in ("unusable", "suspect"):
        usable, why = False, f"alignment {reg['status']}: {reg['reason']}"

    # ---------------- products
    key = obs_key(scene)
    pp = product_paths(aoi_id, key)
    tags = {"scene": scene["source_id"], "pipeline": PIPELINE_VERSION, "offsets": scene["radiometric"].get("offset_to_apply"),
            "scale": "reflectance = value / 10000", "acquired_at": scene["acquired_at"].isoformat()}
    write_cog(pp["cog"], to_uint16(refl), grid["transform"], grid["crs"], nodata=0, descriptions=BANDS, tags=tags)
    write_cog(pp["mask"], mask, grid["transform"], grid["crs"], nodata=None, descriptions=["quality_bits"],
              tags={"bits": "1 nodata,2 saturated,4 cloud,8 haze,16 shadow,32 snow,64 terrain,128 registration-edge"},
              resampling="nearest")
    pp["chip"].parent.mkdir(parents=True, exist_ok=True)
    img = rgb_image(refl)
    Image.fromarray(img).save(pp["chip"])
    cog_sha, mask_sha = sha256_file(pp["cog"]), sha256_file(pp["mask"])

    quality = {**qstats, **{"standardise": std.stats}, "registration": reg}
    with conn() as c:
        row = c.execute(
            """INSERT INTO observations (scene_id, aoi_id, acquired_at, sensor, platform, processing_level, gsd_m, cog_path,
                 mask_path, chip_path, cog_sha256, mask_sha256, usable_fraction, cloud_fraction, shadow_fraction, haze_fraction,
                 haze_score, snow_fraction, nodata_fraction, terrain_fraction, saturation_fraction, reg_ref_obs_id, reg_shift_x,
                 reg_shift_y, reg_residual, reg_peak_ratio, reg_status, norm_params, usable, unusable_reason, quality,
                 preprocessing_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (scene_id, aoi_id) DO UPDATE SET cog_path=EXCLUDED.cog_path, mask_path=EXCLUDED.mask_path,
                 chip_path=EXCLUDED.chip_path, cog_sha256=EXCLUDED.cog_sha256, mask_sha256=EXCLUDED.mask_sha256,
                 usable_fraction=EXCLUDED.usable_fraction, cloud_fraction=EXCLUDED.cloud_fraction,
                 shadow_fraction=EXCLUDED.shadow_fraction, haze_fraction=EXCLUDED.haze_fraction, haze_score=EXCLUDED.haze_score,
                 snow_fraction=EXCLUDED.snow_fraction, nodata_fraction=EXCLUDED.nodata_fraction,
                 terrain_fraction=EXCLUDED.terrain_fraction, saturation_fraction=EXCLUDED.saturation_fraction,
                 reg_ref_obs_id=EXCLUDED.reg_ref_obs_id, reg_shift_x=EXCLUDED.reg_shift_x, reg_shift_y=EXCLUDED.reg_shift_y,
                 reg_residual=EXCLUDED.reg_residual, reg_peak_ratio=EXCLUDED.reg_peak_ratio, reg_status=EXCLUDED.reg_status,
                 norm_params=EXCLUDED.norm_params, usable=EXCLUDED.usable, unusable_reason=EXCLUDED.unusable_reason,
                 quality=EXCLUDED.quality, preprocessing_version=EXCLUDED.preprocessing_version
               RETURNING id""",
            (scene_id, aoi_id, scene["acquired_at"], scene["sensor"], scene["platform"], scene["processing_level"],
             scene["gsd_m"], project_relative(pp["cog"]), project_relative(pp["mask"]), project_relative(pp["chip"]),
             cog_sha, mask_sha, usable_fraction, qstats["cloud_fraction"], qstats["shadow_fraction"], qstats["haze_fraction"],
             qstats["haze_score"], qstats["snow_fraction"], qstats["nodata_fraction"], qstats["terrain_fraction"],
             qstats["saturation_fraction"], reg["ref_obs_id"], reg["shift"][1] if reg["shift"] else None,
             reg["shift"][0] if reg["shift"] else None, reg["residual"], reg["peak_ratio"], reg["status"], J(norm_params),
             usable, why, J(quality), PIPELINE_VERSION)).fetchone()
        obs_id = row["id"]
        if reg["status"] == "reference":
            c.execute("UPDATE aois SET reference_obs_id=%s WHERE id=%s AND reference_obs_id IS NULL", (obs_id, aoi_id))
        # tile statistics (for search filters, embedding selection and the timeline)
        idx = all_indices(refl)
        c.execute("DELETE FROM tile_observations WHERE observation_id=%s", (obs_id,))
        rows = []
        for t in tiles_for_aoi(grid):
            sl = (slice(t.window.row_off, t.window.row_off + t.window.height), slice(t.window.col_off, t.window.col_off + t.window.width))
            u = usable_px[sl]
            uf = float(u.mean())
            means = {k: (float(v[sl][u].mean()) if u.any() else None) for k, v in idx.items()}
            rows.append((t.tile_id, obs_id, scene["acquired_at"], uf, float(((mask[sl] & 4) > 0).mean()),
                         float(((mask[sl] & 8) > 0).mean()), means["ndvi"], means["mndwi"], means["ndbi"], means["bsi"]))
        with c.cursor() as cur:
            cur.executemany("""INSERT INTO tile_observations (tile_id, observation_id, acquired_at, usable_fraction, cloud_fraction,
                               haze_fraction, ndvi, mndwi, ndbi, bsi) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        c.execute("UPDATE scenes SET status='processed', processed_at=now(), status_detail=NULL WHERE id=%s", (scene_id,))
        inputs = [{"type": "scene", "id": scene_id, "sha256": scene["raw_sha256"]}]
        params = {"radiometric": scene["radiometric"], "registration": reg, "grid": {"crs": grid["crs"], "bounds": grid["bounds"], "gsd": grid["gsd"]}}
        provenance.record("observation", obs_id, "standardise+quality+coregister", inputs, pp["cog"], params=params,
                          threshold_set=thr["id"], started_at=t0, c=c)
        provenance.record("quality_mask", obs_id, "quality_mask", inputs, pp["mask"], params={"bits": "see mask tags"},
                          threshold_set=thr["id"], started_at=t0, c=c)
        audit.append("pipeline.observation_processed", "observation", obs_id,
                     {"scene_id": scene_id, "aoi": aoi_id, "usable": usable, "usable_fraction": round(usable_fraction, 4),
                      "reg_status": reg["status"], "cog_sha256": cog_sha}, c=c)
        enqueue("classify_observation", {"observation_id": obs_id}, dedupe_key=f"classify:{obs_id}", c=c)
    log.info("processed scene %s aoi %s -> obs %s usable=%s (%.1f%%) reg=%s", scene_id, aoi_id, obs_id, usable,
             100 * usable_fraction, reg["status"])
    return {"observation_id": obs_id, "usable": usable, "usable_fraction": usable_fraction, "registration": reg["status"]}
