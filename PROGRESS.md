# TerraLens progress checkpoint (2026-09-30)

Work was paused at the user's request. Nothing is committed (no git operations were done;
the folder is not a git repository). No background processes are running and the local
PostgreSQL server is stopped.

## Current milestone
**Frontend-first milestone**: build the complete analyst workstation UI with an isolated
demo data layer. Backend/ML work is paused on purpose: don't resume it until the
frontend has been reviewed.

## Machine (detected)
macOS 27.0, Apple M4 (4P+6E cores), 16 GB RAM, 8-core GPU (Metal 4), ~14 GB free disk,
system Python 3.14, Node 26.4 / npm 11.17, no Docker, no system Postgres/GDAL.

## Environments (project-local, no sudo)
- `.tools/bin/micromamba`: package manager binary
- `.env/db`: PostgreSQL 18.6 + PostGIS 3.6.4 + pgvector 0.8.6 (conda-forge)
- `.env/py`: Python 3.12, GDAL 3.13, rasterio 1.5, PyTorch 2.13 (MPS available), ONNX Runtime 1.30
  (CoreML + CPU), LightGBM 4.7, open_clip 3.3, FastAPI, psycopg 3
- DB cluster: `var/pgdata` (127.0.0.1:55432, user `terralens`, db `terralens`)
  - start: `.env/db/bin/pg_ctl -D var/pgdata -l var/postgres.log start`

## Data staged (real, public)
- `data/staging/sentinel2/<aoi>/<item>/`: 250 real Sentinel-2 L2A AOI clips (native bands,
  DN unchanged, SCL, item.json, MTD_TL.xml where public). AOIs are korba, gangrel, prayagraj,
  naya_raipur and raipur_kharun (all EPSG:32644).
- `data/gis/korba/`: WorldCover, JRC occurrence/seasonality, DEM. `data/gis/gangrel/`: WorldCover
  only. The GIS staging was stopped mid-run, and OSM (Overpass) is not staged.
- `models/encoders/`: RemoteCLIP ViT-B/32, SkyCLIP ViT-B/32 50 %, OpenCLIP ViT-B/32 LAION-2B.
  These are downloaded but the bake-off has not been run.

## Backend code written (paused, partially tested)
`terralens/`: config, db schema (`db/schema.sql`), hashing, storage (path safety), audit
hash chain, provenance, AOI/grid (deterministic tile IDs, dev/test block split), jobs queue +
worker, ingest adapters (Sentinel-2 clip, generic GeoTIFF + sidecar), Gate-0 validation incl.
radiometric dark-pixel consistency check + audited admin override, standardise, quality mask,
co-registration, normalisation, pipeline, LightGBM classifier (written, not yet trained), CLI.

Tested end to end on Gangrel scenes: register → process → COG/mask → sub-pixel registration.
Findings so far:
- The Earth Search `boa_offset_applied: false` flag is contradicted by the data for some
  2025 scenes. Gate 0 now quarantines those scenes as `radiometry_inconsistent`.
- rasterio's direct `driver="COG"` write corrupted data, so the writer now goes GTiff →
  CreateCopy (the round trip is verified).

Not written yet: embeddings/bake-off, query planner, change engine, gates 2–6, API, tests, docs.

## Frontend (in progress: `frontend/`)
Done:
- Vite + React 19 + TypeScript + MapLibre 5 installed (`frontend/package.json`); `vite.config.ts`
  uses `base: "./"`, so the build can be deployed standalone.
- `src/types/domain.ts`: typed domain model (the API contract).
- `src/services/api/TerraLensApi.ts` (interface) and `HttpApi.ts` (future backend).
- `src/config.ts`: `VITE_DATA_MODE=demo|api`, `VITE_API_BASE` (see `.env.example`).
- Demo assets built from the REAL staged frames by the scripts in `scripts/frontend_assets/`:
  - `public/demo/imagery/<aoi>/*.jpg|_mask.png`: 234 real frames (one fixed stretch) with SCL masks
  - `src/data/demo/generated/*.json`: observations, water extent, AOIs, 899 real tiles,
    concept registry, thresholds, model manifest (real SHA-256), and `events_derived.json`
    (demo index-rule events).

Next steps:
1. Apply the Gate-1 haze (HOT) test in `scripts/frontend_assets/build_demo_events.py`. Frames
   from Dec 2025 to Feb 2026 are hazy and inflate the non-vegetation shares. This edit was
   interrupted and is **not applied**.
2. `src/mocks/`: DemoApi (in-memory state, deterministic planner, persistence state machine
   applied to the demo series, SHA-256 audit chain, simulated ingestion, GeoJSON export).
3. Layout shell, map, pages (search, review, evidence card, history/timelapse, similar sites,
   watch sets, priority zones, provenance, audit, models, system, ingest, exports, settings).
4. Typecheck, build, visual QA in the browser, then the completion message + macOS notification.

Demo state assignments (from the real series):
- KOR-0142: clearance, gradual, confirmed.
- NYR-0012: site clearance, provisional (no usable green-season observation yet).
- GNG-0019: seasonal drawdown, suppressed at Gate 4.
- GNG-0024: cloud/shadow, suppressed at Gate 1.
- KHR-0044: seasonal crop, suppressed.
- PRY-0007: Kumbh temporary structures, transient, priority zone. The brightening rule is
  confounded by the floodplain sand drying out, and the UI must say so.
- KHR-0031: developing site beside the Kharun river. Recheck it after the haze fix.
