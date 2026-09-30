/**
 * TerraLens domain model shared by every page. The demo data layer and the future HTTP
 * API both return these shapes, so the UI never knows which one it is talking to.
 *
 * Provenance of values: every object that can carry illustrative content has a
 * `dataOrigin` field. The UI renders a DEMO / ILLUSTRATIVE marker from it.
 */

export type ISODate = string; // YYYY-MM-DD
export type ISODateTime = string;
export type LonLat = [number, number];

/** Where a value came from. `real` = read from real staged data (e.g. Sentinel-2 metadata);
 *  `derived-demo` = computed by a documented demo rule on real pixels; `illustrative` =
 *  hand-authored to show the UI, not produced by any TerraLens component. */
export type DataOrigin = "real" | "derived-demo" | "illustrative" | "computed" | "simulated";

export type EvidenceType = "observed" | "inferred" | "gis" | "verified_record" | "unverifiable";
export type ChipKind = "concept" | "change" | "spatial" | "reference" | "temporal" | "attribute" | "metadata" | "aoi";

export type PersistenceState =
  | "TENTATIVE"
  | "PROVISIONAL"
  | "CONFIRMED"
  | "TRANSIENT"
  | "INSUFFICIENT_DATA"
  | "REVERSED"
  | "REJECTED";

export type ChangeClass =
  | "construction"
  | "clearance"
  | "road_development"
  | "water_expansion"
  | "water_contraction"
  | "other_change"
  | "seasonal"
  | "transient";

export type ChangeForm = "appearance" | "expansion" | "contraction" | "disappearance";
export type LandCoverClass = "water" | "trees" | "other_veg" | "bare" | "built";
export type GateId = "G0" | "G1" | "G2" | "G3" | "G4" | "G5" | "G6";
export type GateOutcome = "pass" | "fail" | "flag" | "waiting" | "n/a";
export type ConfidenceLevel = "LOW" | "MEDIUM" | "HIGH";
export type Role = "analyst" | "admin";

export interface User {
  id: string;
  displayName: string;
  role: Role;
}

export interface AOI {
  id: string;
  name: string;
  purpose: string[];
  crs: string;
  gsd: number;
  widthPx: number;
  heightPx: number;
  areaKm2: number;
  ring: LonLat[];
}

export interface Tile {
  id: string;
  aoi: string;
  col: number;
  row: number;
  split: "dev" | "test" | "buffer";
  ring: LonLat[];
  landcover: Record<string, number> | null;
}

export interface Observation {
  id: string;
  aoi: string;
  date: ISODate;
  datetime: ISODateTime;
  sceneId: string;
  platform: string;
  sensor: string;
  processingLevel: string;
  processingBaseline: string | null;
  providerOffsetFlag: boolean | null;
  offsetApplied: number;
  sceneCloudCover: number;
  usableFraction: number;
  cloudFraction: number;
  shadowFraction: number;
  sunElevation: number | null;
  sunAzimuth: number | null;
  viewIncidence: number | null;
  gsd: number;
  image: string;
  mask: string;
  bounds: LonLat[]; // TL, TR, BR, BL
  stats: { waterFraction: number; meanNdvi: number };
}

export interface Scene {
  id: string;
  aoi: string;
  platform: string;
  sensor: string;
  processingLevel: string;
  processingBaseline: string | null;
  acquiredAt: ISODateTime;
  cloudCover: number;
  status: "registered" | "processing" | "processed" | "failed" | "superseded" | "quarantined";
  rawSha256?: string;
  dataOrigin: DataOrigin;
}

/* ------------------------------------------------------------------ query planning */

export interface PlanChip {
  id: string;
  kind: ChipKind;
  label: string;
  evidence: EvidenceType | "temporal" | "spatial" | "metadata";
  route: "appearance" | "event" | "gis" | "metadata" | "record" | "none" | "filter";
  applied: boolean;
  value?: Record<string, unknown>;
  explanation?: string;
  editable?: boolean;
  source?: string; // e.g. "deterministic: '200 m'"
}

export interface UnverifiableItem {
  phrase: string;
  conceptId: string;
  reason: string;
  wouldNeed?: string;
}

export interface QueryPlan {
  id: string;
  text: string;
  intent: "find_changes" | "find_appearance" | "find_similar" | "history" | "unanswerable";
  chips: PlanChip[];
  unverifiable: UnverifiableItem[];
  notes: string[];
  parser: "deterministic" | "deterministic+llm";
  plannerVersion: string;
  referenceDate: ISODate;
  answerablePart: string;
}

export interface SearchFilters {
  aoi: string | "all";
  dateFrom: ISODate | null;
  dateTo: ISODate | null;
  sensor: string | "any";
  maxGsd: number | null;
  changeTypes: ChangeClass[];
  minState: PersistenceState | "any";
  minConfidence: ConfidenceLevel | "any";
  includeSuppressed: boolean;
  includeTransient: boolean;
  maxDistanceM: number | null;
  referenceLayer: string | null;
  watchSetId: string | null;
}

