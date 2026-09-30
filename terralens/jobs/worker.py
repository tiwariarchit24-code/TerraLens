"""The single worker process: claims jobs, runs handlers, records outcome.

A failure in one job never stops the worker; the job is retried up to max_attempts and
then marked failed (retryable by an admin). Each job logs start, end, duration, status,
input, output and error.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
import traceback
from typing import Callable

from ..config import settings
from . import queue

log = logging.getLogger("terralens.worker")
_stop = threading.Event()


def _h_ingest_scan(p):
    from ..ingest.register import scan
    from ..config import path
    from ..storage import resolve_within
    folder = resolve_within(path("data"), p["folder"]) if p.get("folder") else None
    res = scan(folder, actor=p.get("actor", "ingest"))
    return {"results": res}


def _h_process_scene(p):
    from ..pipeline import process_scene
    return process_scene(int(p["scene_id"]), p["aoi_id"])


def _h_classify(p):
    from ..features.classifier import classify_observation
    return classify_observation(int(p["observation_id"]))


def _h_embed(p):
    from ..embed.embedder import embed_observation
    return embed_observation(int(p["observation_id"]))


def _h_change(p):
    from ..change.engine import update_aoi
    return update_aoi(p["aoi_id"], trigger=p.get("trigger", "ingest"))


def _h_watchset(p):
    from ..retrieval.watchsets import rerun_all
    return rerun_all(trigger=p.get("trigger", "ingest"))


HANDLERS: dict[str, Callable[[dict], dict]] = {
    "ingest_scan": _h_ingest_scan, "process_scene": _h_process_scene, "classify_observation": _h_classify,
    "embed_observation": _h_embed, "change_update": _h_change, "watchset_rerun": _h_watchset,
}


def _heartbeat_loop(job_id: int, stop: threading.Event):
    period = int(settings()["runtime"]["job_heartbeat_seconds"])
    while not stop.wait(period):
        try:
            queue.heartbeat(job_id)
        except Exception:
            pass


def run_one(kinds: list[str] | None = None) -> bool:
    job = queue.claim(kinds)
    if not job:
        return False
    h = HANDLERS.get(job["kind"])
    t0 = time.time()
    log.info("job %s %s start payload=%s attempt=%s", job["id"], job["kind"], json.dumps(job["payload"]), job["attempts"])
    hb_stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(job["id"], hb_stop), daemon=True)
    hb.start()
    try:
        if h is None:
            raise RuntimeError(f"no handler for job kind {job['kind']}")
        res = h(job["payload"]) or {}
        queue.complete(job["id"], res)
        log.info("job %s %s complete in %.1fs result=%s", job["id"], job["kind"], time.time() - t0,
                 json.dumps(res, default=str)[:300])
    except Exception as e:
        tb = traceback.format_exc()
        st = queue.fail(job["id"], f"{e}\n{tb}")
        log.error("job %s %s %s after %.1fs: %s", job["id"], job["kind"], st, time.time() - t0, e)
        try:
            from ..db import ex
            if job["kind"] == "process_scene" and st == "failed":
                ex("UPDATE scenes SET status='failed', status_detail=%s WHERE id=%s", (str(e)[:500], job["payload"]["scene_id"]))
        except Exception:
            pass
    finally:
        hb_stop.set()
    return True


def run_until_idle(kinds: list[str] | None = None, max_jobs: int | None = None) -> int:
    n = 0
    while run_one(kinds):
        n += 1
        if max_jobs and n >= max_jobs:
            break
    return n


def run_forever(watch_folder: bool = True) -> None:
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    poll = float(settings()["runtime"]["worker_poll_seconds"])
    last_scan = 0.0
    log.info("worker %s started (poll %.1fs, watch folder %s)", queue.worker_id(), poll, watch_folder)
    while not _stop.is_set():
        if watch_folder and time.time() - last_scan > 10:
            from ..config import path
            if any(not p.name.startswith(".") for p in path("incoming").iterdir()):
                queue.enqueue("ingest_scan", {"actor": "watch-folder"}, dedupe_key="ingest_scan")
            last_scan = time.time()
        if not run_one():
            _stop.wait(poll)
    log.info("worker stopped")
