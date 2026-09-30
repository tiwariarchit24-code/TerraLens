"""Gate 2 support: sub-pixel co-registration by phase correlation of the NIR band.

The AOI is split into windows; each window with enough clear pixels in both images
yields a sub-pixel shift (skimage, upsample factor 20) and a correlation-peak ratio.
The global shift is the median over windows, the residual is the robust spread of the
window shifts around it (what a global correction cannot remove). Decision:
  |shift| > max_correctable or weak peak or no window -> 'unusable' for change
  |shift| >= min_correct                              -> 'corrected' (bands + masks shifted)
  otherwise                                           -> 'ok'
Residual above residual_edge_suppress_px -> pixels within edge_band_px of strong
reference edges are flagged EDGE, so thin slivers along edges cannot become change.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage
from skimage.registration import phase_cross_correlation


def peak_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """Normalised phase-correlation surface: peak height over mean + 3 std of the rest."""
    fa, fb = np.fft.fft2(a), np.fft.fft2(b)
    cp = fa * np.conj(fb)
    cp /= np.abs(cp) + 1e-12
    r = np.abs(np.fft.ifft2(cp))
    pk = r.max()
    rest = r[r < pk]
    return float(pk / (rest.mean() + 3 * rest.std() + 1e-12))


def estimate_shift(ref: np.ndarray, mov: np.ndarray, ref_ok: np.ndarray, mov_ok: np.ndarray,
                   win: int = 256, min_valid: float = 0.8) -> dict:
    H, W = ref.shape
    shifts, peaks = [], []
    for r in range(0, H - win + 1, win):
        for c in range(0, W - win + 1, win):
            a = ref[r:r + win, c:c + win].astype(np.float64)
            b = mov[r:r + win, c:c + win].astype(np.float64)
            ok = ref_ok[r:r + win, c:c + win] & mov_ok[r:r + win, c:c + win] & np.isfinite(a) & np.isfinite(b)
            if ok.mean() < min_valid:
                continue
            fill_a, fill_b = np.nanmean(a[ok]), np.nanmean(b[ok])
            a[~ok] = fill_a
            b[~ok] = fill_b
            a, b = a - a.mean(), b - b.mean()
            hann = np.outer(np.hanning(win), np.hanning(win))
            a, b = a * hann, b * hann
            s, _err, _ph = phase_cross_correlation(a, b, upsample_factor=20, normalization=None)
            shifts.append(s)
            peaks.append(peak_ratio(a, b))
    if not shifts:
        return {"n_windows": 0, "shift": None, "residual": None, "peak_ratio": None}
    S = np.array(shifts)
    med = np.median(S, axis=0)
    resid = float(np.sqrt(np.median(np.sum((S - med) ** 2, axis=1)))) if len(S) > 1 else 0.0
    return {"n_windows": len(S), "shift": [float(med[0]), float(med[1])], "residual": resid,
            "peak_ratio": float(np.median(peaks)), "window_shifts": S.round(3).tolist()}


def decide(est: dict, rt: dict) -> tuple[str, str | None]:
    if est["n_windows"] == 0:
        return "suspect", "no window with enough clear pixels in both images to measure alignment"
    mag = float(np.hypot(*est["shift"]))
    if mag > rt["max_correctable_px"]:
        return "unusable", f"shift {mag:.2f} px exceeds {rt['max_correctable_px']} px"
    if est["peak_ratio"] < rt["min_peak_ratio"]:
        return "unusable", f"weak correlation peak (ratio {est['peak_ratio']:.1f} < {rt['min_peak_ratio']})"
    if mag >= rt["min_correct_px"]:
        return "corrected", None
    return "ok", None


def apply_shift(arr: np.ndarray, shift: list[float], order: int = 1, cval=np.nan) -> np.ndarray:
    """Shift (…, H, W) by (dy, dx) pixels. order=1 for reflectance, 0 for masks/classes."""
    if arr.ndim == 2:
        return ndimage.shift(arr, shift, order=order, mode="constant", cval=cval)
    return np.stack([ndimage.shift(a, shift, order=order, mode="constant", cval=cval) for a in arr])


def strong_edges(ref_nir: np.ndarray, band_px: int, pct: float = 90.0) -> np.ndarray:
    a = np.nan_to_num(ref_nir, nan=float(np.nanmedian(ref_nir)))
    g = np.hypot(ndimage.sobel(a, 0), ndimage.sobel(a, 1))
    e = g > np.percentile(g, pct)
    return ndimage.binary_dilation(e, iterations=max(int(band_px), 0)) if band_px > 0 else e
