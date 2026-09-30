"""Stage GIS reference layers for the demo AOIs (network needed once; never at runtime).

Rasters are warped onto each AOI's analysis grid so the runtime never reprojects them:
  worldcover  ESA WorldCover 2021 v200 (10 m, CC BY 4.0)          nearest
  jrc_occ     JRC Global Surface Water occurrence 1984-2021 v1.4    nearest (30 m source)
  jrc_seas    JRC GSW seasonality (months of water per year)        nearest
  dem         Copernicus DEM GLO-30                                 bilinear
  soil_vertisol  SoilGrids 2.0 WRB Vertisols probability (250 m)    nearest, kept coarse
Vectors from OpenStreetMap via Overpass (ODbL): rivers, canals, water polygons, roads,
selected land-use polygons. The extract date is recorded.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import httpx
import numpy as np
import rasterio
import yaml
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from terralens.raster.grid import aoi_grid  # noqa: E402

os.environ.update(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES",
                  GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", GDAL_HTTP_MULTIRANGE="YES",
                  CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.vrt,.TIF", GDAL_HTTP_MAX_RETRY="4")
OUT = ROOT / "data/gis"


def wc_tile(lat, lon):
    la, lo = int(np.floor(lat / 3) * 3), int(np.floor(lon / 3) * 3)
    return f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_N{la:02d}E{lo:03d}_Map.tif"


def dem_tile(lat, lon):
    la, lo = int(np.floor(lat)), int(np.floor(lon))
    n = f"Copernicus_DSM_COG_10_N{la:02d}_00_E{lo:03d}_00_DEM"
    return f"https://copernicus-dem-30m.s3.amazonaws.com/{n}/{n}.tif"


def jrc(layer):
    return f"https://storage.googleapis.com/global-surface-water/downloads2021/{layer}/{layer}_80E_30Nv1_4_2021.tif"


SOIL = "https://files.isric.org/soilgrids/latest/data/wrb/Vertisols.vrt"


CACHE = ROOT / "var/tmp/gis_cache"


def local_copy(url: str) -> str:
    """Whole-file download for sources whose remote windowed reads are slow (JRC, DEM).
    The cache lives in var/tmp and is deleted after staging."""
    if url.endswith(".vrt"):
        return url
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / url.rsplit("/", 1)[1]
    if not f.exists():
        tmp = f.with_suffix(".part")
        with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        tmp.rename(f)
    return str(f)


def warp_to_grid(src_url, g, out: Path, resampling, dtype, nodata=None, res=None):
    if out.exists():
        print("  exists", out.name)
        return
    if "worldcover" not in src_url:
        src_url = local_copy(src_url)
    crs, (xmin, ymin, xmax, ymax), gsd = g["crs"], g["bounds"], (res or g["gsd"])
    w, h = int(round((xmax - xmin) / gsd)), int(round((ymax - ymin) / gsd))
    tr = from_origin(xmin, ymax, gsd, gsd)
    with rasterio.open(src_url) as src:
        with WarpedVRT(src, crs=crs, transform=tr, width=w, height=h, resampling=resampling,
                       src_nodata=src.nodata, nodata=nodata if nodata is not None else src.nodata) as vrt:
            arr = vrt.read(1)
            src_nodata = src.nodata
    prof = dict(driver="GTiff", width=w, height=h, count=1, dtype=dtype, crs=crs, transform=tr,
                tiled=True, blockxsize=256, blockysize=256, compress="deflate", nodata=nodata if nodata is not None else src_nodata)
    out.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out, "w", **prof) as dst:
        dst.write(arr.astype(dtype), 1)
        dst.update_tags(source=src_url, staged_at=dt.datetime.now(dt.timezone.utc).isoformat())
    print("  wrote", out.relative_to(ROOT), arr.shape, "unique" if arr.dtype == np.uint8 else "", np.unique(arr)[:12] if arr.dtype == np.uint8 else f"{arr.min():.1f}..{arr.max():.1f}")


OVERPASS_ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                      "https://overpass.private.coffee/api/interpreter"]
OVERPASS = OVERPASS_ENDPOINTS[0]


def overpass(bbox, buffer_deg=0.02):
    import time
    w, s, e, n = bbox[0] - buffer_deg, bbox[1] - buffer_deg, bbox[2] + buffer_deg, bbox[3] + buffer_deg
    bb = f"{s},{w},{n},{e}"
    parts = [
        f'way["waterway"~"^(river|stream|canal)$"]({bb});way["natural"="water"]({bb});relation["natural"="water"]({bb});way["waterway"="riverbank"]({bb});',
        f'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link)$"]({bb});',
        f'way["landuse"~"^(quarry|industrial|residential|farmland|forest|reservoir|basin)$"]({bb});relation["landuse"~"^(quarry|industrial|residential|forest|reservoir)$"]({bb});way["aeroway"="aerodrome"]({bb});way["power"="plant"]({bb});',
        f'node["place"~"^(city|town|village|suburb|neighbourhood)$"]({bb});',
    ]
    elements = []
    for part in parts:
        ql = f"[out:json][timeout:120];({part});out geom;"
        last = None
        for attempt in range(6):
            ep = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
            try:
                r = httpx.post(ep, data={"data": ql}, timeout=180,
                               headers={"User-Agent": "TerraLens-staging/0.9 (offline prototype; one-time extract)"})
                r.raise_for_status()
                elements += r.json()["elements"]
                last = None
                break
            except Exception as ex:
                last = ex
                time.sleep(5 + 5 * attempt)
        if last:
            raise last
    return {"elements": elements}


def main():
    aois = yaml.safe_load((ROOT / "config/aois.yaml").read_text())["aois"]
    only = sys.argv[1:]
    for name, a in aois.items():
        if only and name not in only:
            continue
        g = aoi_grid(a["bbox"])
        lon, lat = (a["bbox"][0] + a["bbox"][2]) / 2, (a["bbox"][1] + a["bbox"][3]) / 2
        d = OUT / name
        print(f"[{name}] grid {g['bounds']}", flush=True)
        try:
            warp_to_grid(wc_tile(lat, lon), g, d / "worldcover.tif", Resampling.nearest, "uint8", nodata=0)
        except Exception as e:
            print("  worldcover FAILED", e)
        for layer in ("occurrence", "seasonality"):
            try:
                warp_to_grid(jrc(layer), g, d / f"jrc_{layer}.tif", Resampling.nearest, "uint8", nodata=255)
            except Exception as e:
                print(f"  jrc {layer} FAILED", e)
        try:
            warp_to_grid(dem_tile(lat, lon), g, d / "dem.tif", Resampling.bilinear, "float32", nodata=-9999)
        except Exception as e:
            print("  dem FAILED", e)
        try:
            warp_to_grid(SOIL, g, d / "soil_vertisol.tif", Resampling.nearest, "uint8", nodata=255, res=250)
        except Exception as e:
            print("  soil FAILED", e)
        osm = d / "osm.json"
        if not osm.exists():
            try:
                data = overpass(a["bbox"])
                data["terralens:extract"] = {"date": dt.date.today().isoformat(), "source": "Overpass API",
                                             "license": "ODbL 1.0, (c) OpenStreetMap contributors"}
                osm.write_text(json.dumps(data))
                print("  osm elements", len(data["elements"]))
            except Exception as e:
                print("  osm FAILED", e)


if __name__ == "__main__":
    main()
    import shutil
    shutil.rmtree(CACHE, ignore_errors=True)
