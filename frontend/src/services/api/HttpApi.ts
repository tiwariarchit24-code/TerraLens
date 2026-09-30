import type { TerraLensApi, DecisionInput } from "./TerraLensApi";
import type * as D from "../../types/domain";

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) {
    super(message);
  }
}

/**
 * HTTP implementation against the planned FastAPI backend (API.md). Every route here is
 * the contract the backend must satisfy; nothing in the UI changes when switching modes.
 * The base URL comes from configuration (VITE_API_BASE), never from components.
 */
export class HttpApi implements TerraLensApi {
  readonly mode = "api" as const;
  constructor(private base: string, private token: () => string | null = () => null) {}

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (init?.body) headers["Content-Type"] = "application/json";
    const t = this.token();
    if (t) headers.Authorization = `Bearer ${t}`;
    const r = await fetch(`${this.base.replace(/\/$/, "")}${path}`, { ...init, headers: { ...headers, ...(init?.headers ?? {}) } });
    if (!r.ok) {
      let detail: unknown;
      try {
        detail = await r.json();
      } catch {
        detail = await r.text();
      }
      throw new ApiError(r.status, `${r.status} ${r.statusText} on ${path}`, detail);
    }
    return (await r.json()) as T;
  }
  private post<T>(path: string, body: unknown) {
    return this.req<T>(path, { method: "POST", body: JSON.stringify(body) });
  }

  currentUser() { return this.req<D.User>("/auth/me"); }
  systemStatus() { return this.req<D.ArchiveStatus>("/system/status"); }
  models() { return this.req<D.ModelEntry[]>("/models"); }
  datasets() { return this.req<D.DatasetEntry[]>("/datasets"); }
  thresholds() { return this.req<Record<string, unknown>>("/system/thresholds"); }
  aois() { return this.req<D.AOI[]>("/aois"); }
  tiles(aoi?: string) { return this.req<D.Tile[]>(`/tiles${aoi ? `?aoi=${encodeURIComponent(aoi)}` : ""}`); }
  observations(aoi: string) { return this.req<D.Observation[]>(`/observations?aoi=${encodeURIComponent(aoi)}`); }
  waterReference(aoi: string) { return this.req<{ date: string; geometry: GeoJSON.Geometry } | null>(`/gis/water?aoi=${encodeURIComponent(aoi)}`); }
  plan(text: string, filters: D.SearchFilters) { return this.post<D.QueryPlan>("/query/plan", { text, filters }); }
  search(plan: D.QueryPlan, filters: D.SearchFilters) { return this.post<D.SearchResponse>("/query/search", { plan, filters }); }
  similar(req: D.SimilarityRequest) { return this.post<D.SimilarityResult[]>("/query/similar", req); }
  events(filters?: Partial<D.SearchFilters>) { return this.post<D.ChangeEvent[]>("/events/search", filters ?? {}); }
  event(id: string) { return this.req<D.ChangeEvent>(`/events/${encodeURIComponent(id)}/evidence`); }
  reviewQueue() { return this.req<{ items: D.ChangeEvent[]; policy: string[]; auditSliceFraction: number }>("/review"); }
  decide(eventId: string, input: DecisionInput) {
    return this.post<{ event: D.ChangeEvent; decision: D.AnalystDecision }>(`/review/${encodeURIComponent(eventId)}/decision`, input);
  }
  watchSets() { return this.req<D.WatchSet[]>("/watchsets"); }
  createWatchSet(name: string, description: string, positives: string[], negatives: string[]) {
    return this.post<D.WatchSet>("/watchsets", { name, description, positives, negatives });
  }
  rerunWatchSet(id: string) { return this.post<{ watchSet: D.WatchSet; results: D.SimilarityResult[] }>(`/watchsets/${encodeURIComponent(id)}/run`, {}); }
  priorityZones() { return this.req<D.PriorityZone[]>("/priority-zones"); }
  createPriorityZone(z: Omit<D.PriorityZone, "id" | "createdAt" | "createdBy" | "dataOrigin">) { return this.post<D.PriorityZone>("/priority-zones", z); }
  setPriorityZoneActive(id: string, active: boolean) { return this.post<D.PriorityZone>(`/priority-zones/${encodeURIComponent(id)}/active`, { active }); }
  provenance(artifactType?: string, artifactId?: string) {
    const q = new URLSearchParams();
    if (artifactType) q.set("type", artifactType);
    if (artifactId) q.set("id", artifactId);
    return this.req<D.ProvenanceRecord[]>(`/provenance?${q}`);
  }
  audit(limit = 200) { return this.req<D.AuditRecord[]>(`/audit?limit=${limit}`); }
  verifyAudit() { return this.post<D.ChainVerification>("/audit/verify", {}); }
  jobs() { return this.req<D.Job[]>("/ingest/jobs"); }
  ingest(input: { fileName: string; sizeBytes: number; heldBackSceneId?: string }) { return this.post<D.Job>("/ingest", input); }
  heldBackScenes() { return this.req<D.Observation[]>("/ingest/holdback"); }
  retryJob(id: string) { return this.post<D.Job>(`/ingest/jobs/${encodeURIComponent(id)}/retry`, {}); }
  exportEvents(req: D.ExportRequest) { return this.post<D.ExportResult>("/export", req); }
  exportsList() { return this.req<D.ExportResult[]>("/export"); }
}
