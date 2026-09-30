"""Derive DEMO change events from the real staged Sentinel-2 frames for the frontend.

This is NOT the TerraLens change engine. Each demo event uses a simple, documented
index-threshold rule inside a search box on real pixels:
  veg_loss     pre NDVI >= t_pre on the pre-window frames, post NDVI < t_post and MNDWI < 0
  water_loss   pre MNDWI > 0.1, post MNDWI <= 0.1
  brighten     visible brightness rises by >= 0.04 and NDVI < 0.25 (bright temporary structures)
  quality      the SCL cloud / cloud-shadow pixels of one date (a candidate that Gate 1 removes)
Outputs (all labelled DEMO in the UI):
  polygon (vectorised 20 m mask), area and a threshold-sensitivity range, a per-observation
  series (usable share from the real SCL; post-state share; area), a change-mask PNG and a
  first-change-date PNG. Frames without >= 80 % usable pixels in the polygon get no value:
  gaps stay gaps.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import transform as stf
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
import yaml  # noqa: E402

import build_demo_imagery as B  # noqa: E402
from terralens.raster.grid import aoi_grid  # noqa: E402

GEN = ROOT / "frontend/src/data/demo/generated"
IMG = ROOT / "frontend/public/demo/events"
PX_HA = 0.04  # 20 m display pixels

SPECS = [
    {"id": "EVT-KOR-0142", "aoi": "korba", "box": [292, 60, 392, 108], "rule": "veg_loss", "t_pre": 0.30, "t_post": 0.15,
     "pre": ["2024-10-01", "2024-12-31"], "post": ["2025-11-01", "2026-02-28"]},
    {"id": "EVT-KHR-0031", "aoi": "raipur_kharun", "box": [86, 0, 116, 26], "rule": "veg_loss", "t_pre": 0.22, "t_post": 0.17,
     "pre": ["2024-10-01", "2024-12-31"], "post": ["2025-11-01", "2026-02-28"]},
    {"id": "EVT-KHR-0036", "aoi": "raipur_kharun", "box": [132, 66, 156, 90], "rule": "veg_loss", "t_pre": 0.22, "t_post": 0.17,
     "pre": ["2024-10-01", "2024-12-31"], "post": ["2025-11-01", "2026-02-28"]},
    {"id": "EVT-NYR-0012", "aoi": "naya_raipur", "box": [66, 218, 102, 248], "rule": "veg_loss", "t_pre": 0.22, "t_post": 0.17,
     "pre": ["2024-10-01", "2024-12-31"], "post": ["2025-11-01", "2026-02-28"]},
    {"id": "EVT-PRY-0007", "aoi": "prayagraj", "box": [140, 15, 255, 250], "rule": "brighten",
     "pre": ["2024-10-01", "2024-11-10"], "post": ["2025-01-10", "2025-02-20"]},
    {"id": "EVT-GNG-0019", "aoi": "gangrel", "box": [0, 300, 170, 440], "rule": "water_loss",
     "pre": ["2024-11-15", "2024-12-31"], "post": ["2025-05-01", "2025-06-15"]},
    {"id": "EVT-GNG-0024", "aoi": "gangrel", "box": [480, 110, 600, 200], "rule": "quality", "date": "2026-06-13"},
    {"id": "EVT-KHR-0044", "aoi": "raipur_kharun", "box": [0, 260, 120, 380], "rule": "veg_loss", "t_pre": 0.40, "t_post": 0.20,
     "pre": ["2025-09-01", "2025-10-31"], "post": ["2026-02-15", "2026-04-15"]},
]


def load(o, aoi_bbox):
    g = aoi_grid(aoi_bbox)
    H, W = o["pixel"]["height"], o["pixel"]["width"]
    d = B.STAGING / o["aoi"] / o["sceneId"]
    b = {k: (B.read_band(d / f"{k}.tif", g, H, W, Resampling.average) + o["offsetApplied"]) / 1e4
         for k in ("B02", "B03", "B04", "B08", "B11")}
    scl = B.read_band(d / "SCL.tif", g, H, W, Resampling.nearest).astype(np.uint8)
    unusable = np.isin(scl, (0, 3, 8, 9, 10)) | (b["B04"] <= 0)
    # Gate-1 haze test (haze-optimised transform, as in terralens.raster.quality): SCL misses
    # thin haze, which depresses NDVI and would otherwise masquerade as vegetation loss.
    hot = b["B02"] - 0.5 * b["B04"] - 0.08
    haze = (hot > HAZE_REF.get(o["aoi"], 0.0) + 0.02) & ~unusable & ((b["B03"] - b["B11"]) / (b["B03"] + b["B11"] + 1e-6) < 0.1)
    ndvi = (b["B08"] - b["B04"]) / (b["B08"] + b["B04"] + 1e-6)
    mndwi = (b["B03"] - b["B11"]) / (b["B03"] + b["B11"] + 1e-6)
    bright = (b["B02"] + b["B03"] + b["B04"]) / 3
    return {"ndvi": ndvi, "mndwi": mndwi, "bright": bright, "unusable": unusable | haze, "haze": haze, "scl": scl, "hot": hot}


HAZE_REF: dict[str, float] = {}


def post_state(spec, a, pre_bright=None, t_post=None):
    r = spec["rule"]
    if r == "veg_loss":
        return (a["ndvi"] < (t_post if t_post is not None else spec["t_post"])) & (a["mndwi"] < 0)
    if r == "water_loss":
        return a["mndwi"] <= 0.1 + (t_post or 0.0)
    if r == "brighten":
        return (a["bright"] >= pre_bright + (t_post if t_post is not None else 0.04)) & (a["ndvi"] < 0.25)
    raise ValueError(r)


def pre_state(spec, a, t_pre=None):
    r = spec["rule"]
    if r == "veg_loss":
        return a["ndvi"] >= (t_pre if t_pre is not None else spec["t_pre"])
    if r == "water_loss":
        return a["mndwi"] > 0.1
    return np.ones_like(a["ndvi"], bool)


def main():
    aois = yaml.safe_load((ROOT / "config/aois.yaml").read_text())["aois"]
    obs = json.loads((GEN / "observations.json").read_text())
    tf = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)
    IMG.mkdir(parents=True, exist_ok=True)
    out = {}
    for spec in SPECS:
        aoi = spec["aoi"]
        ao = sorted([o for o in obs if o["aoi"] == aoi], key=lambda o: o["date"])
        if aoi not in HAZE_REF:
            clear = sorted(ao, key=lambda o: -o["usableFraction"])[:6]
            HAZE_REF[aoi] = 0.0
            HAZE_REF[aoi] = float(np.median([np.nanpercentile(load(o, aois[aoi]["bbox"])["hot"], 90) for o in clear]))
        cache = {o["id"]: load(o, aois[aoi]["bbox"]) for o in ao}
        x0, y0, x1, y1 = spec["box"]
        sl = (slice(y0, y1), slice(x0, x1))
        W, H = ao[0]["pixel"]["width"], ao[0]["pixel"]["height"]
        ub = ao[0]["pixel"]["utmBounds"]
        tr = from_origin(ub[0], ub[3], 20, 20)

        def box_mask(m):
            full = np.zeros((H, W), bool)
            full[sl] = m[sl]
            return full

        if spec["rule"] == "quality":
            o = next(o for o in ao if o["date"] == spec["date"])
            a = cache[o["id"]]
            mask = box_mask(np.isin(a["scl"], (3, 8, 9)))
            masks = {"strict": mask, "loose": mask}
            pre_frames = []
            pre_bright = None
        else:
            in_w = lambda o, w: w[0] <= o["date"] <= w[1]
            pre_frames = [o for o in ao if in_w(o, spec["pre"]) and o["usableFraction"] > 0.9]
            post_frames = [o for o in ao if in_w(o, spec["post"]) and o["usableFraction"] > 0.9]
            pre_bright = np.mean([cache[o["id"]]["bright"] for o in pre_frames], 0) if spec["rule"] == "brighten" else None
            masks = {}
            for name, dt_pre, dt_post in (("mid", 0.0, 0.0), ("strict", 0.05, -0.03), ("loose", -0.05, 0.03)):
                tpre = spec.get("t_pre", 0) + dt_pre if spec["rule"] == "veg_loss" else None
                tpost = {"veg_loss": spec.get("t_post", 0) + dt_post, "brighten": 0.04 - dt_post, "water_loss": dt_post * 2}.get(spec["rule"])
                pre_ok = np.mean([pre_state(spec, cache[o["id"]], tpre) for o in pre_frames], 0) >= 0.6
                votes = np.sum([post_state(spec, cache[o["id"]], pre_bright, tpost) & ~cache[o["id"]]["unusable"] for o in post_frames], 0)
                m = pre_ok & (votes >= max(1, int(np.ceil(0.6 * len(post_frames)))))
                m = ndimage.binary_opening(box_mask(m)) if spec["rule"] != "brighten" else box_mask(ndimage.binary_opening(m))
                masks[name] = m
            mask = masks["mid"]
        lab, n = ndimage.label(mask)
        if n == 0:
            print(spec["id"], "EMPTY MASK")
            continue
        sizes = ndimage.sum(mask, lab, range(1, n + 1))
        keep = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s >= 4])
        polys = [shape(s) for s, v in shapes(keep.astype(np.uint8), transform=tr) if v == 1]
        geom = unary_union(polys).buffer(0)
        geom_ll = stf(tf.transform, geom.simplify(5))
        c = geom.centroid
        clon, clat = tf.transform(c.x, c.y)
        # per-observation series inside the polygon
        series = []
        first_date = np.full((H, W), -1, np.int32)
        for i, o in enumerate(ao):
            a = cache[o["id"]]
            u = ~a["unusable"][keep]
            us = float(u.mean()) if keep.any() else 0.0
            row = {"obsId": o["id"], "date": o["date"], "usableShare": round(us, 3), "postShare": None, "valueHa": None}
            if spec["rule"] != "quality" and us >= 0.8:
                ps = post_state(spec, a, pre_bright)
                share = float(ps[keep][u].mean())
                row["postShare"] = round(share, 3)
                row["valueHa"] = round(share * keep.sum() * PX_HA, 2)
                newly = keep & ps & ~a["unusable"] & (first_date < 0) & (o["date"] > (spec.get("pre") or ["0"])[1])
                first_date[newly] = i
            series.append(row)
        # overlays
        ov = np.zeros((H, W, 4), np.uint8)
        ov[keep] = (255, 64, 32, 190)
        edge = keep & ~ndimage.binary_erosion(keep)
        ov[edge] = (255, 230, 0, 255)
        Image.fromarray(ov[sl[0], sl[1]] if False else ov, "RGBA").save(IMG / f"{spec['id']}_mask.png", optimize=True)
        fc = np.zeros((H, W, 4), np.uint8)
        valid = first_date >= 0
        if valid.any():
            lo, hi = first_date[valid].min(), first_date[valid].max()
            t = (first_date - lo) / max(hi - lo, 1)
            import colorsys
            for idx in np.unique(first_date[valid]):
                r, g_, b_ = colorsys.hsv_to_rgb(0.62 - 0.62 * float(t[first_date == idx][0]), 0.85, 0.95)
                fc[first_date == idx] = (int(r * 255), int(g_ * 255), int(b_ * 255), 220)
        Image.fromarray(fc, "RGBA").save(IMG / f"{spec['id']}_firstchange.png", optimize=True)
        fc_legend = sorted({int(v) for v in np.unique(first_date[valid])}) if valid.any() else []
        out[spec["id"]] = {
            "id": spec["id"], "aoi": aoi, "rule": spec["rule"], "rulePre": spec.get("pre"), "rulePost": spec.get("post"),
            "thresholds": {k: spec[k] for k in ("t_pre", "t_post") if k in spec},
            "geometry": mapping(geom_ll), "centroid": [round(clon, 6), round(clat, 6)],
            "areaHa": round(float(keep.sum()) * PX_HA, 2),
            "areaRangeHa": [round(float(masks["strict"].sum()) * PX_HA, 2), round(float(masks["loose"].sum()) * PX_HA, 2)],
            "aoiShare": round(float(keep.sum()) / (H * W), 5),
            "boxBoundsLonLat": [list(tf.transform(ub[0] + x0 * 20, ub[3] - y0 * 20)), list(tf.transform(ub[0] + x1 * 20, ub[3] - y1 * 20))],
            "maskImage": f"demo/events/{spec['id']}_mask.png", "firstChangeImage": f"demo/events/{spec['id']}_firstchange.png",
            "firstChangeDates": [ao[i]["date"] for i in fc_legend],
            "preFrames": [o["id"] for o in pre_frames], "series": series,
        }
        print(spec["id"], "area ha", out[spec["id"]]["areaHa"], "range", out[spec["id"]]["areaRangeHa"])
    (GEN / "events_derived.json").write_text(json.dumps(out))


if __name__ == "__main__":
    main()
