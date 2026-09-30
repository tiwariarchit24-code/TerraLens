"""Tamper-evident, hash-chained audit log.

record_hash = SHA-256(prev_hash || canonical_json(ts, actor, role, action, object_type,
object_id, payload)). Appends are serialised with a transaction-scoped advisory lock so
the chain never forks. `verify_chain()` recomputes every hash and reports the first
broken link, which answers "has the audit chain been altered?".
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from .db import J, conn
from .hashing import canonical_json, sha256_bytes

GENESIS = "0" * 64
_LOCK_KEY = 7_240_001


def _ts_str(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def _record_hash(prev_hash: str, ts: str, actor: str, role: str, action: str, object_type: str,
                 object_id: str | None, payload: Any) -> str:
    body = canonical_json({"ts": ts, "actor": actor, "role": role, "action": action,
                           "object_type": object_type, "object_id": object_id, "payload": payload})
    return sha256_bytes(prev_hash.encode() + body)


def append(action: str, object_type: str, object_id: Any, payload: dict | None = None,
           actor: str = "system", role: str = "system", c: psycopg.Connection | None = None) -> dict:
    payload = payload or {}
    object_id = None if object_id is None else str(object_id)

    def _do(cx: psycopg.Connection) -> dict:
        cx.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        last = cx.execute("SELECT record_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["record_hash"] if last else GENESIS
        ts = _ts_str(dt.datetime.now(dt.timezone.utc))
        # round-trip payload through JSON so what we hash is exactly what is stored
        import json
        payload_rt = json.loads(canonical_json(payload))
        h = _record_hash(prev, ts, actor, role, action, object_type, object_id, payload_rt)
        row = cx.execute(
            "INSERT INTO audit_log (ts, actor, role, action, object_type, object_id, payload, prev_hash, record_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING seq, record_hash",
            (ts, actor, role, action, object_type, object_id, J(payload_rt), prev, h),
        ).fetchone()
        return {"seq": row["seq"], "record_hash": row["record_hash"]}

    if c is not None:
        return _do(c)
    with conn() as cx:
        return _do(cx)


def verify_chain() -> dict:
    """Recompute the chain. Returns ok=False with the first bad sequence number if any
    record was edited, deleted or re-ordered."""
    prev = GENESIS
    n = 0
    with conn() as c:
        cur = c.cursor(name="audit_verify")
        cur.execute("SELECT seq, ts, actor, role, action, object_type, object_id, payload, prev_hash, record_hash "
                    "FROM audit_log ORDER BY seq")
        for r in cur:
            n += 1
            if r["prev_hash"] != prev:
                return {"ok": False, "records": n, "first_bad_seq": r["seq"], "problem": "prev_hash does not match previous record (deletion or re-ordering)"}
            h = _record_hash(prev, _ts_str(r["ts"]), r["actor"], r["role"], r["action"], r["object_type"],
                             r["object_id"], r["payload"])
            if h != r["record_hash"]:
                return {"ok": False, "records": n, "first_bad_seq": r["seq"], "problem": "record content does not match its hash (edited)"}
            prev = r["record_hash"]
    return {"ok": True, "records": n, "head": prev}
