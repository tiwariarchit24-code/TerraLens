-- TerraLens schema v1. PostgreSQL 16+ with PostGIS 3 and pgvector 0.8.
-- Analysis geometry is stored in the AOI's UTM projection (EPSG:32644) so every
-- distance/area predicate is in metres; WGS84 copies exist only for display/export.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_meta (key text PRIMARY KEY, value text NOT NULL);

-- ----------------------------------------------------------------- users & roles
CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY,
  display_name text NOT NULL,
  role text NOT NULL CHECK (role IN ('analyst','admin')),
  token_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- AOIs & grid
CREATE TABLE IF NOT EXISTS aois (
  id text PRIMARY KEY,
  name text NOT NULL,
  crs text NOT NULL,
  gsd_m real NOT NULL,
  grid_xmin double precision NOT NULL, grid_ymin double precision NOT NULL,
  grid_xmax double precision NOT NULL, grid_ymax double precision NOT NULL,
  width int NOT NULL, height int NOT NULL,
  geom_utm geometry(Polygon, 32644) NOT NULL,
  geom geometry(Polygon, 4326) NOT NULL,
  reference_obs_id bigint,              -- co-registration reference (fixed once chosen)
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS grid_tiles (
  tile_id text PRIMARY KEY,             -- epsg_gsd_size_s<stride>_c<col>_r<row>: identical on every date
  aoi_id text NOT NULL REFERENCES aois(id),
  epsg int NOT NULL, gsd_m real NOT NULL, size_px int NOT NULL, stride_px int NOT NULL,
  col int NOT NULL, row int NOT NULL,
  grid_version text NOT NULL,
  footprint_utm geometry(Polygon, 32644) NOT NULL,
  footprint geometry(Polygon, 4326) NOT NULL,
  centroid geometry(Point, 4326) NOT NULL,
  landcover jsonb,                      -- reference composition (WorldCover 2021) fractions
  split text NOT NULL DEFAULT 'dev' CHECK (split IN ('dev','test','buffer')),
  block_id text
);
CREATE INDEX IF NOT EXISTS grid_tiles_fp_gix ON grid_tiles USING gist (footprint_utm);
CREATE INDEX IF NOT EXISTS grid_tiles_fp4326_gix ON grid_tiles USING gist (footprint);
CREATE INDEX IF NOT EXISTS grid_tiles_aoi_ix ON grid_tiles (aoi_id);

-- ----------------------------------------------------------------- models & manifests
CREATE TABLE IF NOT EXISTS models (
  id text PRIMARY KEY,                  -- e.g. enc-remoteclip-vitb32@<sha12>
  kind text NOT NULL CHECK (kind IN ('encoder','classifier','calibrator','planner','asr','cloudmask','reranker')),
  name text NOT NULL,
  version text NOT NULL,
  source text,
  license text,
  file_path text,
  sha256 text,
  embedding_dim int,
  input_spec jsonb,                     -- bands, normalisation, input size, GSD suitability
  params jsonb,
  status text NOT NULL DEFAULT 'registered'
    CHECK (status IN ('registered','active','inactive','missing','checksum_mismatch','candidate','rejected')),
  metrics jsonb,
  registered_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS models_kind_status_ix ON models (kind, status);

-- ----------------------------------------------------------------- scenes & observations
CREATE TABLE IF NOT EXISTS scenes (
  id bigserial PRIMARY KEY,
  scene_uid text NOT NULL,              -- identity: platform|sensing time|tile|baseline
  acquisition_key text NOT NULL,        -- platform|sensing time|aoi  (same acquisition, any baseline)
  source_id text NOT NULL,
  adapter text NOT NULL,
  aoi_id text REFERENCES aois(id),
  sensor text NOT NULL, platform text NOT NULL, instrument text,
  processing_level text NOT NULL, processing_baseline text,
  acquired_at timestamptz NOT NULL,
  gsd_m real NOT NULL,
  crs text NOT NULL,
  footprint geometry(Polygon, 4326),
  sun_azimuth real, sun_elevation real, view_azimuth real, view_zenith real,
  cloud_cover_scene real,
  radiometric jsonb NOT NULL,           -- offsets found, whether provider applied them, what we apply
  raw_path text NOT NULL,
  raw_sha256 text NOT NULL,             -- hash over all raw files of the scene
  file_hashes jsonb NOT NULL,
  metadata jsonb NOT NULL,
  synthetic boolean NOT NULL DEFAULT false,
  attribution text,
  status text NOT NULL DEFAULT 'registered'
    CHECK (status IN ('registered','processing','processed','failed','superseded')),
  status_detail text,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS scenes_uid_aoi_ux ON scenes (scene_uid, aoi_id);
CREATE UNIQUE INDEX IF NOT EXISTS scenes_rawsha_ux ON scenes (raw_sha256);
CREATE INDEX IF NOT EXISTS scenes_acq_ix ON scenes (acquired_at);
CREATE INDEX IF NOT EXISTS scenes_sensor_gsd_ix ON scenes (sensor, gsd_m);
CREATE INDEX IF NOT EXISTS scenes_aoi_acq_ix ON scenes (aoi_id, acquired_at);
CREATE INDEX IF NOT EXISTS scenes_fp_gix ON scenes USING gist (footprint);

CREATE TABLE IF NOT EXISTS quarantine (
  id bigserial PRIMARY KEY,
  original_name text NOT NULL,
  stored_path text NOT NULL,
  sha256 text,
  reason_code text NOT NULL,
  reasons jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  released boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS observations (
  id bigserial PRIMARY KEY,
  scene_id bigint NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  aoi_id text NOT NULL REFERENCES aois(id),
  acquired_at timestamptz NOT NULL,
  sensor text NOT NULL, platform text NOT NULL, processing_level text NOT NULL, gsd_m real NOT NULL,
  cog_path text, mask_path text, classprob_path text, chip_path text,
  cog_sha256 text, mask_sha256 text, classprob_sha256 text,
  usable_fraction real, cloud_fraction real, shadow_fraction real, haze_fraction real, haze_score real,
  snow_fraction real, nodata_fraction real, terrain_fraction real, saturation_fraction real,
  reg_ref_obs_id bigint, reg_shift_x real, reg_shift_y real, reg_residual real, reg_peak_ratio real,
  reg_status text CHECK (reg_status IN ('reference','ok','corrected','suspect','unusable')),
  norm_params jsonb,
  usable boolean NOT NULL DEFAULT false,
  unusable_reason text,
  quality jsonb,
  preprocessing_version text NOT NULL,
  classifier_model_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scene_id, aoi_id)
);
CREATE INDEX IF NOT EXISTS obs_aoi_acq_ix ON observations (aoi_id, acquired_at);
CREATE INDEX IF NOT EXISTS obs_usable_ix ON observations (aoi_id, usable);

CREATE TABLE IF NOT EXISTS tile_observations (
  tile_id text NOT NULL REFERENCES grid_tiles(tile_id),
  observation_id bigint NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  acquired_at timestamptz NOT NULL,
  usable_fraction real NOT NULL,
  cloud_fraction real, haze_fraction real,
  ndvi real, mndwi real, ndbi real, bsi real,
  class_fractions jsonb,
  PRIMARY KEY (tile_id, observation_id)
);
CREATE INDEX IF NOT EXISTS tobs_tile_acq_ix ON tile_observations (tile_id, acquired_at);

-- ----------------------------------------------------------------- embeddings (pgvector)
-- One table for every encoder; each encoder gets its own partial HNSW expression index
-- (created at activation) so vectors of different models are never compared.
CREATE TABLE IF NOT EXISTS embeddings (
  id bigserial PRIMARY KEY,
  tile_id text NOT NULL REFERENCES grid_tiles(tile_id),
  observation_id bigint REFERENCES observations(id) ON DELETE CASCADE,
  period text NOT NULL,                 -- 'YYYY-MM' (one per tile per month) or 'query'
  acquired_at timestamptz,
  model_id text NOT NULL REFERENCES models(id),
  preprocessing_version text NOT NULL,
  cache_key text NOT NULL UNIQUE,       -- sha of (source hash, tile, preprocessing, model, model hash)
  usable_fraction real,
  vec halfvec NOT NULL,
  concepts jsonb,                       -- top zero-shot registry concepts (supporting evidence)
  sensor text, gsd_m real,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tile_id, period, model_id)
);
CREATE INDEX IF NOT EXISTS emb_model_period_ix ON embeddings (model_id, period);
CREATE INDEX IF NOT EXISTS emb_tile_ix ON embeddings (tile_id);

-- ----------------------------------------------------------------- GIS reference layers
CREATE TABLE IF NOT EXISTS gis_layers (
  id text PRIMARY KEY,                  -- river, road, water, soil_vertisol, worldcover, jrc_gsw, dem
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('vector','raster')),
  evidence text NOT NULL DEFAULT 'gis',
  source text NOT NULL, license text, version text, extract_date text,
  resolution_m real, path text, sha256 text, params jsonb,
  loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS gis_features (
  id bigserial PRIMARY KEY,
  layer_id text NOT NULL REFERENCES gis_layers(id) ON DELETE CASCADE,
  feature_class text NOT NULL,
  name text,
  attrs jsonb,
  geom geometry(Geometry, 32644) NOT NULL,
  geom_4326 geometry(Geometry, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS gisf_geom_gix ON gis_features USING gist (geom);
CREATE INDEX IF NOT EXISTS gisf_layer_ix ON gis_features (layer_id, feature_class);

-- ----------------------------------------------------------------- change engine
CREATE TABLE IF NOT EXISTS change_runs (
  id bigserial PRIMARY KEY,
  aoi_id text NOT NULL REFERENCES aois(id),
  trigger text NOT NULL,
  n_observations int, n_usable int,
  funnel jsonb,                         -- candidates in/out per gate + leave-one-gate-out counts
  engine_version text NOT NULL, threshold_set text NOT NULL, classifier_model_id text,
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  products jsonb                        -- paths of per-pixel products (first-change date, etc.)
);

CREATE TABLE IF NOT EXISTS change_candidates (
  id bigserial PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES change_runs(id) ON DELETE CASCADE,
  aoi_id text NOT NULL REFERENCES aois(id),
  object_label int NOT NULL,
  change_class text NOT NULL, from_class text NOT NULL, to_class text NOT NULL, form text,
  area_px int NOT NULL,
  geom geometry(MultiPolygon, 32644) NOT NULL,
  first_obs_id bigint,
  features jsonb NOT NULL,
  gate_status text NOT NULL CHECK (gate_status IN ('passed','suppressed')),
  suppressed_by text, suppress_reason text,
  event_id bigint
);
CREATE INDEX IF NOT EXISTS cand_run_ix ON change_candidates (run_id);
CREATE INDEX IF NOT EXISTS cand_geom_gix ON change_candidates USING gist (geom);

CREATE TABLE IF NOT EXISTS change_events (
  id bigserial PRIMARY KEY,
  event_uid text NOT NULL UNIQUE,
  aoi_id text NOT NULL REFERENCES aois(id),
  latest_candidate_id bigint,
  change_class text NOT NULL, from_class text NOT NULL, to_class text NOT NULL,
  form text,
  geom geometry(MultiPolygon, 32644) NOT NULL,
  geom_4326 geometry(MultiPolygon, 4326) NOT NULL,
  centroid geometry(Point, 4326) NOT NULL,
  area_ha real NOT NULL, area_ha_low real, area_ha_high real,
  pct_aoi real, length_m real, elongation real,
  magnitude real, peer_z real, stratum text,
  gate_status text NOT NULL CHECK (gate_status IN ('passed','suppressed')),
  suppressed_by text, suppress_reason text,
  state text NOT NULL CHECK (state IN ('TENTATIVE','PROVISIONAL','CONFIRMED','TRANSIENT','INSUFFICIENT_DATA','REVERSED','REJECTED','SUPPRESSED')),
  machine_state text NOT NULL,          -- state from the rules, never overwritten by the analyst
  analyst_state text CHECK (analyst_state IN ('confirmed','rejected','relabelled','deferred')),
  state_changed_at timestamptz,
  last_clean_pre_obs_id bigint, earliest_supported_obs_id bigint,
  confirmation_obs_ids bigint[], contradicting_obs_ids bigint[],
  n_confirming int NOT NULL DEFAULT 0, n_contradicting int NOT NULL DEFAULT 0,
  last_usable_obs_at timestamptz,
  bracket_days real,
  temporal_mode text, sharpness real, gap_note text,
  in_priority_zone boolean NOT NULL DEFAULT false, priority_zone_ids bigint[],
  confidence_score real, confidence_calibrated boolean NOT NULL DEFAULT false,
  confidence_level text, confidence_version text,
  reasons jsonb, quality jsonb,
  sensor text, gsd_m real,
  threshold_set text NOT NULL, engine_version text NOT NULL, classifier_model_id text,
  pipeline_version text NOT NULL,
  synthetic boolean NOT NULL DEFAULT false,
  audit_sample boolean NOT NULL DEFAULT false,
  lookalike_warning jsonb,
  parent_event_id bigint REFERENCES change_events(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ev_state_ix ON change_events (state, gate_status);
CREATE INDEX IF NOT EXISTS ev_aoi_class_ix ON change_events (aoi_id, change_class);
CREATE INDEX IF NOT EXISTS ev_geom_gix ON change_events USING gist (geom);
CREATE INDEX IF NOT EXISTS ev_geom4326_gix ON change_events USING gist (geom_4326);
CREATE INDEX IF NOT EXISTS ev_earliest_ix ON change_events (earliest_supported_obs_id);

CREATE TABLE IF NOT EXISTS event_observations (
  event_id bigint NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
  observation_id bigint NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  acquired_at timestamptz NOT NULL,
  role text NOT NULL,                   -- pre | last_clean_pre | first_supported | confirming | contradicting | unusable | post | ambiguous
  usable boolean NOT NULL,
  usable_share real, post_share real, pre_share real,
  value_ha real, value_ha_low real, value_ha_high real,
  gates_ok boolean, gate_notes jsonb,
  PRIMARY KEY (event_id, observation_id)
);

CREATE TABLE IF NOT EXISTS gate_results (
  id bigserial PRIMARY KEY,
  candidate_id bigint REFERENCES change_candidates(id) ON DELETE CASCADE,
  event_id bigint REFERENCES change_events(id) ON DELETE CASCADE,
  observation_id bigint,
  gate text NOT NULL,                   -- G0..G6
  gate_name text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('pass','fail','flag','n/a')),
  reason_code text,
  measured jsonb NOT NULL,
  threshold_set text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gates_event_ix ON gate_results (event_id);
CREATE INDEX IF NOT EXISTS gates_cand_ix ON gate_results (candidate_id);

CREATE TABLE IF NOT EXISTS event_state_history (
  id bigserial PRIMARY KEY,
  event_id bigint NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
  from_state text, to_state text NOT NULL,
  trigger text NOT NULL,
  observation_id bigint,
  reason text,
  at timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- confidence & calibration
CREATE TABLE IF NOT EXISTS confidence_versions (
  id text PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('change','retrieval')),
  concept_id text,
  model jsonb NOT NULL,                 -- coefficients, intercept, feature names, scaler
  n_train int NOT NULL, n_pos int NOT NULL,
  metrics jsonb,                        -- held-out ECE, Brier, log-loss, reliability bins
  status text NOT NULL CHECK (status IN ('active','candidate','rejected','retired')),
  calibrated_display boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  fitted_on jsonb
);

-- ----------------------------------------------------------------- queries
CREATE TABLE IF NOT EXISTS queries (
  id bigserial PRIMARY KEY,
  text text,
  source text NOT NULL DEFAULT 'typed',
  plan jsonb NOT NULL,
  planner_version text NOT NULL,
  parser_path text,                     -- deterministic | deterministic+llm
  edited boolean NOT NULL DEFAULT false,
  analyst_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  executed_at timestamptz,
  latency_ms real,
  n_results int
);
CREATE TABLE IF NOT EXISTS query_results (
  query_id bigint NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
  rank int NOT NULL,
  result_type text NOT NULL,
  ref_id text NOT NULL,
  score real, percentile real, calibrated real,
  payload jsonb,
  PRIMARY KEY (query_id, rank)
);

-- ----------------------------------------------------------------- analyst decisions
CREATE TABLE IF NOT EXISTS analyst_decisions (
  id bigserial PRIMARY KEY,
  target_type text NOT NULL CHECK (target_type IN ('event','tile')),
  event_id bigint REFERENCES change_events(id),
  tile_id text, embedding_id bigint, query_id bigint, concept_id text,
  action text NOT NULL CHECK (action IN ('confirm','reject','relabel','defer')),
  reason_code text, notes text, new_class text,
  previous_state text, new_state text,
  analyst_id text NOT NULL, analyst_role text NOT NULL,
  evidence_snapshot jsonb NOT NULL, evidence_hash text NOT NULL,
  audit_sample boolean NOT NULL DEFAULT false,
  seconds_on_card real,
  split text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dec_event_ix ON analyst_decisions (event_id);
CREATE INDEX IF NOT EXISTS dec_tile_ix ON analyst_decisions (tile_id);

-- ----------------------------------------------------------------- watch sets & zones
CREATE TABLE IF NOT EXISTS watch_sets (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  description text,
  model_id text,
  params jsonb,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_run_at timestamptz
);
CREATE TABLE IF NOT EXISTS watch_set_members (
  watch_set_id bigint NOT NULL REFERENCES watch_sets(id) ON DELETE CASCADE,
  tile_id text NOT NULL REFERENCES grid_tiles(tile_id),
  period text,
  role text NOT NULL CHECK (role IN ('positive','negative')),
  added_by text NOT NULL,
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (watch_set_id, tile_id, role)
);
CREATE TABLE IF NOT EXISTS watch_set_hits (
  watch_set_id bigint NOT NULL REFERENCES watch_sets(id) ON DELETE CASCADE,
  tile_id text NOT NULL,
  run_at timestamptz NOT NULL,
  score real NOT NULL,
  percentile real,
  is_new boolean NOT NULL DEFAULT false,
  PRIMARY KEY (watch_set_id, tile_id, run_at)
);
CREATE TABLE IF NOT EXISTS priority_zones (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  kind text NOT NULL DEFAULT 'monitoring',
  geom geometry(Polygon, 4326) NOT NULL,
  geom_utm geometry(Polygon, 32644) NOT NULL,
  params jsonb,
  notify boolean NOT NULL DEFAULT false,
  active boolean NOT NULL DEFAULT true,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pz_geom_gix ON priority_zones USING gist (geom_utm);

-- ----------------------------------------------------------------- provenance & audit
CREATE TABLE IF NOT EXISTS provenance (
  id bigserial PRIMARY KEY,
  artifact_type text NOT NULL,
  artifact_id text NOT NULL,
  artifact_path text,
  artifact_sha256 text,
  activity text NOT NULL,
  inputs jsonb NOT NULL,                -- [{type, id, sha256}]
  code_version text NOT NULL,
  pipeline_version text,
  model_ids jsonb,
  config_version text NOT NULL,
  threshold_set text,
  params jsonb,
  started_at timestamptz, ended_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prov_artifact_ix ON provenance (artifact_type, artifact_id);

CREATE TABLE IF NOT EXISTS audit_log (
  seq bigserial PRIMARY KEY,
  ts timestamptz NOT NULL,
  actor text NOT NULL,
  role text NOT NULL,
  action text NOT NULL,
  object_type text NOT NULL,
  object_id text,
  payload jsonb NOT NULL,
  prev_hash text NOT NULL,
  record_hash text NOT NULL UNIQUE
);

-- ----------------------------------------------------------------- jobs
CREATE TABLE IF NOT EXISTS jobs (
  id bigserial PRIMARY KEY,
  kind text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','complete','failed','retry','cancelled')),
  priority int NOT NULL DEFAULT 100,
  payload jsonb NOT NULL,
  dedupe_key text,
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  locked_by text, locked_at timestamptz, heartbeat_at timestamptz,
  progress jsonb,
  started_at timestamptz, finished_at timestamptz, duration_ms real,
  error text,
  result jsonb,
  parent_job_id bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_status_ix ON jobs (status, priority, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe_active_ux ON jobs (dedupe_key)
  WHERE status IN ('pending','running','retry');

-- ----------------------------------------------------------------- exports & evaluation
CREATE TABLE IF NOT EXISTS exports (
  id bigserial PRIMARY KEY,
  format text NOT NULL,
  path text NOT NULL,
  sha256 text NOT NULL,
  params jsonb,
  n_features int,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS evaluation_runs (
  id bigserial PRIMARY KEY,
  kind text NOT NULL,
  split text,
  config jsonb,
  metrics jsonb,
  report_path text,
  code_version text,
  sealed_hash text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- verified sources (feature-flagged)
CREATE TABLE IF NOT EXISTS trusted_sources (
  id text PRIMARY KEY,
  name text NOT NULL,
  public_key_hex text NOT NULL,
  status text NOT NULL CHECK (status IN ('trusted','revoked')),
  added_by text NOT NULL,
  added_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);
CREATE TABLE IF NOT EXISTS source_packs (
  id text PRIMARY KEY,
  source_id text NOT NULL REFERENCES trusted_sources(id),
  manifest jsonb NOT NULL,
  manifest_sha256 text NOT NULL,
  signature_ok boolean NOT NULL,
  checks jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('quarantined','approved','rejected','revoked')),
  stored_path text NOT NULL,
  decided_by text, decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS verified_records (
  id bigserial PRIMARY KEY,
  pack_id text NOT NULL REFERENCES source_packs(id) ON DELETE CASCADE,
  record_class text NOT NULL,
  observed_date date,
  attrs jsonb,
  geom geometry(Geometry, 32644) NOT NULL,
  active boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS vrec_geom_gix ON verified_records USING gist (geom);

-- ----------------------------------------------------------------- discovery (k-means gallery)
CREATE TABLE IF NOT EXISTS cluster_runs (
  id bigserial PRIMARY KEY,
  model_id text NOT NULL, k int NOT NULL, n_vectors int NOT NULL,
  params jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tile_clusters (
  run_id bigint NOT NULL REFERENCES cluster_runs(id) ON DELETE CASCADE,
  embedding_id bigint NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
  cluster int NOT NULL, distance real NOT NULL,
  PRIMARY KEY (run_id, embedding_id)
);
