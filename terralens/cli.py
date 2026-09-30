"""TerraLens command line.  Run `python -m terralens --help`."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from .config import ROOT, path, settings


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("rasterio", "PIL", "urllib3", "httpx", "matplotlib", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def cmd_db_init(a):
    from .db import init_schema
    init_schema()
    print("schema ready")


def cmd_aoi_sync(a):
    from .aoi import ensure_aois, refresh_landcover
    print("aois:", ensure_aois())
    print("tiles with land-cover composition:", refresh_landcover())


def cmd_setup(a):
    from .auth import ensure_users
    cmd_db_init(a)
    cmd_aoi_sync(a)
    from .gis.layers import load_all
    print("gis layers:", load_all())
    from .models_registry import sync_manifest
    _print(sync_manifest())
    users = ensure_users()
    print("local users file:", users)


def cmd_stage_demo(a):
    """Copy staged scenes into the watch folder, holding back the newest scene of each AOI
    (and, optionally, a mid-series scene for the back-fill demo) in data/holdback/."""
    src = ROOT / "data/staging/sentinel2"
    inc, hold = path("incoming"), path("holdback")
    n_in = n_hold = 0
    for aoi_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        if a.aoi and aoi_dir.name not in a.aoi:
            continue
        scenes = sorted((p for p in aoi_dir.iterdir() if (p / "item.json").exists()), key=lambda p: p.name.split("_")[2])
        held = set(s.name for s in scenes[-a.holdback:]) if a.holdback else set()
        for s in scenes:
            dest = (hold if s.name in held else inc) / f"{aoi_dir.name}__{s.name}"
            if dest.exists() or any(True for _ in path("raw").rglob(f"{s.name}__*")):
                continue
            shutil.copytree(s, dest)
            if s.name in held:
                n_hold += 1
            else:
                n_in += 1
    print(f"copied {n_in} scenes to {inc.relative_to(ROOT)}, held back {n_hold} in {hold.relative_to(ROOT)}")


def cmd_ingest_scan(a):
    from .ingest.register import scan
    folder = Path(a.folder).resolve() if a.folder else None
    _print(scan(folder, actor=a.actor))


def cmd_worker(a):
    from .jobs.worker import run_forever, run_until_idle
    if a.until_idle:
        kinds = a.kinds.split(",") if a.kinds else None
        print("jobs run:", run_until_idle(kinds, a.max_jobs))
    else:
        run_forever(watch_folder=not a.no_watch)


def cmd_jobs(a):
    from .jobs import queue
    if a.retry:
        queue.retry(a.retry)
    _print(queue.summary())


def cmd_audit_verify(a):
    from .audit import verify_chain
    r = verify_chain()
    _print(r)
    sys.exit(0 if r["ok"] else 2)


def cmd_reset_archive(a):
    """Development reset: drops the database content and every generated/registered product
    under data/ (staged originals in data/staging and GIS layers are kept)."""
    if not a.yes:
        print("refusing without --yes")
        sys.exit(1)
    import stat
    import psycopg
    from .db import dsn, init_schema
    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for key in ("raw", "cog", "masks", "features", "chips", "quarantine", "exports", "incoming", "holdback", "reports"):
        d = path(key)
        if ROOT / "data" not in d.resolve().parents:
            continue
        for f in d.rglob("*"):
            if f.is_file():
                f.chmod(f.stat().st_mode | stat.S_IWUSR)
        for child in d.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    for extra in ("var/release", "data/timelapse", "data/embeddings"):
        p = ROOT / extra
        if p.exists():
            shutil.rmtree(p)
    init_schema()
    print("archive reset")


def choose_references() -> list[dict]:
    """Per AOI with no reference yet, pick the co-registration reference among registered
    scenes: dry season (Nov-Mar), lowest scene cloud, newest processing baseline, recent
    year - then process it first by raising its job priority."""
    from .db import conn, q
    out = []
    for a in q("SELECT id FROM aois WHERE reference_obs_id IS NULL"):
        r = q("""SELECT s.id, s.source_id FROM scenes s WHERE s.aoi_id=%s AND s.status='registered'
                 AND extract(month from s.acquired_at) IN (11,12,1,2,3)
                 ORDER BY s.cloud_cover_scene ASC NULLS LAST, s.processing_baseline DESC, s.acquired_at DESC LIMIT 1""", (a["id"],))
        if r:
            with conn() as c:
                c.execute("UPDATE jobs SET priority=30 WHERE kind='process_scene' AND status='pending' AND (payload->>'scene_id')::int=%s", (r[0]["id"],))
            out.append({"aoi": a["id"], "scene_id": r[0]["id"], "source_id": r[0]["source_id"]})
    return out


def cmd_bootstrap(a):
    cmd_stage_demo(a)
    from .ingest.register import scan
    res = scan(None, actor="bootstrap")
    by = {}
    for r in res:
        by[r["status"]] = by.get(r["status"], 0) + 1
    print("registration:", by)
    print("references:", choose_references())
    if a.release_radiometry:
        from .db import q
        from .ingest.register import release_quarantine
        for qrow in q("SELECT id, original_name FROM quarantine WHERE reason_code='radiometry_inconsistent' AND NOT released"):
            rr = release_quarantine(qrow["id"], actor="admin", role="admin", reason=a.release_radiometry,
                                    overrides={"radiometric": {"provider_applied": True}})
            print("released", qrow["original_name"], "->", rr.get("status"), rr.get("scene_id"))


def cmd_train_classifier(a):
    from .features.classifier import train
    from .jobs.queue import enqueue
    from .db import q
    r = train(a.version)
    _print(r)
    n = 0
    for o in q("SELECT id FROM observations WHERE classifier_model_id IS DISTINCT FROM %s", (r["model_id"],)):
        enqueue("classify_observation", {"observation_id": o["id"]}, dedupe_key=f"classify:{o['id']}")
        n += 1
    print("classification jobs queued:", n)


def cmd_quarantine(a):
    from .db import q
    if a.release:
        from .ingest.register import release_quarantine
        ov = {"radiometric": {"provider_applied": a.provider_applied == "true"}} if a.provider_applied else None
        _print(release_quarantine(a.release, actor=a.actor, role="admin", reason=a.reason or "", overrides=ov))
        return
    _print(q("SELECT id, original_name, reason_code, reasons, released, created_at FROM quarantine ORDER BY id"))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="terralens")
    ap.add_argument("-v", "--verbose", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("setup", help="initialise database, AOIs, GIS layers, model manifest, local users").set_defaults(fn=cmd_setup)
    sp.add_parser("db-init").set_defaults(fn=cmd_db_init)
    sp.add_parser("aoi-sync").set_defaults(fn=cmd_aoi_sync)
    p = sp.add_parser("stage-demo", help="copy staged Sentinel-2 clips into the watch folder")
    p.add_argument("--aoi", nargs="*")
    p.add_argument("--holdback", type=int, default=1, help="newest N scenes per AOI kept for the live ingest demo")
    p.set_defaults(fn=cmd_stage_demo)
    p = sp.add_parser("bootstrap", help="stage demo scenes, register them, choose co-registration references")
    p.add_argument("--aoi", nargs="*")
    p.add_argument("--holdback", type=int, default=1)
    p.add_argument("--release-radiometry", metavar="REASON",
                   help="ADMIN decision: release scenes quarantined as radiometry_inconsistent with provider_applied=true")
    p.set_defaults(fn=cmd_bootstrap)
    p = sp.add_parser("train-classifier", help="train the LightGBM land-cover classifier on dev blocks")
    p.add_argument("--version", default="1.0.0")
    p.set_defaults(fn=cmd_train_classifier)
    p = sp.add_parser("quarantine", help="list quarantined inputs or release one (admin)")
    p.add_argument("--release", type=int)
    p.add_argument("--provider-applied", choices=["true", "false"])
    p.add_argument("--reason")
    p.add_argument("--actor", default="admin")
    p.set_defaults(fn=cmd_quarantine)
    p = sp.add_parser("ingest", help="scan a folder (default: the watch folder) and register scenes")
    p.add_argument("--folder")
    p.add_argument("--actor", default="cli")
    p.set_defaults(fn=cmd_ingest_scan)
    p = sp.add_parser("worker", help="run the job worker")
    p.add_argument("--until-idle", action="store_true")
    p.add_argument("--kinds")
    p.add_argument("--max-jobs", type=int)
    p.add_argument("--no-watch", action="store_true")
    p.set_defaults(fn=cmd_worker)
    p = sp.add_parser("jobs")
    p.add_argument("--retry", type=int)
    p.set_defaults(fn=cmd_jobs)
    p = sp.add_parser("reset-archive", help="DEV ONLY: wipe database content and generated products")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_reset_archive)
    sp.add_parser("audit-verify", help="verify the hash-chained audit log").set_defaults(fn=cmd_audit_verify)
    return ap


def main(argv: list[str] | None = None) -> None:
    settings()
    ap = build_parser()
    a = ap.parse_args(argv)
    _setup_logging(a.verbose)
    a.fn(a)


if __name__ == "__main__":
    main()
