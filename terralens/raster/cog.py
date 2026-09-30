"""Cloud Optimized GeoTIFF writing and windowed reading."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator

import numpy as np
import rasterio
from rasterio.shutil import copy as rio_copy
from rasterio.windows import Window


def write_cog(path: Path, arr: np.ndarray, transform, crs: str, nodata=None, descriptions: list[str] | None = None,
              tags: dict | None = None, resampling: str = "average", blocksize: int = 256) -> Path:
    """Write a (bands, rows, cols) array as a COG (internal tiling + overviews) atomically."""
    if arr.ndim == 2:
        arr = arr[None]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".gtiff.tif", dir=path.parent)
    os.close(fd)
    fd2, tmp_cog = tempfile.mkstemp(suffix=".cog.tif", dir=path.parent)
    os.close(fd2)
    try:
        # GDAL's COG driver is CreateCopy-only: write a plain tiled GeoTIFF first, then copy.
        prof = dict(driver="GTiff", width=arr.shape[2], height=arr.shape[1], count=arr.shape[0], dtype=arr.dtype,
                    crs=crs, transform=transform, nodata=nodata, tiled=True, blockxsize=blocksize,
                    blockysize=blocksize, interleave="band")
        with rasterio.open(tmp, "w", **prof) as dst:
            dst.write(arr)
            if descriptions:
                for i, d in enumerate(descriptions, 1):
                    dst.set_band_description(i, d)
            if tags:
                dst.update_tags(**{k: str(v) for k, v in tags.items()})
        rio_copy(tmp, tmp_cog, driver="COG", compress="DEFLATE",
                 predictor="2" if np.issubdtype(arr.dtype, np.integer) else "3",
                 blocksize=str(blocksize), overview_resampling=resampling.upper(), BIGTIFF="IF_SAFER")
        os.replace(tmp_cog, path)
    finally:
        for t in (tmp, tmp_cog):
            if os.path.exists(t):
                os.remove(t)
    return path


def iter_blocks(height: int, width: int, block: int) -> Iterator[Window]:
    for r in range(0, height, block):
        for c in range(0, width, block):
            yield Window(c, r, min(block, width - c), min(block, height - r))


def read(path: Path, window: Window | None = None, bands: list[int] | None = None) -> np.ndarray:
    with rasterio.open(path) as ds:
        return ds.read(bands, window=window) if bands else ds.read(window=window)
