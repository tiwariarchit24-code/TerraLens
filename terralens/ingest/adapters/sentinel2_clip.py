"""Adapter for Sentinel-2 L2A clips: a folder with per-band GeoTIFF/COG files at native
resolution plus the STAC item (item.json) as the metadata sidecar.

Radiometric offset rule (applied exactly once, by the standardiser, never here):
  * Processing baseline < 04.00: L2A reflectance = DN / 10000, no offset exists.
  * Baseline >= 04.00 (ESA products from 25 Jan 2022, and reprocessed Collection-1
    products): the product carries BOA_ADD_OFFSET = -1000, reflectance = (DN - 1000) / 10000.
  * Some distributors (Earth Search) already subtract the offset from the COGs; they say so
    with `earthsearch:boa_offset_applied: true`. Then the offset we apply is 0.
  * If the baseline is >= 04.00 and the distributor flag is missing, the scene is
    AMBIGUOUS and is quarantined: we never guess whether an offset was applied.
If a product MTD_MSIL2A.xml is present its BOA_ADD_OFFSET values take precedence over the
baseline rule for the size of the offset.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import numpy as np
import rasterio
from shapely.geometry import shape

from .base import BandSource, SceneMeta, ValidationFailure

BAND_FILES = {"B02": 10, "B03": 10, "B04": 10, "B08": 10, "B11": 20, "B12": 20}
# band_id order used by BOA_ADD_OFFSET in the L2A product metadata
_MTD_BAND_IDS = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B10", "B11", "B12"]
S2_LAUNCH = dt.datetime(2015, 6, 23, tzinfo=dt.timezone.utc)
OFFSET_BASELINE = (4, 0)


def _baseline_tuple(b: str | None) -> tuple[int, int] | None:
    if not b:
        return None
    m = re.match(r"^\s*(\d+)\.(\d+)\s*$", str(b))
    return (int(m.group(1)), int(m.group(2))) if m else None


def offset_decision(baseline: str | None, provider_applied: bool | None, mtd_offsets: dict[str, int] | None) -> dict:
    """Pure function (unit-tested across the 25 Jan 2022 transition)."""
    bt = _baseline_tuple(baseline)
    if bt is None:
        raise ValidationFailure("radiometry_ambiguous", [f"processing baseline missing or unparseable: {baseline!r}"])
    product_has_offset = bt >= OFFSET_BASELINE
    size = {b: -1000 for b in BAND_FILES} if product_has_offset else {b: 0 for b in BAND_FILES}
    source = "baseline rule (>= 04.00 carries BOA_ADD_OFFSET = -1000)" if product_has_offset else "baseline rule (< 04.00 has no offset)"
    if mtd_offsets:
        size = {b: int(mtd_offsets.get(b, size[b])) for b in BAND_FILES}
        source = "MTD_MSIL2A.xml BOA_ADD_OFFSET"
        product_has_offset = any(v != 0 for v in size.values())
    if not product_has_offset:
        to_apply = {b: 0 for b in BAND_FILES}
        provider = bool(provider_applied) if provider_applied is not None else None
    else:
        if provider_applied is None:
            raise ValidationFailure("radiometry_ambiguous", [
                f"baseline {baseline} carries a BOA offset but the distributor does not state whether it was already applied"])
        to_apply = {b: 0 for b in BAND_FILES} if provider_applied else dict(size)
        provider = bool(provider_applied)
    return {"scale": 10000, "product_offset": size, "product_has_offset": product_has_offset,
            "provider_applied": provider, "offset_to_apply": to_apply, "offset_source": source,
            "formula": "reflectance = (DN + offset_to_apply) / scale; DN == 0 is no-data"}


def dark_pixel_check(path: Path, radiometric: dict, tol_dn: int = 50) -> dict:
    """Data-consistency check for the offset decision. Surface reflectance of the darkest
    real surfaces (deep water in NIR/SWIR) is small but positive; if the DNs still
    contained a +1000 offset, no valid pixel could fall below ~1000 DN."""
    off = min(int(v) for v in radiometric["offset_to_apply"].values())
    vals = []
    for b in ("B08", "B11", "B12"):
        with rasterio.open(path / f"{b}.tif") as ds:
            a = ds.read(1, out_shape=(max(1, ds.height // 2), max(1, ds.width // 2)))
        vals.append(a[a > 0].ravel())
    v = np.concatenate(vals) if vals else np.array([])
    if v.size == 0:
        return {"status": "no_data", "offset": off}
    p005 = float(np.percentile(v, 0.5))
    res = {"offset": off, "p005": p005, "n": int(v.size)}
    if off < 0:
        share = float((v < (-off - tol_dn)).mean())
        res["share_below"] = share
        res["status"] = "inconsistent" if share > 0.005 else "consistent"
    else:
        res["status"] = "consistent" if p005 < 1000 else "unverified"   # no dark pixels -> cannot confirm
    return res


def _parse_mtd(p: Path) -> dict[str, int] | None:
    if not p.exists():
        return None
    txt = p.read_text(errors="ignore")
    vals = re.findall(r'<BOA_ADD_OFFSET band_id="(\d+)">(-?\d+)</BOA_ADD_OFFSET>', txt)
    if not vals:
        return None
    return {_MTD_BAND_IDS[int(i)]: int(v) for i, v in vals if int(i) < len(_MTD_BAND_IDS)}


class Sentinel2ClipAdapter:
    name = "sentinel2-l2a-clip"

    def detect(self, path: Path) -> bool:
        return path.is_dir() and (path / "item.json").exists() and (path / "B04.tif").exists()

    def read(self, path: Path, overrides: dict | None = None) -> SceneMeta:
        reasons: list[str] = []
        overrides = overrides or {}
        try:
            item = json.loads((path / "item.json").read_text())
        except Exception as e:
            raise ValidationFailure("metadata_unreadable", [f"item.json: {e}"])
        p = item.get("properties", {})
        # --- acquisition time
        try:
            acq = dt.datetime.fromisoformat(p["datetime"].replace("Z", "+00:00"))
            if acq.tzinfo is None:
                raise ValueError("timestamp has no time zone")
            acq = acq.astimezone(dt.timezone.utc)
        except Exception as e:
            raise ValidationFailure("timestamp_invalid", [f"datetime: {e}"])
        if acq < S2_LAUNCH or acq > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1):
            reasons.append(f"acquisition time {acq.isoformat()} outside the Sentinel-2 mission period")
        # --- sensor / level
        platform = str(p.get("platform", "")).lower()
        if not platform.startswith("sentinel-2"):
            reasons.append(f"platform {platform!r} is not Sentinel-2")
        instruments = [str(i).lower() for i in p.get("instruments", [])]
        if "msi" not in instruments:
            reasons.append("instrument is not MSI")
        level = "L2A" if "L2A" in item.get("id", "") or item.get("collection", "").endswith("l2a") else None
        if level is None:
            reasons.append("processing level could not be established as L2A (L1C is not mixed into an L2A archive)")
        # --- CRS
        epsg = p.get("proj:epsg") or p.get("proj:code")
        crs = f"EPSG:{epsg}" if isinstance(epsg, int) else (epsg if isinstance(epsg, str) else None)
        if not crs:
            reasons.append("projection (proj:epsg) missing")
        # --- files and dimensions
        bands, files = [], []
        shapes = {}
        for b, gsd in BAND_FILES.items():
            f = path / f"{b}.tif"
            if not f.exists():
                reasons.append(f"missing band file {b}.tif")
                continue
            try:
                with rasterio.open(f) as ds:
                    shapes[b] = (ds.height, ds.width, abs(ds.transform.a), ds.crs.to_string() if ds.crs else None, ds.dtypes[0])
            except Exception as e:
                reasons.append(f"{b}.tif unreadable: {e}")
                continue
            bands.append(BandSource(b, f, 1, float(gsd)))
            files.append(f)
        scl = path / "SCL.tif"
        if scl.exists():
            bands.append(BandSource("SCL", scl, 1, 20.0, kind="scl"))
            files.append(scl)
        else:
            reasons.append("missing SCL.tif (scene classification is required for quality masking)")
        for b, (h, w, res, fcrs, dtype) in shapes.items():
            if h < 16 or w < 16:
                reasons.append(f"{b} too small: {w}x{h}")
            if abs(res - BAND_FILES[b]) > 0.01:
                reasons.append(f"{b} resolution {res} m, expected {BAND_FILES[b]} m")
            if crs and fcrs and fcrs != crs:
                reasons.append(f"{b} CRS {fcrs} disagrees with item {crs}")
            if dtype != "uint16":
                reasons.append(f"{b} dtype {dtype}, expected uint16 DN")
        if "B04" in shapes and "B11" in shapes:
            h10, w10 = shapes["B04"][:2]
            h20, w20 = shapes["B11"][:2]
            if abs(h10 - 2 * h20) > 1 or abs(w10 - 2 * w20) > 1:
                reasons.append("10 m and 20 m bands do not cover the same extent")
        if reasons:
            raise ValidationFailure("validation_failed", reasons)
        for extra in ("item.json", "MTD_TL.xml", "MTD_MSIL2A.xml"):
            if (path / extra).exists():
                files.append(path / extra)
        provider_flag = p.get("earthsearch:boa_offset_applied")
        ov = overrides.get("radiometric", {})
        if "provider_applied" in ov:
            provider_flag = bool(ov["provider_applied"])
        radiometric = offset_decision(p.get("s2:processing_baseline"), provider_flag, _parse_mtd(path / "MTD_MSIL2A.xml"))
        radiometric["dark_pixel_check"] = dark_pixel_check(path, radiometric)
        if ov:
            radiometric["override"] = {**ov, "metadata_value": p.get("earthsearch:boa_offset_applied")}
        if radiometric["dark_pixel_check"]["status"] == "inconsistent" and not ov:
            chk = radiometric["dark_pixel_check"]
            raise ValidationFailure("radiometry_inconsistent", [
                f"metadata implies subtracting {-chk['offset']} DN, but {chk['share_below']:.1%} of valid NIR/SWIR pixels "
                f"have DN below {-chk['offset']} (0.5th percentile {chk['p005']:.0f}); applying the offset would give "
                "negative reflectance, so the DNs cannot still contain the offset. The metadata contradicts the data; "
                "an administrator must decide (quarantine release with an explicit radiometric override)."])
        staging = item.get("terralens:staging", {})
        tile_code = f"{p.get('mgrs:utm_zone', '')}{p.get('mgrs:latitude_band', '')}{p.get('mgrs:grid_square', '')}" or None
        return SceneMeta(
            source_id=item["id"], adapter=self.name, sensor="sentinel-2", platform=platform, instrument="msi",
            processing_level="L2A", processing_baseline=p.get("s2:processing_baseline"), acquired_at=acq,
            gsd_m=10.0, crs=crs, footprint_wkt=shape(item["geometry"]).wkt if item.get("geometry") else None,
            tile_code=tile_code, sun_azimuth=p.get("view:sun_azimuth"), sun_elevation=p.get("view:sun_elevation"),
            view_azimuth=p.get("view:azimuth"), view_zenith=p.get("view:incidence_angle"),
            cloud_cover=p.get("eo:cloud_cover"), radiometric=radiometric, bands=bands, files=files,
            metadata={"stac_properties": p, "staging": staging, "relative_orbit": p.get("sat:relative_orbit")},
            synthetic=bool(item.get("terralens:synthetic", False)),
            attribution=staging.get("attribution", "Contains modified Copernicus Sentinel data"),
        )
