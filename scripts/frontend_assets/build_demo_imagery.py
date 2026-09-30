"""Render the staged REAL Sentinel-2 L2A clips into static frames for the frontend demo mode.

Output: frontend/public/demo/imagery/<aoi>/<date>_<platform>.jpg  (true colour, ONE fixed
linear stretch 0-0.30 reflectance for every frame; never per-frame auto-stretch)
        frontend/public/demo/imagery/<aoi>/<date>_<platform>_mask.png  (SCL cloud/shadow/cirrus overlay)
        frontend/src/data/demo/generated/observations.json  (real per-frame metadata)
        frontend/src/data/demo/generated/water.json          (MNDWI water extent, one clear frame per AOI)

Radiometry: the same rule as the backend adapter - subtract the 1000 DN offset only when
the product carries it AND the darkest valid NIR/SWIR pixels show it is still present.
No interpolation, no synthesis: frames exist only for real acquisitions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.windows import from_bounds
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402

from terralens.raster.grid import aoi_grid  # noqa: E402

STAGING = ROOT / "data/staging/sentinel2"
OUT_IMG = ROOT / "frontend/public/demo/imagery"
OUT_DATA = ROOT / "frontend/src/data/demo/generated"
STRETCH = (0.0, 0.30)
DISPLAY_GSD = 20.0          # frames are rendered at 20 m to keep the static bundle small


def read_band(p: Path, grid, H, W, rs):
    with rasterio.open(p) as ds:
        win = from_bounds(*grid["bounds"], ds.transform)
        return ds.read(1, window=win, out_shape=(H, W), resampling=rs, boundless=True, fill_value=0).astype(np.float32)


def main():
    aois = yaml.safe_load((ROOT / "config/aois.yaml").read_text())["aois"]
    tf = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)
    all_obs, water = [], {}
    for aoi, a in aois.items():
        g = aoi_grid(a["bbox"])
        xmin, ymin, xmax, ymax = g["bounds"]
        H, W = int((ymax - ymin) / DISPLAY_GSD), int((xmax - xmin) / DISPLAY_GSD)
        corners = [list(tf.transform(x, y)) for x, y in [(xmin, ymax), (xmax, ymax), (xmax, ymin), (xmin, ymin)]]
        (OUT_IMG / aoi).mkdir(parents=True, exist_ok=True)
        best = None
        for d in sorted(p for p in (STAGING / aoi).iterdir() if (p / "item.json").exists()):
            item = json.loads((d / "item.json").read_text())
            pr = item["properties"]
            date = pr["datetime"][:10]
            if date < "2024-09-01":
                continue   # historical same-season scenes are not part of the demo timeline
            b = {k: read_band(d / f"{k}.tif", g, H, W, Resampling.average) for k in ("B02", "B03", "B04", "B08", "B11")}
            scl = read_band(d / "SCL.tif", g, H, W, Resampling.nearest).astype(np.uint8)
            valid = b["B04"] > 0
            dark = np.concatenate([b["B08"][valid], b["B11"][valid]])
            has_offset = tuple(int(x) for x in pr.get("s2:processing_baseline", "0.0").split(".")) >= (4, 0)
            offset_present = has_offset and dark.size and np.percentile(dark, 0.5) > 950
            off = -1000.0 if offset_present else 0.0
            refl = {k: np.where(valid, (v + off) / 10000.0, np.nan) for k, v in b.items()}
            cloud = np.isin(scl, (8, 9, 10))
            shadow = scl == 3
            unusable = cloud | shadow | (scl == 0) | ~valid
            usable_frac = float(1 - unusable.mean())
            rgb = np.stack([refl["B04"], refl["B03"], refl["B02"]], -1)
            img = (np.nan_to_num(np.clip((rgb - STRETCH[0]) / (STRETCH[1] - STRETCH[0]), 0, 1)) * 255).astype(np.uint8)
            stem = f"{date}_{pr['platform'].replace('sentinel-', 'S')}"
            Image.fromarray(img).save(OUT_IMG / aoi / f"{stem}.jpg", quality=80, optimize=True)
            m = np.zeros((H, W, 4), np.uint8)
            m[cloud] = (235, 235, 235, 170)
            m[shadow] = (40, 40, 120, 170)
            m[(scl == 0) | ~valid] = (0, 0, 0, 200)
            Image.fromarray(m, "RGBA").save(OUT_IMG / aoi / f"{stem}_mask.png", optimize=True)
            mndwi = (refl["B03"] - refl["B11"]) / (refl["B03"] + refl["B11"] + 1e-6)
            ndvi = (refl["B08"] - refl["B04"]) / (refl["B08"] + refl["B04"] + 1e-6)
            all_obs.append({
                "id": f"{aoi}:{stem}", "aoi": aoi, "date": date, "datetime": pr["datetime"], "sceneId": item["id"],
                "platform": pr["platform"], "sensor": "sentinel-2", "processingLevel": "L2A",
                "processingBaseline": pr.get("s2:processing_baseline"),
                "providerOffsetFlag": pr.get("earthsearch:boa_offset_applied"), "offsetApplied": off,
                "sceneCloudCover": round(pr.get("eo:cloud_cover", 0), 1), "usableFraction": round(usable_frac, 3),
                "cloudFraction": round(float(cloud.mean()), 3), "shadowFraction": round(float(shadow.mean()), 3),
                "sunElevation": pr.get("view:sun_elevation"), "sunAzimuth": pr.get("view:sun_azimuth"),
                "viewIncidence": pr.get("view:incidence_angle"), "gsd": 10,
                "image": f"demo/imagery/{aoi}/{stem}.jpg", "mask": f"demo/imagery/{aoi}/{stem}_mask.png",
                "stats": {"waterFraction": round(float(np.nanmean((mndwi > 0.1)[~unusable])) if (~unusable).any() else 0, 4),
                          "meanNdvi": round(float(np.nanmean(ndvi[~unusable])) if (~unusable).any() else 0, 4)},
                "_arrays": (mndwi, ndvi, unusable),
            })
            if usable_frac > 0.98 and (best is None or date > best[0]) and date[5:7] in ("11", "12", "01", "02"):
                best = (date, mndwi, g, H, W)
        if best:
            date, mndwi, g2, H2, W2 = best
            tr = rasterio.transform.from_origin(g2["bounds"][0], g2["bounds"][3], DISPLAY_GSD, DISPLAY_GSD)
            polys = [shape(s) for s, v in shapes((mndwi > 0.1).astype(np.uint8), transform=tr) if v == 1]
            polys = [p for p in polys if p.area > 40000]  # >= 4 ha
            if polys:
                u = unary_union(polys).simplify(20)
                from shapely.ops import transform as stf
                water[aoi] = {"date": date, "geometry": mapping(stf(tf.transform, u))}
        for o in [o for o in all_obs if o["aoi"] == aoi]:
            o["bounds"] = corners
            o["pixel"] = {"width": W, "height": H, "gsd": DISPLAY_GSD, "utmBounds": list(g["bounds"])}
    OUT_DATA.mkdir(parents=True, exist_ok=True)
    # per-polygon demo measurements are added by build_demo_measurements(); arrays are dropped here
    np.save(ROOT / "var/demo_arrays.npy", np.array([0]))
    _measure(all_obs)
    for o in all_obs:
        o.pop("_arrays", None)
    (OUT_DATA / "observations.json").write_text(json.dumps(all_obs, indent=0))
    (OUT_DATA / "water.json").write_text(json.dumps(water))
    print(len(all_obs), "frames;", {a: sum(o["aoi"] == a for o in all_obs) for a in aois})


def _measure(all_obs):
    """Simple index-threshold measurements inside the demo event polygons (pixel boxes in
    frontend/src/data/demo/event_polygons.json). These are DEMO measurements: thresholded
    indices on usable pixels only, with no gates and no classifier."""
    f = ROOT / "frontend/src/data/demo/event_polygons.json"
    if not f.exists():
        return
    polys = json.loads(f.read_text())
    out = {}
    for ev in polys:
        rows = []
        for o in [o for o in all_obs if o["aoi"] == ev["aoi"]]:
            mndwi, ndvi, unusable = o["_arrays"]
            x0, y0, x1, y1 = ev["pixelBox"]
            sl = (slice(y0, y1), slice(x0, x1))
            un = unusable[sl]
            ok = ~un
            usable_share = float(ok.mean())
            if ev["measure"] == "water":
                v = (mndwi[sl] > 0.1) & ok
            elif ev["measure"] == "nonveg":
                v = (ndvi[sl] < 0.2) & (mndwi[sl] < 0) & ok
            else:
                v = (ndvi[sl] >= 0.4) & ok
            px_ha = (20 * 20) / 10000
            rows.append({"obsId": o["id"], "date": o["date"], "usableShare": round(usable_share, 3),
                         "valueHa": round(float(v.sum()) * px_ha, 2) if usable_share >= 0.8 else None})
        out[ev["id"]] = rows
    (OUT_DATA / "measurements.json").write_text(json.dumps(out, indent=0))


if __name__ == "__main__":
    main()
