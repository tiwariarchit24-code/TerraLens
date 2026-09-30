"""Stage real Sentinel-2 L2A AOI clips from the public Earth Search STAC (AWS open data).

This is a STAGING script: it runs once, with network access, before the system is
taken offline. The running application never calls it and never touches the network.

Output layout (the "sentinel2-l2a-clip" raw format read by
terralens.ingest.adapters.sentinel2_clip):

    data/staging/sentinel2/<aoi>/<item_id>/
        B02.tif B03.tif B04.tif B08.tif   10 m, uint16 DN exactly as delivered
        B11.tif B12.tif SCL.tif           20 m, native resolution, not resampled
        item.json                         full STAC item + a "terralens:staging" block

Only a spatial subset is taken (integer pixel window aligned to the source grid);
pixel values and resolution are never altered here. Radiometric offsets are NOT
applied at staging - that is the ingest pipeline's job (exactly once).

Scene selection per AOI: one lowest-cloud acquisition per time bin, so the archive
keeps natural cloudy periods (monsoon) instead of cherry-picking clear dates.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import numpy as np
import rasterio
import yaml
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
from shapely.geometry import box, shape

ROOT = Path(__file__).resolve().parents[2]
STAC = "https://earth-search.aws.element84.com/v1/search"
BANDS = {"B02": "blue", "B03": "green", "B04": "red", "B08": "nir", "B11": "swir16", "B12": "swir22", "SCL": "scl"}
os.environ.update(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    AWS_NO_SIGN_REQUEST="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_MULTIRANGE="YES",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
    GDAL_HTTP_MAX_RETRY="4",
    GDAL_HTTP_RETRY_DELAY="2",
)

# Per-AOI selection windows: (start, end, bin_days)
PLANS: dict[str, list[tuple[str, str, int]]] = {
    "default": [("2024-10-01", "2026-09-30", 15)],
    "prayagraj": [
        ("2024-09-01", "2024-10-31", 15),
        ("2024-11-01", "2025-04-30", 8),   # dense around the Maha Kumbh (13 Jan - 26 Feb 2025)
        ("2025-05-01", "2026-09-30", 20),
    ],
    "gangrel": [("2024-10-01", "2026-09-30", 15)]
    # same-season (pre-monsoon) history for the seasonal water gate
    + [(f"{y}-03-01", f"{y}-05-31", 31) for y in range(2019, 2024)]
    # scenes either side of the 25 Jan 2022 processing-baseline 04.00 offset change
    + [("2021-12-01", "2022-01-24", 60), ("2022-01-26", "2022-03-15", 60)],
}


def search(bbox, start, end):
    items, token = [], None
    body = {"collections": ["sentinel-2-l2a"], "bbox": bbox, "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
            "limit": 200, "query": {"eo:cloud_cover": {"lt": 90}}}
    with httpx.Client(timeout=120) as c:
        while True:
            b = dict(body)
            if token:
                b["next"] = token
            r = c.post(STAC, json=b)
            r.raise_for_status()
            d = r.json()
            items += d["features"]
            nxt = [l for l in d.get("links", []) if l.get("rel") == "next"]
            if not nxt:
                break
            token = nxt[0].get("body", {}).get("next")
            if not token:
                break
    return items


def select(items, aoi_poly, start, end, bin_days):
    ok = []
    for it in items:
        p = it["properties"]
        if p.get("proj:epsg") != 32644:
            continue
        if not shape(it["geometry"]).buffer(-0.002).contains(aoi_poly):
            continue  # AOI must be fully inside the valid-data footprint
        ok.append(it)
    # one acquisition per sensing date: prefer the latest processing, then lowest cloud
    by_date: dict[str, dict] = {}
    for it in ok:
        d = it["properties"]["datetime"][:10]
        key = (it["properties"].get("s2:processing_baseline", ""), -it["properties"]["eo:cloud_cover"])
        if d not in by_date or key > by_date[d][0]:
            by_date[d] = (key, it)
    t0 = dt.date.fromisoformat(start)
    bins: dict[int, dict] = {}
    for d, (_, it) in by_date.items():
        b = (dt.date.fromisoformat(d) - t0).days // bin_days
        if b not in bins or it["properties"]["eo:cloud_cover"] < bins[b]["properties"]["eo:cloud_cover"]:
            bins[b] = it
    return sorted(bins.values(), key=lambda i: i["properties"]["datetime"])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_band(it, band, bounds_utm, outdir: Path):
    href = it["assets"][BANDS[band]]["href"]
    out = outdir / f"{band}.tif"
    if out.exists():
        return band, href, sha256(out)
    with rasterio.open(href) as ds:
        win = from_bounds(*bounds_utm, ds.transform)
        win = win.round_offsets().round_lengths()
        arr = ds.read(1, window=win, boundless=False)
        prof = ds.profile.copy()
        prof.update(driver="GTiff", height=arr.shape[0], width=arr.shape[1], transform=ds.window_transform(win),
                    tiled=True, blockxsize=256, blockysize=256, compress="deflate", predictor=2, BIGTIFF="NO")
        prof.pop("photometric", None)
    tmp = out.with_suffix(".part.tif")
    with rasterio.open(tmp, "w", **prof) as dst:
        dst.write(arr, 1)
        dst.set_band_description(1, band)
    tmp.rename(out)
    return band, href, sha256(out)


def stage_item(it, aoi_name, bounds_utm, root: Path):
    outdir = root / aoi_name / it["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    if (outdir / "item.json").exists():
        return it["id"], "exists"
    files = {}
    # Granule metadata XML (public): sun and per-band viewing angle grids. The product
    # metadata XML (BOA_ADD_OFFSET values) sits in a requester-pays bucket and is not
    # staged; the offset decision therefore uses s2:processing_baseline together with
    # earthsearch:boa_offset_applied (see terralens.ingest.adapters.sentinel2_clip).
    for asset, fname in (("granule_metadata", "MTD_TL.xml"), ("product_metadata", "MTD_MSIL2A.xml")):
        href = it["assets"].get(asset, {}).get("href")
        if not href or not href.startswith("https://") or (outdir / fname).exists():
            continue
        r = httpx.get(href, timeout=120)
        r.raise_for_status()
        (outdir / fname).write_bytes(r.content)
        files[fname] = {"source_href": href, "sha256": sha256(outdir / fname)}
    for band in BANDS:
        b, href, digest = fetch_band(it, band, bounds_utm, outdir)
        files[b] = {"source_href": href, "sha256": digest}
    it = dict(it)
    it["terralens:staging"] = {
        "staged_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "aoi": aoi_name,
        "window_bounds_utm": list(bounds_utm),
        "crs": "EPSG:32644",
        "files": files,
        "note": "Spatial subset only; DN values and native resolution unchanged; no offset applied.",
        "source": "Earth Search STAC v1 (Element 84), collection sentinel-2-l2a, AWS open data",
        "attribution": "Contains modified Copernicus Sentinel data",
    }
    (outdir / "item.json").write_text(json.dumps(it, indent=1))
    return it["id"], "staged"


def aoi_bounds_utm(bbox, snap=60.0):
    w, s, e, n = transform_bounds("EPSG:4326", "EPSG:32644", *bbox)
    return (np.floor(w / snap) * snap, np.floor(s / snap) * snap, np.ceil(e / snap) * snap, np.ceil(n / snap) * snap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aoi", nargs="*")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config/aois.yaml").read_text())
    root = ROOT / "data/staging/sentinel2"
    for name, a in cfg["aois"].items():
        if args.aoi and name not in args.aoi:
            continue
        poly = box(*a["bbox"])
        bounds = aoi_bounds_utm(a["bbox"])
        chosen = {}
        for start, end, bin_days in PLANS.get(name, PLANS["default"]):
            items = search(a["bbox"], start, end)
            for it in select(items, poly, start, end, bin_days):
                chosen[it["id"]] = it
        chosen = sorted(chosen.values(), key=lambda i: i["properties"]["datetime"])
        print(f"[{name}] {len(chosen)} scenes selected, window {bounds}", flush=True)
        if args.dry_run:
            for it in chosen:
                p = it["properties"]
                print("   ", it["id"], p["datetime"][:10], f"cc={p['eo:cloud_cover']:.0f}", p.get("s2:processing_baseline"), p.get("earthsearch:boa_offset_applied"))
            continue
        with ThreadPoolExecutor(args.workers) as ex:
            futs = [ex.submit(stage_item, it, name, bounds, root) for it in chosen]
            for i, f in enumerate(as_completed(futs)):
                try:
                    iid, st = f.result()
                    print(f"  [{name}] {i + 1}/{len(chosen)} {iid} {st}", flush=True)
                except Exception as e:  # keep going; a failed scene is simply not staged
                    print(f"  [{name}] FAILED: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
