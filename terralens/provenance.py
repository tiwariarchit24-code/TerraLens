"""Provenance records: every generated artefact links its inputs (with hashes), the
code version, pipeline/model versions, configuration hash, threshold set and
parameters. Exports embed these rows so provenance survives leaving the system."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import psycopg

from .config import config_hash, settings
from .db import J, conn, q
from .hashing import sha256_file
from .storage import project_relative
from .version import PIPELINE_VERSION, code_version


def record(artifact_type: str, artifact_id: Any, activity: str, inputs: list[dict],
           artifact_path: Path | None = None, model_ids: list[str] | None = None,
           params: dict | None = None, threshold_set: str | None = None,
           started_at: dt.datetime | None = None, c: psycopg.Connection | None = None,
           artifact_sha256: str | None = None) -> int:
    sha = artifact_sha256
    if artifact_path is not None and sha is None and Path(artifact_path).exists():
        sha = sha256_file(artifact_path)
    row = (
        artifact_type, str(artifact_id), project_relative(Path(artifact_path)) if artifact_path else None, sha,
        activity, J(inputs), code_version(), PIPELINE_VERSION, J(model_ids or []),
        f"{settings()['config_version']}#{config_hash()}", threshold_set, J(params or {}),
        started_at, dt.datetime.now(dt.timezone.utc),
    )
    sql = ("INSERT INTO provenance (artifact_type, artifact_id, artifact_path, artifact_sha256, activity, inputs, "
           "code_version, pipeline_version, model_ids, config_version, threshold_set, params, started_at, ended_at) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id")
    if c is not None:
        return c.execute(sql, row).fetchone()["id"]
    with conn() as cx:
        return cx.execute(sql, row).fetchone()["id"]


def for_artifact(artifact_type: str, artifact_id: Any) -> list[dict]:
    return q("SELECT * FROM provenance WHERE artifact_type=%s AND artifact_id=%s ORDER BY id",
             (artifact_type, str(artifact_id)))


def lineage_for_event(event_id: int) -> dict:
    """Walk event -> engine run -> observations -> scenes, collecting provenance rows."""
    ev = q("SELECT id, aoi_id, latest_candidate_id FROM change_events WHERE id=%s", (event_id,))
    if not ev:
        return {}
    obs = q("""SELECT o.id, o.scene_id, s.source_id, s.raw_sha256, o.cog_sha256, o.mask_sha256, o.classprob_sha256,
                      o.acquired_at, s.processing_baseline, s.radiometric
               FROM event_observations eo JOIN observations o ON o.id=eo.observation_id
               JOIN scenes s ON s.id=o.scene_id WHERE eo.event_id=%s ORDER BY o.acquired_at""", (event_id,))
    prov = q("""SELECT * FROM provenance WHERE (artifact_type='change_event' AND artifact_id=%s)
                OR (artifact_type IN ('observation','scene') AND artifact_id = ANY(%s)) ORDER BY id""",
             (str(event_id), [str(o["id"]) for o in obs] + [str(o["scene_id"]) for o in obs]))
    return {"observations": obs, "provenance": prov}
