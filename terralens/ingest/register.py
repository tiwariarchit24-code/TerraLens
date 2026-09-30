"""Ingestion: detect -> validate (Gate 0) -> de-duplicate -> register or quarantine.

Raw inputs are moved from the watch folder into data/raw/ and made read-only; they are
never modified afterwards. Every outcome (registered, duplicate, superseded,
quarantined) is written to the audit log.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
from pathlib import Path

from .. import audit, provenance
from ..aoi import aoi_for_footprint
from ..config import path, settings
from ..db import J, conn, q, q1
from ..hashing import sha256_file, sha256_json
from ..jobs.queue import enqueue
from ..storage import make_read_only, project_relative, safe_name
from .adapters import ValidationFailure, detect

log = logging.getLogger("terralens.ingest")


def _candidates(folder: Path) -> list[Path]:
    """Scene candidates in the watch folder: clip folders, or GeoTIFFs (sidecars are not scenes)."""
    out = []
    for p in sorted(folder.iterdir()):
        if p.name.startswith(".") or p.name.endswith(".terralens.json") or p.name.endswith(".part") or p.name.endswith(".part.tif"):
            continue
        out.append(p)
    return out


def quarantine(p: Path, code: str, reasons: list[str], actor: str = "ingest") -> int:
    qdir = path("quarantine") / f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}_{safe_name(p.name)}"
    qdir.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in [p] + ([p.with_name(p.stem + ".terralens.json")] if p.is_file() else []):
        if f.exists():
            shutil.move(str(f), qdir / f.name)
            moved.append(f.name)
    (qdir / "QUARANTINE_REASON.json").write_text(json.dumps({"code": code, "reasons": reasons, "files": moved}, indent=1))
    sha = None
    with conn() as c:
        r = c.execute("INSERT INTO quarantine (original_name, stored_path, sha256, reason_code, reasons) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                      (p.name, project_relative(qdir), sha, code, J(reasons))).fetchone()
        audit.append("ingest.quarantined", "quarantine", r["id"], {"name": p.name, "code": code, "reasons": reasons}, actor=actor, role="system", c=c)
    log.warning("quarantined %s: %s %s", p.name, code, reasons)
    return r["id"]


def register_one(p: Path, actor: str = "ingest", overrides: dict | None = None) -> dict:
    adapter = detect(p)
    if adapter is None:
        qid = quarantine(p, "unsupported_format", ["no adapter recognises this input (supported: Sentinel-2 L2A clip folder, GeoTIFF/COG + .terralens.json sidecar)"], actor)
        return {"status": "quarantined", "quarantine_id": qid}
    try:
        meta = adapter.read(p, overrides) if overrides else adapter.read(p)
    except ValidationFailure as e:
        qid = quarantine(p, e.code, e.reasons, actor)
        return {"status": "quarantined", "quarantine_id": qid, "code": e.code, "reasons": e.reasons}

    # ---- sensor / level policy (Gate 0): never mix levels in one archive
    sens = settings()["sensors"].get(meta.sensor)
    if sens is None and meta.sensor != "sentinel-2":
        qid = quarantine(p, "sensor_unknown", [f"sensor {meta.sensor!r} is not configured"], actor)
        return {"status": "quarantined", "quarantine_id": qid}
    if sens and meta.processing_level not in sens["levels"]:
        qid = quarantine(p, "processing_level_mismatch", [f"level {meta.processing_level} is not accepted for {meta.sensor} (archive holds {sens['levels']}); top-of-atmosphere and surface reflectance are never mixed"], actor)
        return {"status": "quarantined", "quarantine_id": qid}

    file_hashes = {f.name: sha256_file(f) for f in meta.files}
    raw_sha = sha256_json(sorted(file_hashes.items()))

    # ---- AOI
    staged_aoi = meta.metadata.get("staging", {}).get("aoi")
    aois = aoi_for_footprint(meta.footprint_wkt)
    if staged_aoi and staged_aoi in aois:
        aois = [staged_aoi]
    if not aois:
        qid = quarantine(p, "outside_aoi", ["scene footprint does not intersect any configured AOI"], actor)
        return {"status": "quarantined", "quarantine_id": qid}
    aoi_id = aois[0] if len(aois) == 1 else None

    # ---- duplicates
    dup = q1("SELECT id, source_id FROM scenes WHERE raw_sha256=%s", (raw_sha,))
    if dup:
        qid = quarantine(p, "duplicate", [f"identical content already registered as scene {dup['id']} ({dup['source_id']})"], actor)
        return {"status": "duplicate", "scene_id": dup["id"], "quarantine_id": qid}
    dup = q1("SELECT id, source_id FROM scenes WHERE scene_uid=%s AND aoi_id IS NOT DISTINCT FROM %s", (meta.scene_uid(), aoi_id))
    if dup:
        qid = quarantine(p, "duplicate", [f"same acquisition and processing already registered as scene {dup['id']} ({dup['source_id']})"], actor)
        return {"status": "duplicate", "scene_id": dup["id"], "quarantine_id": qid}
    same_acq = q("SELECT id, processing_baseline, status FROM scenes WHERE acquisition_key=%s AND aoi_id IS NOT DISTINCT FROM %s AND status <> 'superseded'",
                 (meta.acquisition_key(), aoi_id))
    supersede: list[int] = []
    for s in same_acq:
        if (meta.processing_baseline or "") > (s["processing_baseline"] or ""):
            supersede.append(s["id"])
        else:
            qid = quarantine(p, "superseded_duplicate", [f"acquisition already registered with processing baseline {s['processing_baseline']} (this file: {meta.processing_baseline})"], actor)
            return {"status": "duplicate", "scene_id": s["id"], "quarantine_id": qid}

    # ---- move into the read-only raw store
    dest = path("raw") / meta.sensor / (aoi_id or "multi") / f"{safe_name(meta.source_id)}__{raw_sha[:8]}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if p.is_dir():
        shutil.move(str(p), dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        for f in meta.files:
            shutil.move(str(f), dest / f.name)
    make_read_only(dest)

    with conn() as c:
        row = c.execute(
            """INSERT INTO scenes (scene_uid, acquisition_key, source_id, adapter, aoi_id, sensor, platform, instrument,
                 processing_level, processing_baseline, acquired_at, gsd_m, crs, footprint, sun_azimuth, sun_elevation,
                 view_azimuth, view_zenith, cloud_cover_scene, radiometric, raw_path, raw_sha256, file_hashes, metadata,
                 synthetic, attribution, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, ST_GeomFromText(%s,4326), %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'registered')
               RETURNING id""",
            (meta.scene_uid(), meta.acquisition_key(), meta.source_id, meta.adapter, aoi_id, meta.sensor, meta.platform,
             meta.instrument, meta.processing_level, meta.processing_baseline, meta.acquired_at, meta.gsd_m, meta.crs,
             meta.footprint_wkt, meta.sun_azimuth, meta.sun_elevation, meta.view_azimuth, meta.view_zenith,
             meta.cloud_cover, J(meta.radiometric), project_relative(dest), raw_sha, J(file_hashes),
             J(meta.metadata), meta.synthetic, meta.attribution)).fetchone()
        sid = row["id"]
        for old in supersede:
            c.execute("UPDATE scenes SET status='superseded', status_detail=%s WHERE id=%s", (f"superseded by scene {sid} (newer processing baseline)", old))
            c.execute("UPDATE observations SET usable=false, unusable_reason='superseded by reprocessed scene' WHERE scene_id=%s", (old,))
        provenance.record("scene", sid, "register", [{"type": "raw_file", "id": n, "sha256": h} for n, h in file_hashes.items()],
                          params={"adapter": meta.adapter, "radiometric": meta.radiometric, "aois": aois}, c=c,
                          artifact_sha256=raw_sha)
        audit.append("ingest.registered", "scene", sid, {"source_id": meta.source_id, "raw_sha256": raw_sha, "aois": aois,
                                                          "acquired_at": meta.acquired_at.isoformat(), "supersedes": supersede,
                                                          "synthetic": meta.synthetic}, actor=actor, role="system", c=c)
        for a in aois:
            enqueue("process_scene", {"scene_id": sid, "aoi_id": a}, dedupe_key=f"process:{sid}:{a}", c=c)
    log.info("registered scene %s (%s) -> %s", sid, meta.source_id, aois)
    return {"status": "registered", "scene_id": sid, "aois": aois, "supersedes": supersede}


def scan(folder: Path | None = None, actor: str = "ingest") -> list[dict]:
    folder = folder or path("incoming")
    results = []
    for p in _candidates(folder):
        try:
            r = register_one(p, actor)
        except Exception as e:  # one bad input never stops the scan
            log.exception("ingest of %s failed", p)
            r = {"status": "error", "error": str(e)}
        r["input"] = p.name
        results.append(r)
    return results


def release_quarantine(qid: int, actor: str, role: str, reason: str, overrides: dict | None = None) -> dict:
    """Admin decision: move a quarantined input back and register it, optionally with an
    explicit override (e.g. the radiometric offset). The override, the evidence and the
    reason are written to the scene record and the audit log."""
    if role != "admin":
        raise PermissionError("only an administrator can release quarantined inputs")
    row = q1("SELECT * FROM quarantine WHERE id=%s AND NOT released", (qid,))
    if not row:
        raise ValueError(f"quarantine entry {qid} not found or already released")
    from ..config import ROOT
    qdir = ROOT / row["stored_path"]
    items = [x for x in qdir.iterdir() if x.name != "QUARANTINE_REASON.json" and not x.name.endswith(".terralens.json")]
    if len(items) != 1:
        raise ValueError("quarantine folder does not contain exactly one input")
    staging = ROOT / "var" / "release"
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / items[0].name
    shutil.move(str(items[0]), target)
    for side in qdir.glob("*.terralens.json"):
        shutil.move(str(side), staging / side.name)
    ov = dict(overrides or {})
    if ov:
        ov.setdefault("radiometric", {})
        ov["radiometric"].update({"decided_by": actor, "reason": reason, "quarantine_id": qid})
    res = register_one(target, actor=actor, overrides=ov or None)
    with conn() as c:
        c.execute("UPDATE quarantine SET released=true WHERE id=%s", (qid,))
        audit.append("ingest.quarantine_released", "quarantine", qid,
                     {"reason": reason, "overrides": ov, "result": res.get("status"), "scene_id": res.get("scene_id"),
                      "original_code": row["reason_code"], "original_reasons": row["reasons"]}, actor=actor, role=role, c=c)
    return res