/* ------------------------------------------------------------------ results */

export interface ScoreTriple {
  /** raw cosine similarity, -1..1. Never a probability. */
  similarity: number | null;
  /** rank statistic against a background sample for this query, 0..100 */
  percentile: number | null;
  /** fitted probability, only when a calibration exists for this concept */
  calibrated: number | null;
  calibrationStatus: "calibrated" | "uncalibrated" | "not-applicable";
}

export interface SearchResult {
  rank: number;
  kind: "event" | "tile";
  refId: string;
  title: string;
  aoi: string;
  location: LonLat;
  date: ISODate | null;
  sensor: string;
  gsd: number;
  evidence: EvidenceType;
  state?: PersistenceState;
  changeClass?: ChangeClass;
  areaHa?: number;
  distanceM?: number | null;
  scores: ScoreTriple;
  matched: { label: string; evidence: EvidenceType | "temporal" | "spatial" }[];
  reason: string;
  dataOrigin: DataOrigin;
}

export interface SearchResponse {
  plan: QueryPlan;
  results: SearchResult[];
  suppressed: SearchResult[];
  latencyMs: number;
  executedAt: ISODateTime;
  notes: string[];
}

export interface SimilarityRequest {
  tileId?: string;
  upload?: { name: string; gsd: number | null; georeferenced: boolean };
  positives: string[];
  negatives: string[];
  aoi: string | "all";
  k: number;
}

export interface SimilarityResult {
  rank: number;
  tileId: string;
  aoi: string;
  location: LonLat;
  period: string;
  scores: ScoreTriple;
  sharedConcepts: string[];
  landcover: Record<string, number> | null;
  nearestPositive?: string;
  dataOrigin: DataOrigin;
}

/* ------------------------------------------------------------------ change events */

export interface GateMeasurement {
  name: string;
  value: string | number | boolean | null;
  threshold?: string | number;
  unit?: string;
}

export interface GateResult {
  gate: GateId;
  name: string;
  outcome: GateOutcome;
  reasonCode: string | null;
  summary: string;
  measured: GateMeasurement[];
  thresholdSet: string;
  observationIds?: string[];
  dataOrigin: DataOrigin;
}

export interface Confidence {
  level: ConfidenceLevel;
  score: number | null;
  calibrated: boolean;
  calibrationVersion: string | null;
  /** exact log-odds contributions of the logistic model (feature, contribution) */
  contributions: { feature: string; value: number }[];
  capNote?: string;
  dataOrigin: DataOrigin;
}

export type ObservationRole =
  | "pre"
  | "last_clean_pre"
  | "first_supported"
  | "confirming"
  | "contradicting"
  | "unusable"
  | "post"
  | "ambiguous";

export interface EventObservation {
  obsId: string;
  date: ISODate;
  role: ObservationRole;
  usableShare: number;
  postShare: number | null;
  valueHa: number | null;
}

export interface StateTransition {
  from: PersistenceState | null;
  to: PersistenceState;
  at: ISODate;
  trigger: string;
  observationId?: string;
  reason: string;
}

export interface AnalystDecision {
  id: string;
  targetType: "event" | "tile";
  targetId: string;
  action: "confirm" | "reject" | "relabel" | "defer";
  reasonCode: RejectReason | null;
  notes: string;
  newClass: ChangeClass | null;
  previousState: string;
  newState: string;
  analystId: string;
  analystRole: Role;
  evidenceHash: string;
  createdAt: ISODateTime;
  auditSample: boolean;
}

export type RejectReason =
  | "cloud"
  | "shadow"
  | "haze"
  | "seasonal"
  | "misregistration"
  | "radiometric"
  | "sensor"
  | "transient"
  | "false_semantic_match"
  | "wrong_change_class"
  | "too_small"
  | "duplicate"
  | "other";

export interface ChangeEvent {
  id: string;
  title: string;
  aoi: string;
  changeClass: ChangeClass;
  fromClass: LandCoverClass | string;
  toClass: LandCoverClass | string;
  form: ChangeForm;
  geometry: GeoJSON.Geometry;
  centroid: LonLat;
  areaHa: number;
  areaRangeHa: [number, number];
  aoiSharePct: number;
  lengthM: number | null;
  magnitude: number | null;
  state: PersistenceState;
  machineState: PersistenceState;
  analystState: "confirmed" | "rejected" | "relabelled" | "deferred" | null;
  suppressed: boolean;
  suppressedBy: GateId | null;
  suppressReason: string | null;
  inPriorityZone: boolean;
  priorityZoneIds: string[];
  lastCleanPre: EventObservation | null;
  earliestSupported: EventObservation | null;
  confirmations: EventObservation[];
  observations: EventObservation[];
  bracketDays: number | null;
  gapNote: string | null;
  temporalMode: "abrupt" | "gradual" | "abrupt_or_unobserved_gradual" | "undetermined";
  sharpness: number | null;
  confidence: Confidence;
  gates: GateResult[];
  reasons: string[];
  sensor: string;
  gsd: number;
  sourceSceneIds: string[];
  thresholdSet: string;
  engineVersion: string;
  pipelineVersion: string;
  classifierVersion: string | null;
  stateHistory: StateTransition[];
  decisions: AnalystDecision[];
  maskImage: string | null;
  firstChangeImage: string | null;
  firstChangeDates: ISODate[];
  measurementLabel: string;
  measurementNote: string;
  lookalikeWarning: string | null;
  auditSample: boolean;
  dataOrigin: DataOrigin;
  originNote: string;
}

