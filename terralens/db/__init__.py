"""PostgreSQL access (psycopg 3). One pool per process; transactions are explicit."""
from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..config import settings

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def dsn() -> str:
    d = settings()["database"]
    return f"host={d['host']} port={d['port']} user={d['user']} dbname={d['name']} application_name=terralens"


def pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(dsn(), min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextlib.contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """A pooled connection inside one transaction (commit on success, rollback on error)."""
    with pool().connection() as c:
        yield c


def q(sql: str, params: Any = None) -> list[dict]:
    with conn() as c:
        return c.execute(sql, params).fetchall()


def q1(sql: str, params: Any = None) -> dict | None:
    with conn() as c:
        return c.execute(sql, params).fetchone()


def ex(sql: str, params: Any = None) -> None:
    with conn() as c:
        c.execute(sql, params)


def J(obj: Any) -> Jsonb:
    return Jsonb(obj, dumps=_dumps)


def _dumps(o: Any) -> str:
    import json
    return json.dumps(o, default=_default)


def _default(o: Any):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in np.asarray(v, dtype=np.float32).ravel()) + "]"


def parse_vec(s: str | list) -> np.ndarray:
    if isinstance(s, list):
        return np.asarray(s, dtype=np.float32)
    return np.asarray([float(x) for x in s.strip("[]").split(",")], dtype=np.float32)


def init_schema() -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute(sql)
        c.execute("INSERT INTO schema_meta VALUES ('schema_version','1') ON CONFLICT (key) DO NOTHING")


def ensure_hnsw_index(model_id: str, dim: int) -> str:
    """Partial HNSW expression index per encoder: vectors from different models live in
    one table but are never mixed in a search (the query must repeat the predicate)."""
    import hashlib
    name = "emb_hnsw_" + hashlib.sha1(model_id.encode()).hexdigest()[:10]
    lit = model_id.replace("'", "''")
    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON embeddings USING hnsw ((vec::halfvec({int(dim)})) halfvec_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) WHERE model_id = '{lit}'"
        )
    return name
