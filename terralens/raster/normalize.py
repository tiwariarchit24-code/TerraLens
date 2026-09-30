"""Gate 3 support: relative radiometric normalisation on pseudo-invariant pixels.

Used only to compute change magnitude and the normalisation residual; stored
reflectance is never overwritten. Per band, a robust (Theil-Sen) line maps this
observation to the AOI reference observation, fitted on pixels that are usable in both
and spectrally stable (small NDVI / NDBI / MNDWI differences).
"""
from __future__ import annotations

import numpy as np
from scipy.stats import theilslopes

from .indices import BANDS, mndwi, ndbi, ndvi


def fit(ref: np.ndarray, obs: np.ndarray, ok: np.ndarray, max_px: int = 1500, seed: int = 7) -> dict:
    stable = ok & (np.abs(ndvi(ref) - ndvi(obs)) < 0.05) & (np.abs(ndbi(ref) - ndbi(obs)) < 0.05) \
        & (np.abs(mndwi(ref) - mndwi(obs)) < 0.05)
    idx = np.flatnonzero(stable.ravel())
    if idx.size < 200:
        return {"n_pif": int(idx.size), "status": "insufficient_invariant_pixels", "bands": {}}
    rng = np.random.default_rng(seed)
    if idx.size > max_px:
        idx = rng.choice(idx, max_px, replace=False)
    out = {}
    rmses = []
    for i, b in enumerate(BANDS):
        x = ref[i].ravel()[idx]
        y = obs[i].ravel()[idx]
        slope, inter, _, _ = theilslopes(x, y)       # maps obs -> ref
        pred = slope * y + inter
        rmse = float(np.sqrt(np.mean((pred - x) ** 2)))
        rmses.append(rmse)
        out[b] = {"gain": float(slope), "offset": float(inter), "rmse": rmse}
    return {"n_pif": int(stable.sum()), "status": "ok", "bands": out, "rmse_max": float(max(rmses))}


def apply(obs: np.ndarray, params: dict) -> np.ndarray:
    if not params or params.get("status") != "ok":
        return obs
    out = np.empty_like(obs)
    for i, b in enumerate(BANDS):
        p = params["bands"][b]
        out[i] = obs[i] * p["gain"] + p["offset"]
    return out
