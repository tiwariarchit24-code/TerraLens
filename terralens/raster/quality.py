"""Gate 1: per-pixel quality mask.

Bit flags (uint8). A pixel is usable only when the mask is 0.
    1 NODATA      SCL 0 or DN 0
    2 SATURATED   SCL 1 or DN at the saturation value
    4 CLOUD       SCL 8/9, dilated
    8 HAZE        SCL 10 (thin cirrus) or HOT haze test
   16 SHADOW      SCL 3, plus cloud mask projected along the sun azimuth over a range of
                  cloud heights, intersected with dark NIR; dilated
   32 SNOW        SCL 11, or NDSI > t and bright green (brightness separates snow from water)
   64 TERRAIN     DEM illumination cos(i) below threshold (or SCL 2 on steep slopes)
  128 EDGE        set by co-registration: within 1 px of strong edges when residual is high
"""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

from .indices import IDX, mndwi, ndsi

NODATA, SATURATED, CLOUD, HAZE, SHADOW, SNOW, TERRAIN, EDGE = 1, 2, 4, 8, 16, 32, 64, 128
FLAG_NAMES = {NODATA: "nodata", SATURATED: "saturated", CLOUD: "cloud", HAZE: "haze", SHADOW: "cloud_shadow",
              SNOW: "snow", TERRAIN: "terrain_shadow", EDGE: "registration_edge"}


def _dilate(m: np.ndarray, px: int) -> np.ndarray:
    if px <= 0 or not m.any():
        return m
    return ndimage.binary_dilation(m, structure=np.ones((3, 3), bool), iterations=px)


def project_shadow(cloud: np.ndarray, sun_azimuth: float, sun_elevation: float, gsd: float,
                   heights_m: list[float]) -> np.ndarray:
    """Where cloud shadows would fall for the given cloud heights.

    A cloud at height h casts its shadow h * tan(sun zenith) away from the sun, i.e. in the
    direction azimuth + 180 deg. Rows increase southwards and columns eastwards, so the
    displacement is d_row = +cos(az) * d and d_col = -sin(az) * d (in pixels).
    """
    out = np.zeros_like(cloud, dtype=bool)
    if not cloud.any() or sun_elevation is None or sun_azimuth is None or sun_elevation <= 0:
        return out
    zen = math.radians(90.0 - float(sun_elevation))
    az = math.radians(float(sun_azimuth))
    H, W = cloud.shape
    for h in heights_m:
        d = h * math.tan(zen) / gsd
        dr, dc = int(round(math.cos(az) * d)), int(round(-math.sin(az) * d))
        shifted = np.zeros_like(out)
        r0, r1 = max(0, dr), min(H, H + dr)
        c0, c1 = max(0, dc), min(W, W + dc)
        if r1 > r0 and c1 > c0:
            shifted[r0:r1, c0:c1] = cloud[r0 - dr:r1 - dr, c0 - dc:c1 - dc]
        out |= shifted
    return out


def terrain_illumination(dem: np.ndarray, gsd: float, sun_azimuth: float, sun_elevation: float) -> tuple[np.ndarray, np.ndarray]:
    """cos(i) = cos(slope) cos(zenith) + sin(slope) sin(zenith) cos(azimuth - aspect)."""
    gy, gx = np.gradient(dem.astype(np.float64), gsd)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)  # clockwise from north: downslope direction
    zen = math.radians(90.0 - sun_elevation)
    az = math.radians(sun_azimuth)
    cos_i = np.cos(slope) * math.cos(zen) + np.sin(slope) * math.sin(zen) * np.cos(az - aspect)
    return cos_i.astype(np.float32), np.degrees(slope).astype(np.float32)


def quality_mask(refl: np.ndarray, scl: np.ndarray | None, nodata: np.ndarray, saturated: np.ndarray,
                 sun_azimuth: float | None, sun_elevation: float | None, gsd: float, qt: dict,
                 dem: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    H, W = nodata.shape
    m = np.zeros((H, W), np.uint8)
    valid = ~nodata
    if scl is not None:
        nodata = nodata | (scl == 0)
        saturated = saturated | (scl == 1)
    m[nodata] |= NODATA
    m[saturated] |= SATURATED

    cloud = np.zeros((H, W), bool) if scl is None else np.isin(scl, (8, 9))
    cloud = _dilate(cloud, int(qt["cloud_dilate_px"])) & valid
    m[cloud] |= CLOUD

    # haze: SCL thin cirrus + haze-optimised transform relative to the scene's clear pixels
    blue, red, nir, green = refl[IDX["B02"]], refl[IDX["B04"]], refl[IDX["B08"]], refl[IDX["B03"]]
    hot = blue - 0.5 * red - 0.08
    haze = np.zeros((H, W), bool) if scl is None else (scl == 10)
    clear = valid & ~cloud & ~haze & ~np.isnan(hot)
    if scl is not None:
        clear &= np.isin(scl, (4, 5))           # vegetation / not-vegetated land for the reference
    hot_med = hot_mad = None
    if clear.sum() > 500:
        hv = hot[clear]
        hot_med = float(np.median(hv))
        hot_mad = float(np.median(np.abs(hv - hot_med))) * 1.4826
        wat = mndwi(refl) > 0.1
        haze |= valid & ~wat & (hot > hot_med + qt["haze_hot_mad_k"] * max(hot_mad, 1e-3)) & (hot > qt["haze_min_hot"])
    haze &= ~cloud
    m[haze] |= HAZE

    shadow = np.zeros((H, W), bool) if scl is None else (scl == 3)
    proj = project_shadow(cloud, sun_azimuth, sun_elevation, gsd, qt["shadow_heights_m"])
    shadow |= proj & (np.nan_to_num(nir, nan=1.0) < qt["shadow_dark_nir"]) & (mndwi(refl) < 0.2)
    shadow = _dilate(shadow, int(qt["shadow_dilate_px"])) & valid & ~cloud
    m[shadow] |= SHADOW

    snow = np.zeros((H, W), bool) if scl is None else (scl == 11)
    snow |= (ndsi(refl) > qt["snow_ndsi"]) & (np.nan_to_num(green) > qt["snow_min_green"])
    snow &= valid
    m[snow] |= SNOW

    terrain = np.zeros((H, W), bool)
    cos_i_min = None
    if dem is not None and sun_elevation is not None and sun_azimuth is not None:
        cos_i, slope = terrain_illumination(dem, gsd, sun_azimuth, sun_elevation)
        terrain |= cos_i < qt["terrain_min_cos_i"]
        if scl is not None:
            terrain |= (scl == 2) & (slope > 10.0)
        cos_i_min = float(np.nanmin(cos_i))
    terrain &= valid
    m[terrain] |= TERRAIN

    n = float(H * W)
    stats = {
        "usable_fraction": float((m == 0).sum() / n),
        "nodata_fraction": float(nodata.sum() / n),
        "saturation_fraction": float(saturated.sum() / n),
        "cloud_fraction": float(cloud.sum() / n),
        "haze_fraction": float(haze.sum() / n),
        "shadow_fraction": float(shadow.sum() / n),
        "projected_shadow_px": int(proj.sum()),
        "snow_fraction": float(snow.sum() / n),
        "terrain_fraction": float(terrain.sum() / n),
        "hot_clear_median": hot_med, "hot_clear_mad": hot_mad,
        "haze_score": float(haze.sum() / max(valid.sum(), 1)),
        "dem_used": dem is not None, "min_cos_i": cos_i_min,
    }
    return m, stats
