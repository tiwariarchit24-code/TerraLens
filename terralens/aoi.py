"""AOI and grid-tile registration, including the spatially blocked dev/test split."""
from __future__ import annotations

import hashlib

import numpy as np
import rasterio
from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import transform as shp_transform

from .config import aois as aoi_cfg
from .config import path, settings
from .db import J, conn, q
from .raster.grid import aoi_grid, tiles_for_aoi
from .version import GRID_VERSION

BLOCK_M = 2560.0          # evaluation block (4 x 4 tile strides); tiles inherit their block's split
TEST_SHARE = 0.3
WC_CLASSES = {10: "tree", 20: "shrub", 30: "grass", 40: "cropland", 50: "built", 60: "bare", 70: "snow",
              80: "water", 90: "wetland", 95: "mangrove", 100: "moss"}


def _to4326():
    return Transformer.from_crs(settings()["grid"]["crs"], "EPSG:4326", always_xy=True).transform


def block_split(bx: int, by: int) -> str:
    """Deterministic, seed-free assignment of an evaluation block to dev or test."""
    h = int(hashlib.sha256(f"terralens-block-{bx}-{by}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "test" if h < TEST_SHARE else "dev"


def _tile_split(x0: float, y0: float, x1: float, y1: float) -> tuple[str, str]:
    bxs = {int(np.floor(x0 / BLOCK_M)), int(np.floor((x1 - 1) / BLOCK_M))}
    bys = {int(np.floor(y0 / BLOCK_M)), int(np.floor((y1 - 1) / BLOCK_M))}
    splits = {block_split(bx, by) for bx in bxs for by in bys}
    block = f"b{min(bxs)}_{min(bys)}"
    return (splits.pop() if len(splits) == 1 else "buffer"), block


def landcover_fractions(aoi_id: str, window) -> dict | None:
    f = path("gis") / aoi_id / "worldcover.tif"
    if not f.exists():
        return None
    with rasterio.open(f) as ds:
        a = ds.read(1, window=window)
    vals, counts = np.unique(a[a > 0], return_counts=True)
    tot = counts.sum()
    return {WC_CLASSES.get(int(v), str(v)): round(float(c) / tot, 4) for v, c in zip(vals, counts)} if tot else None


def ensure_aois() -> list[str]:
    ids = []
    tf = _to4326()
    for aoi_id, a in aoi_cfg()["aois"].items():
        g = aoi_grid(a["bbox"])
        utm = box(*g["bounds"])
        with conn() as c:
            c.execute(
                """INSERT INTO aois (id, name, crs, gsd_m, grid_xmin, grid_ymin, grid_xmax, grid_ymax, width, height, geom_utm, geom)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, ST_GeomFromText(%s, 32644), ST_GeomFromText(%s, 4326))
                   ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name""",
                (aoi_id, a["name"], g["crs"], g["gsd"], *g["bounds"], g["width"], g["height"], utm.wkt,
                 shp_transform(tf, utm).wkt))
            existing = {r["tile_id"] for r in c.execute("SELECT tile_id FROM grid_tiles WHERE aoi_id=%s", (aoi_id,))}
            for t in tiles_for_aoi(g):
                if t.tile_id in existing:
                    continue
                fp = box(t.x0, t.y0, t.x1, t.y1)
                fp4 = shp_transform(tf, fp)
                split, block = _tile_split(t.x0, t.y0, t.x1, t.y1)
                c.execute(
                    """INSERT INTO grid_tiles (tile_id, aoi_id, epsg, gsd_m, size_px, stride_px, col, row, grid_version,
                         footprint_utm, footprint, centroid, landcover, split, block_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, ST_GeomFromText(%s,32644), ST_GeomFromText(%s,4326),
                               ST_Centroid(ST_GeomFromText(%s,4326)), %s, %s, %s)
                       ON CONFLICT (tile_id) DO NOTHING""",
                    (t.tile_id, aoi_id, g["epsg"], g["gsd"], settings()["grid"]["tile_size_px"],
                     settings()["grid"]["tile_stride_px"], t.col, t.row, GRID_VERSION, fp.wkt, fp4.wkt, fp4.wkt,
                     J(landcover_fractions(aoi_id, t.window)), split, block))
        ids.append(aoi_id)
    return ids


def refresh_landcover() -> int:
    """Fill tile land-cover composition once WorldCover has been staged."""
    n = 0
    for aoi_id, a in aoi_cfg()["aois"].items():
        g = aoi_grid(a["bbox"])
        with conn() as c:
            for t in tiles_for_aoi(g):
                lc = landcover_fractions(aoi_id, t.window)
                if lc:
                    c.execute("UPDATE grid_tiles SET landcover=%s WHERE tile_id=%s", (J(lc), t.tile_id))
                    n += 1
    return n


def get_aoi(aoi_id: str) -> dict:
    r = q("SELECT * FROM aois WHERE id=%s", (aoi_id,))
    if not r:
        raise KeyError(aoi_id)
    a = r[0]
    g = aoi_grid(aoi_cfg()["aois"][aoi_id]["bbox"])
    a["grid"] = g
    return a


def aoi_for_footprint(footprint_wkt: str | None) -> list[str]:
    """AOIs fully covered by a scene footprint (partial coverage is allowed: the no-data
    mask handles it), falling back to any intersecting AOI."""
    if not footprint_wkt:
        return []
    rows = q("SELECT id FROM aois WHERE ST_Intersects(geom, ST_GeomFromText(%s, 4326)) ORDER BY id", (footprint_wkt,))
    return [r["id"] for r in rows]
