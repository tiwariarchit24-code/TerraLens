"""PostgreSQL job table with SELECT ... FOR UPDATE SKIP LOCKED.

States: pending -> running -> complete | failed | retry (-> running ...) | cancelled.
Jobs are idempotent (each handler checks what already exists before doing work), carry
a dedupe key so the same work is never queued twice while active, and record a
heartbeat so a job whose worker died is picked up again after `job_stale_after_seconds`.
"""
from __future__ import annotations

import datetime as dt
import os
import socket
from typing import Any

import psycopg

from ..config import settings
from ..db import J, conn, q

PRIORITY = {"ingest_scan": 10, "register_scene": 20, "process_scene": 40, "classify_observation": 50,
            "embed_observation": 60, "change_update": 90, "watchset_rerun": 95, "cluster": 120}


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(kind: str, payload: dict, dedupe_key: str | None = None, priority: int | None = None,
            max_attempts: int = 3, parent_job_id: int | None = None, c: psycopg.Connection | None = None) -> int | None:
    """Returns the job id, or the id of the already-active job with the same dedupe key."""
    pr = priority if priority is not None else PRIORITY.get(kind, 100)
    sql = ("INSERT INTO jobs (kind, payload, dedupe_key, priority, max_attempts, parent_job_id) VALUES (%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (dedupe_key) WHERE status IN ('pending','running','retry') DO NOTHING RETURNING id")

    def _do(cx):
        r = cx.execute(sql, (kind, J(payload), dedupe_key, pr, max_attempts, parent_job_id)).fetchone()
        if r:
            return r["id"]
        if dedupe_key:
            r = cx.execute("SELECT id FROM jobs WHERE dedupe_key=%s AND status IN ('pending','running','retry')",
                           (dedupe_key,)).fetchone()
            return r["id"] if r else None
        return None

    if c is not None:
        return _do(c)
    with conn() as cx:
        return _do(cx)


def claim(kinds: list[str] | None = None) -> dict | None:
    stale = int(settings()["runtime"]["job_stale_after_seconds"])
    with conn() as c:
        # resurrect jobs whose worker stopped heart-beating
        c.execute("""UPDATE jobs SET status='retry', locked_by=NULL, error=coalesce(error,'') || ' [worker lost; resumed]'
                     WHERE status='running' AND heartbeat_at < now() - make_interval(secs => %s)""", (stale,))
        kind_filter = "AND kind = ANY(%s)" if kinds else ""
        params: list[Any] = [kinds] if kinds else []
        row = c.execute(
            f"""SELECT * FROM jobs WHERE status IN ('pending','retry') {kind_filter}
                ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1""", params).fetchone()
        if not row:
            return None
        c.execute("""UPDATE jobs SET status='running', locked_by=%s, locked_at=now(), heartbeat_at=now(),
                     started_at=coalesce(started_at, now()), attempts=attempts+1 WHERE id=%s""", (worker_id(), row["id"]))
        row["attempts"] += 1
        return row


def heartbeat(job_id: int, progress: dict | None = None) -> None:
    with conn() as c:
        c.execute("UPDATE jobs SET heartbeat_at=now(), progress=coalesce(%s, progress) WHERE id=%s",
                  (J(progress) if progress is not None else None, job_id))


def complete(job_id: int, result: dict | None = None) -> None:
    with conn() as c:
        c.execute("""UPDATE jobs SET status='complete', finished_at=now(),
                     duration_ms = extract(epoch from (now() - started_at)) * 1000, result=%s, error=NULL
                     WHERE id=%s""", (J(result or {}), job_id))


def fail(job_id: int, error: str) -> str:
    with conn() as c:
        r = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=%s", (job_id,)).fetchone()
        status = "retry" if r and r["attempts"] < r["max_attempts"] else "failed"
        c.execute("""UPDATE jobs SET status=%s, error=%s, locked_by=NULL, finished_at=now(),
                     duration_ms = extract(epoch from (now() - started_at)) * 1000 WHERE id=%s""",
                  (status, error[-4000:], job_id))
    return status


def retry(job_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE jobs SET status='retry', attempts=0, error=NULL, finished_at=NULL WHERE id=%s AND status IN ('failed','cancelled')", (job_id,))


def cancel(job_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE jobs SET status='cancelled', finished_at=now() WHERE id=%s AND status IN ('pending','retry')", (job_id,))


def summary() -> dict:
    rows = q("SELECT status, count(*) AS n FROM jobs GROUP BY status")
    out = {r["status"]: r["n"] for r in rows}
    run = q("SELECT id, kind, payload, progress, started_at FROM jobs WHERE status='running' ORDER BY started_at")
    out["running_jobs"] = run
    return out


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
