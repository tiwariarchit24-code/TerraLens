"""Deterministic fixed analysis grid.

* One analysis CRS per archive (UTM 44N, EPSG:32644, for all demo AOIs).
* Pixel grid origin (0, 0) with 10 m pixels: every pixel edge is a multiple of 10 m,
  and the native Sentinel-2 10/20/60 m grids (origins on multiples of 60 m) align with
  it, so 10 m bands are cropped, never resampled, and 20 m bands upsample exactly 2x.
* Tiles are 128 x 128 px (1.28 km) on a 64 px (640 m) stride. A tile's ID is derived
  only from its position in the global grid, so a place keeps its ID on every date,
  in every scene and in every AOI that contains it.
* AOI rasters are snapped inward to multiples of the tile stride, so AOI pixels and
  tiles line up exactly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.windows import Window

from ..config import settings


def _g():
    return settings()["grid"]


def epsg() -> int:
    return int(_g()["crs"].split(":")[1])


def aoi_grid(bbox_wgs84: list[float] | tuple[float, ...]) -> dict:
    g = _g()
    gsd, stride = float(g["gsd_m"]), int(g["tile_stride_px"])
    step = gsd * stride
    t = Transformer.from_crs("EPSG:4326", g["crs"], always_xy=True)
    w, s, e, n = bbox_wgs84
    xs, ys = zip(*[t.transform(x, y) for x, y in [(w, s), (w, n), (e, s), (e, n)]])
    # inward snap: the grid box is fully inside the requested (and staged) area
    xmin, ymin = math.ceil(max(xs[0], xs[1]) / step) * step, math.ceil(max(ys[0], ys[2]) / step) * step
    xmax, ymax = math.floor(min(xs[2], xs[3]) / step) * step, math.floor(min(ys[1], ys[3]) / step) * step
    width, height = int(round((xmax - xmin) / gsd)), int(round((ymax - ymin) / gsd))
    return {"crs": g["crs"], "epsg": epsg(), "gsd": gsd, "bounds": (xmin, ymin, xmax, ymax),
            "width": width, "height": height, "transform": from_origin(xmin, ymax, gsd, gsd)}


@dataclass(frozen=True)
class Tile:
    tile_id: str
    col: int
    row: int
    x0: float
    y0: float
    x1: float
    y1: float
    window: Window  # pixel window inside the AOI raster


def tile_id(col: int, row: int) -> str:
    g = _g()
    return f"{epsg()}_{int(g['gsd_m'])}m_{int(g['tile_size_px'])}s{int(g['tile_stride_px'])}_c{col}_r{row}"


def parse_tile_id(tid: str) -> tuple[int, int]:
    parts = tid.split("_")
    return int(parts[-2][1:]), int(parts[-1][1:])


def tile_bounds(col: int, row: int) -> tuple[float, float, float, float]:
    g = _g()
    gsd, size, stride = float(g["gsd_m"]), int(g["tile_size_px"]), int(g["tile_stride_px"])
    x0, y0 = col * stride * gsd + g["origin"][0], row * stride * gsd + g["origin"][1]
    return x0, y0, x0 + size * gsd, y0 + size * gsd


def tiles_for_aoi(grid: dict) -> list[Tile]:
    g = _g()
    gsd, size, stride = float(g["gsd_m"]), int(g["tile_size_px"]), int(g["tile_stride_px"])
    step, ext = stride * gsd, size * gsd
    xmin, ymin, xmax, ymax = grid["bounds"]
    out = []
    for col in range(int(round(xmin / step)), int(round((xmax - ext) / step)) + 1):
        for row in range(int(round(ymin / step)), int(round((ymax - ext) / step)) + 1):
            x0, y0, x1, y1 = tile_bounds(col, row)
            win = Window(int(round((x0 - xmin) / gsd)), int(round((ymax - y1) / gsd)), size, size)
            out.append(Tile(tile_id(col, row), col, row, x0, y0, x1, y1, win))
    return out


def tile_containing(x: float, y: float) -> tuple[int, int]:
    """The tile whose central 64 px core contains (x, y): each point maps to exactly one
    tile even though tiles overlap by 50 %."""
    g = _g()
    gsd, size, stride = float(g["gsd_m"]), int(g["tile_size_px"]), int(g["tile_stride_px"])
    step, margin = stride * gsd, (size - stride) * gsd / 2   # tile col c has core [c*step+margin, c*step+margin+step)
    return int(math.floor((x - g["origin"][0] - margin) / step)), int(math.floor((y - g["origin"][1] - margin) / step))
