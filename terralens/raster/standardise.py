"""Standardisation: raw band files -> surface reflectance on the AOI analysis grid.

* 10 m bands on the same UTM grid are cropped (pixel-aligned window read, no resampling).
* 20 m bands (B11, B12) are upsampled exactly 2x (bilinear); SCL uses nearest.
* Scenes in another CRS are warped with a WarpedVRT (bilinear; nearest for SCL).
* The radiometric offset decided at registration is applied here and ONLY here:
      reflectance = (DN + offset_to_apply) / scale        DN == 0 -> no-data
  Raw files are opened read-only and never modified.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds

from ..config import ROOT
from ..ingest.adapters import BandSource
from .indices import BANDS


@dataclass
class Standardised:
    refl: np.ndarray        # float32 (6, H, W), NaN where no-data
    scl: np.ndarray | None  # uint8 (H, W) SCL codes, 0 = no data
    nodata: np.ndarray      # bool (H, W)
    saturated: np.ndarray   # bool (H, W)
    stats: dict


def _read_on_grid(src: BandSource, grid: dict, resampling: Resampling) -> np.ndarray:
    H, W = grid["height"], grid["width"]
    with rasterio.open(src.path) as ds:
        if ds.crs and ds.crs.to_string() == grid["crs"]:
            win = from_bounds(*grid["bounds"], ds.transform)
            return ds.read(src.band_index, window=win, out_shape=(H, W), resampling=resampling,
                           boundless=True, fill_value=0)
        with WarpedVRT(ds, crs=grid["crs"], transform=grid["transform"], width=W, height=H,
                       resampling=resampling, src_nodata=0, nodata=0) as vrt:
            return vrt.read(src.band_index)


def standardise(bands: list[BandSource], radiometric: dict, grid: dict, scene_saturation_dn: int = 65535,
                resample_20m: str = "bilinear") -> Standardised:
    H, W = grid["height"], grid["width"]
    by = {b.band: b for b in bands}
    for b in by.values():
        if not b.path.is_absolute():
            b.path = ROOT / b.path
    refl = np.full((len(BANDS), H, W), np.nan, dtype=np.float32)
    nodata = np.zeros((H, W), bool)
    saturated = np.zeros((H, W), bool)
    scale = float(radiometric.get("scale", 10000))
    offsets = radiometric.get("offset_to_apply", {})
    clipped_negative = 0
    for i, name in enumerate(BANDS):
        src = by[name]
        rs = Resampling.nearest if abs(src.native_gsd - grid["gsd"]) < 1e-6 else getattr(Resampling, resample_20m)
        dn = _read_on_grid(src, grid, rs).astype(np.float32)
        nd = dn <= 0
        nodata |= nd
        saturated |= dn >= scene_saturation_dn
        r = (dn + float(offsets.get(name, 0))) / scale
        neg = (~nd) & (r <= 0)
        clipped_negative += int(neg.sum())
        r[neg] = 1.0 / scale   # dark pixels (deep water) can fall below zero after the offset
        r[nd] = np.nan
        refl[i] = r
    scl = None
    if "SCL" in by:
        scl = _read_on_grid(by["SCL"], grid, Resampling.nearest).astype(np.uint8)
    refl[:, nodata] = np.nan
    stats = {"nodata_fraction": float(nodata.mean()), "saturated_fraction": float(saturated.mean()),
             "negative_clipped_pixels": clipped_negative,
             "median_reflectance": {b: (float(np.nanmedian(refl[i])) if not nodata.all() else None) for i, b in enumerate(BANDS)},
             "offset_applied": {b: float(offsets.get(b, 0)) for b in BANDS}, "scale": scale}
    return Standardised(refl, scl, nodata, saturated, stats)


def to_uint16(refl: np.ndarray) -> np.ndarray:
    """Storage encoding: reflectance x 10000 as uint16, 0 = no-data."""
    out = np.where(np.isnan(refl), 0, np.clip(np.round(refl * 10000.0), 1, 65534)).astype(np.uint16)
    return out


def from_uint16(a: np.ndarray) -> np.ndarray:
    r = a.astype(np.float32) / 10000.0
    r[a == 0] = np.nan
    return r
