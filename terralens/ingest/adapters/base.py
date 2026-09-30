"""SensorAdapter interface. An adapter turns one raw input (a file or a folder) into
validated SceneMeta plus the list of band sources. Adapters never guess: missing or
ambiguous metadata raises ValidationFailure, and the input is quarantined."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ValidationFailure(Exception):
    def __init__(self, code: str, reasons: list[str]):
        super().__init__(f"{code}: {'; '.join(reasons)}")
        self.code = code
        self.reasons = reasons


@dataclass
class BandSource:
    band: str
    path: Path
    band_index: int = 1
    native_gsd: float = 10.0
    kind: str = "reflectance"          # reflectance | scl | mask


@dataclass
class SceneMeta:
    source_id: str
    adapter: str
    sensor: str
    platform: str
    instrument: str | None
    processing_level: str
    processing_baseline: str | None
    acquired_at: dt.datetime
    gsd_m: float
    crs: str
    footprint_wkt: str | None
    tile_code: str | None
    sun_azimuth: float | None = None
    sun_elevation: float | None = None
    view_azimuth: float | None = None
    view_zenith: float | None = None
    cloud_cover: float | None = None
    radiometric: dict[str, Any] = field(default_factory=dict)   # scale, per-band offsets, source of truth
    bands: list[BandSource] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    synthetic: bool = False
    attribution: str | None = None

    def scene_uid(self) -> str:
        t = self.acquired_at.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{self.platform}|{t}|{self.tile_code or '-'}|{self.processing_baseline or '-'}|{self.processing_level}"

    def acquisition_key(self) -> str:
        t = self.acquired_at.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M")
        return f"{self.platform}|{t}"


class SensorAdapter(Protocol):
    name: str

    def detect(self, path: Path) -> bool: ...

    def read(self, path: Path) -> SceneMeta: ...
