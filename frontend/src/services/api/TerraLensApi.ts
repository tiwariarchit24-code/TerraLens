import type {
  AOI,
  AnalystDecision,
  ArchiveStatus,
  AuditRecord,
  ChainVerification,
  ChangeClass,
  ChangeEvent,
  DatasetEntry,
  ExportRequest,
  ExportResult,
  Job,
  ModelEntry,
  Observation,
  PriorityZone,
  ProvenanceRecord,
  QueryPlan,
  RejectReason,
  SearchFilters,
  SearchResponse,
  SimilarityRequest,
  SimilarityResult,
  Tile,
  User,
  WatchSet,
} from "../../types/domain";

export interface DecisionInput {
  action: "confirm" | "reject" | "relabel" | "defer";
  reasonCode?: RejectReason | null;
  notes?: string;
  newClass?: ChangeClass | null;
  secondsOnCard?: number;
}

/**
 * The single boundary between UI and data. `DemoApi` (src/mocks) and `HttpApi`
 * (./HttpApi) both implement it; pages only ever see this interface.
 * Endpoint names mirror the planned FastAPI routes (see API.md).
 */
export interface TerraLensApi {
  readonly mode: "demo" | "api";

  // system
  currentUser(): Promise<User>;
  systemStatus(): Promise<ArchiveStatus>;
  models(): Promise<ModelEntry[]>;
  datasets(): Promise<DatasetEntry[]>;
  thresholds(): Promise<Record<string, unknown>>;

  // archive
  aois(): Promise<AOI[]>;
  tiles(aoi?: string): Promise<Tile[]>;
  observations(aoi: string): Promise<Observation[]>;
  waterReference(aoi: string): Promise<{ date: string; geometry: GeoJSON.Geometry } | null>;

  // query
  plan(text: string, filters: SearchFilters): Promise<QueryPlan>;
  search(plan: QueryPlan, filters: SearchFilters): Promise<SearchResponse>;
  similar(req: SimilarityRequest): Promise<SimilarityResult[]>;

  // events & review
  events(filters?: Partial<SearchFilters>): Promise<ChangeEvent[]>;
  event(id: string): Promise<ChangeEvent>;
  reviewQueue(): Promise<{ items: ChangeEvent[]; policy: string[]; auditSliceFraction: number }>;
  decide(eventId: string, input: DecisionInput): Promise<{ event: ChangeEvent; decision: AnalystDecision }>;

  // discovery
  watchSets(): Promise<WatchSet[]>;
  createWatchSet(name: string, description: string, positives: string[], negatives: string[]): Promise<WatchSet>;
  rerunWatchSet(id: string): Promise<{ watchSet: WatchSet; results: SimilarityResult[] }>;

  // priority zones
  priorityZones(): Promise<PriorityZone[]>;
  createPriorityZone(z: Omit<PriorityZone, "id" | "createdAt" | "createdBy" | "dataOrigin">): Promise<PriorityZone>;
  setPriorityZoneActive(id: string, active: boolean): Promise<PriorityZone>;

  // provenance & audit
  provenance(artifactType?: string, artifactId?: string): Promise<ProvenanceRecord[]>;
  audit(limit?: number): Promise<AuditRecord[]>;
  verifyAudit(): Promise<ChainVerification>;

  // ingestion & jobs
  jobs(): Promise<Job[]>;
  ingest(input: { fileName: string; sizeBytes: number; heldBackSceneId?: string }): Promise<Job>;
  heldBackScenes(): Promise<Observation[]>;
  retryJob(id: string): Promise<Job>;

  // export
  exportEvents(req: ExportRequest): Promise<ExportResult>;
  exportsList(): Promise<ExportResult[]>;
}
