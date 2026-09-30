from pathlib import Path

from .base import BandSource, SceneMeta, SensorAdapter, ValidationFailure
from .generic_geotiff import GenericGeoTiffAdapter
from .sentinel2_clip import Sentinel2ClipAdapter

ADAPTERS: list = [Sentinel2ClipAdapter(), GenericGeoTiffAdapter()]


def detect(path: Path):
    for a in ADAPTERS:
        if a.detect(path):
            return a
    return None


def by_name(name: str):
    for a in ADAPTERS:
        if a.name == name:
            return a
    raise KeyError(name)


__all__ = ["ADAPTERS", "detect", "by_name", "BandSource", "SceneMeta", "SensorAdapter", "ValidationFailure"]
