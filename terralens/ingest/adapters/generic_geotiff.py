"""Adapter for organiser-style GeoTIFF/COG scenes: one multi-band raster plus a REQUIRED
JSON sidecar `<name>.terralens.json`. Acquisition time, sensor, level, GSD and band
order cannot be inferred reliably from a bare GeoTIFF, so without the sidecar the file
is quarantined rather than guessed.

Sidecar schema (all required unless marked optional):
{
  "acquired_at": "2026-03-11T05:12:57Z",          # ISO 8601 with time zone
  "sensor": "sentinel-2", "platform": "sentinel-2a", "instrument": "msi",
  "processing_level": "L2A", "processing_baseline": "05.11",   # baseline optional for non-S2
  "gsd_m": 10,
  "bands": ["B02","B03","B04","B08","B11","B12"],  # raster band order
  "scale": 10000, "offset": 0,                      # reflectance = (DN + offset) / scale
  "offset_already_applied": true,                   # required when an offset exists
  "nodata": 0,
  "quality_band": "SCL" | null,                     # optional: extra band with SCL codes
  "sun_azimuth": 130.1, "sun_elevation": 60.2, "view_zenith": 8.0, "view_azimuth": 100.0,   # optional
  "synthetic": false, "attribution": "..."          # optional
}
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds
from shapely.geometry import box

from .base import BandSource, SceneMeta, ValidationFailure

REQUIRED = ["acquired_at", "sensor", "platform", "processing_level", "gsd_m", "bands", "scale", "offset", "nodata"]


def sidecar_for(path: Path) -> Path:
    return path.with_name(path.stem + ".terralens.json")


class GenericGeoTiffAdapter:
    name = "generic-geotiff"

    def detect(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in (".tif", ".tiff") and not path.name.endswith(".part.tif")

    def read(self, path: Path, overrides: dict | None = None) -> SceneMeta:
        sc = sidecar_for(path)
        if not sc.exists():
            raise ValidationFailure("metadata_missing", [f"required sidecar {sc.name} not found; acquisition time, sensor and band order are never guessed"])
        try:
            m = json.loads(sc.read_text())
        except Exception as e:
            raise ValidationFailure("metadata_unreadable", [f"{sc.name}: {e}"])
        reasons = [f"sidecar missing field '{k}'" for k in REQUIRED if k not in m]
        if reasons:
            raise ValidationFailure("metadata_missing", reasons)
        try:
            acq = dt.datetime.fromisoformat(str(m["acquired_at"]).replace("Z", "+00:00"))
            if acq.tzinfo is None:
                raise ValueError("no time zone")
            acq = acq.astimezone(dt.timezone.utc)
        except Exception as e:
            raise ValidationFailure("timestamp_invalid", [f"acquired_at: {e}"])
        if acq > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1):
            reasons.append("acquisition time is in the future")
        if m.get("offset", 0) != 0 and "offset_already_applied" not in m:
            reasons.append("an offset is declared but 'offset_already_applied' is not stated")
        try:
            with rasterio.open(path) as ds:
                if ds.crs is None:
                    reasons.append("raster has no CRS")
                elif not ds.crs.is_projected:
                    reasons.append("raster CRS is geographic; a projected CRS is required for metric analysis")
                if ds.count < len(m["bands"]):
                    reasons.append(f"raster has {ds.count} bands, sidecar lists {len(m['bands'])}")
                if ds.width < 16 or ds.height < 16:
                    reasons.append("raster too small")
                res = abs(ds.transform.a)
                if abs(res - float(m["gsd_m"])) > 0.05 * float(m["gsd_m"]):
                    reasons.append(f"pixel size {res} m disagrees with sidecar gsd_m {m['gsd_m']}")
                crs = ds.crs.to_string() if ds.crs else None
                fp = box(*transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)).wkt if ds.crs else None
        except rasterio.errors.RasterioIOError as e:
            raise ValidationFailure("raster_unreadable", [str(e)])
        if reasons:
            raise ValidationFailure("validation_failed", reasons)
        bands = [BandSource(b, path, i + 1, float(m["gsd_m"])) for i, b in enumerate(m["bands"])]
        if m.get("quality_band"):
            bands.append(BandSource("SCL", path, len(m["bands"]) + 1, float(m["gsd_m"]), kind="scl"))
        off = int(m.get("offset", 0))
        applied = bool(m.get("offset_already_applied", off == 0))
        radiometric = {"scale": float(m["scale"]), "product_offset": {b: off for b in m["bands"]},
                       "product_has_offset": off != 0, "provider_applied": applied,
                       "offset_to_apply": {b: (0 if applied else off) for b in m["bands"]},
                       "offset_source": f"sidecar {sc.name}",
                       "formula": "reflectance = (DN + offset_to_apply) / scale"}
        return SceneMeta(
            source_id=path.stem, adapter=self.name, sensor=str(m["sensor"]).lower(), platform=str(m["platform"]).lower(),
            instrument=m.get("instrument"), processing_level=str(m["processing_level"]).upper(),
            processing_baseline=m.get("processing_baseline"), acquired_at=acq, gsd_m=float(m["gsd_m"]), crs=crs,
            footprint_wkt=fp, tile_code=m.get("tile_code"), sun_azimuth=m.get("sun_azimuth"),
            sun_elevation=m.get("sun_elevation"), view_azimuth=m.get("view_azimuth"), view_zenith=m.get("view_zenith"),
            cloud_cover=m.get("cloud_cover"), radiometric=radiometric, bands=bands, files=[path, sc],
            metadata={"sidecar": m}, synthetic=bool(m.get("synthetic", False)), attribution=m.get("attribution"),
        )