/* ------------------------------------------------------------------ collections */

export interface WatchSet {
  id: string;
  name: string;
  description: string;
  positives: string[];
  negatives: string[];
  createdBy: string;
  createdAt: ISODateTime;
  lastRunAt: ISODateTime | null;
  newHits: number;
  dataOrigin: DataOrigin;
}

export interface PriorityZone {
  id: string;
  name: string;
  purpose: "protected_area" | "critical_infrastructure" | "dam" | "border_region" | "monitoring";
  ring: LonLat[];
  active: boolean;
  notify: boolean;
  policy: { pDetect: number; mmuPx: number; surfaceTentative: boolean; keepTransientVisible: boolean };
  createdBy: string;
  createdAt: ISODateTime;
  dataOrigin: DataOrigin;
}

export interface ProvenanceRecord {
  id: string;
  artifactType: string;
  artifactId: string;
  artifactSha256: string | null;
  activity: string;
  inputs: { type: string; id: string; sha256: string | null }[];
  codeVersion: string;
  pipelineVersion: string | null;
  modelIds: string[];
  configVersion: string;
  thresholdSet: string | null;
  params: Record<string, unknown>;
  createdAt: ISODateTime;
  dataOrigin: DataOrigin;
}

export interface AuditRecord {
  seq: number;
  ts: ISODateTime;
  actor: string;
  role: string;
  action: string;
  objectType: string;
  objectId: string | null;
  payload: Record<string, unknown>;
  prevHash: string;
  recordHash: string;
}

export interface ChainVerification {
  ok: boolean;
  records: number;
  firstBadSeq?: number;
  problem?: string;
  head?: string;
  checkedAt: ISODateTime;
}

export interface ModelEntry {
  id: string;
  kind: "encoder" | "classifier" | "calibrator" | "planner" | "asr" | "cloudmask";
  name: string;
  license: string;
  source: string;
  file: string | null;
  sha256: string | null;
  sizeBytes: number | null;
  embeddingDim?: number;
  bands?: string;
  gsdSuitability?: string;
  status: "staged" | "active" | "not-staged" | "not-trained" | "checksum-mismatch" | "not-benchmarked";
  note: string;
}

export interface DatasetEntry {
  id: string;
  name: string;
  license: string;
  source: string;
  role: string;
  status: "staged" | "partial" | "not-staged";
  detail: string;
}

export type JobStatus = "pending" | "running" | "complete" | "failed" | "retry" | "cancelled";
export type IngestStage =
  | "VALIDATING"
  | "REGISTERING"
  | "PREPROCESSING"
  | "QUALITY"
  | "FEATURES"
  | "EMBEDDING"
  | "INDEXING"
  | "CHANGE_UPDATE"
  | "COMPLETE";

export interface Job {
  id: string;
  kind: string;
  status: JobStatus;
  stage: IngestStage | null;
  stageIndex: number;
  input: string;
  startedAt: ISODateTime | null;
  finishedAt: ISODateTime | null;
  durationMs: number | null;
  log: { at: ISODateTime; stage: IngestStage | string; message: string }[];
  error: string | null;
  result: Record<string, unknown> | null;
  dataOrigin: DataOrigin;
}

export interface ArchiveStatus {
  network: "offline" | "online" | "unknown";
  networkNote: string;
  dataMode: "demo" | "api";
  archiveFrom: ISODate;
  archiveTo: ISODate;
  aois: number;
  scenes: number;
  observations: number;
  usableObservations: number;
  tiles: number;
  embeddings: number | null;
  embeddingsNote: string;
  events: number;
  suppressedCandidates: number;
  activeEncoder: string | null;
  classifier: string | null;
  indexStatus: string;
  lastIngestion: ISODateTime | null;
  jobs: { pending: number; running: number; failed: number };
  storage: { label: string; bytes: number | null }[];
  hardware: string;
  selfTest: { ranAt: ISODateTime | null; ok: boolean | null; note: string };
  counts: Record<string, DataOrigin>;
}

export interface ExportRequest {
  format: "geojson" | "gpkg";
  eventIds: string[];
  include: { provenance: boolean; decisions: boolean; observations: boolean; gates: boolean };
}

export interface ExportResult {
  id: string;
  format: string;
  filename: string;
  sizeBytes: number;
  sha256: string;
  features: number;
  createdAt: ISODateTime;
  blobUrl: string | null;
  note: string;
}
