"""Spectral indices on surface reflectance. Band order everywhere: B02 B03 B04 B08 B11 B12."""
from __future__ import annotations

import numpy as np

BANDS = ["B02", "B03", "B04", "B08", "B11", "B12"]
IDX = {b: i for i, b in enumerate(BANDS)}


def _nd(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (a - b) / (a + b)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def ndvi(r: np.ndarray) -> np.ndarray:
    return _nd(r[IDX["B08"]], r[IDX["B04"]])


def mndwi(r: np.ndarray) -> np.ndarray:
    return _nd(r[IDX["B03"]], r[IDX["B11"]])


def ndbi(r: np.ndarray) -> np.ndarray:
    return _nd(r[IDX["B11"]], r[IDX["B08"]])


def ndsi(r: np.ndarray) -> np.ndarray:
    # identical formula to MNDWI; brightness (green) is what separates snow from water
    return mndwi(r)


def bsi(r: np.ndarray) -> np.ndarray:
    a = r[IDX["B11"]] + r[IDX["B04"]]
    b = r[IDX["B08"]] + r[IDX["B02"]]
    return _nd(a, b)


def all_indices(r: np.ndarray) -> dict[str, np.ndarray]:
    return {"ndvi": ndvi(r), "mndwi": mndwi(r), "ndbi": ndbi(r), "bsi": bsi(r)}
