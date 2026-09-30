import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";

type SiteStatus =
  | "Confirmed"
  | "Provisional"
  | "Tentative"
  | "Transient"
  | "Insufficient Data"
  | "Rejected";

type Site = {
  id: string;
  name: string;
  region: string;
  coordinates: [number, number];
  status: SiteStatus;
  firstChange: string;
  areaHa: number;
  changePct: number;
  confidence: number;
  category: string;
  description: string;
};

type Feature = {
  type: "Feature";
  properties: Record<string, string | number>;
  geometry: {
    type: "Polygon";
    coordinates: number[][][];
  };
};

const sites: Site[] = [
  {
    id: "TL-001",
    name: "Naya Raipur South",
    region: "Raipur, Chhattisgarh",
    coordinates: [81.646, 21.193],
    status: "Confirmed",
    firstChange: "2025-11-18",
    areaHa: 18.4,
    changePct: 31.7,
    confidence: 0.94,
    category: "Built-up expansion",
    description: "Persistent surface change consistent with new built-up footprint.",
  },
  {
    id: "TL-002",
    name: "Gangrel Reservoir Edge",
    region: "Dhamtari, Chhattisgarh",
    coordinates: [81.952, 20.854],
    status: "Provisional",
    firstChange: "2026-01-14",
    areaHa: 7.8,
    changePct: 14.2,
    confidence: 0.82,
    category: "Water / land-cover transition",
    description: "Seasonal and persistent signals require contextual review.",
  },
  {
    id: "TL-003",
    name: "Korba Industrial Fringe",
    region: "Korba, Chhattisgarh",
    coordinates: [82.690, 22.359],
    status: "Confirmed",
    firstChange: "2025-09-27",
    areaHa: 26.1,
    changePct: 42.5,
    confidence: 0.91,
    category: "Industrial expansion",
    description: "Multi-date structural change detected across an industrial fringe.",
  },
  {
    id: "TL-004",
    name: "Kharun Corridor",
    region: "Raipur, Chhattisgarh",
    coordinates: [81.610, 21.258],
    status: "Transient",
    firstChange: "2026-02-09",
    areaHa: 11.2,
    changePct: 9.6,
    confidence: 0.58,
    category: "Vegetation / moisture",
    description: "Signal weakens on subsequent observations; likely contextual variation.",
  },
  {
    id: "TL-005",
    name: "Raipur East Works",
    region: "Raipur, Chhattisgarh",
    coordinates: [81.702, 21.256],
    status: "Tentative",
    firstChange: "2026-03-16",
    areaHa: 5.6,
    changePct: 7.8,
    confidence: 0.64,
    category: "Surface disturbance",
    description: "Candidate change with incomplete persistence evidence.",
  },
  {
    id: "TL-006",
    name: "Bhilai Peripheral Block",
    region: "Durg, Chhattisgarh",
    coordinates: [81.331, 21.185],
    status: "Insufficient Data",
    firstChange: "—",
    areaHa: 3.1,
    changePct: 4.2,
    confidence: 0.41,
    category: "Unresolved",
    description: "Available observations do not support a reliable change date.",
  },
];

const navItems = [
  ["overview", "Overview"],
  ["search", "Semantic Search"],
  ["change", "Change Analysis"],
  ["similar", "Similar Sites"],
  ["review", "Review Queue"],
  ["temporal", "History / Timelapse"],
  ["watch", "Watch Sets"],
  ["priority", "Priority Zones"],
  ["provenance", "Provenance / Audit"],
  ["models", "Models / Data"],
  ["system", "System Status"],
];



const square = (x: number, y: number, size: number): number[][] => [
  [x, y],
  [x + size, y],
  [x + size, y + size],
  [x, y + size],
  [x, y],
];

const mapFeatures: Feature[] = sites.map((site, i) => {
  const [lon, lat] = site.coordinates;
  const size = i % 3 === 0 ? 0.012 : 0.009;
  return {
    type: "Feature",
    properties: {
      id: site.id,
      status: site.status,
      name: site.name,
    },
    geometry: {
      type: "Polygon",
      coordinates: [square(lon - size / 2, lat - size / 2, size)],
    },
  };
});

function Badge({ status }: { status: SiteStatus }) {
  return <span className={`badge badge-${status.toLowerCase().replaceAll(" ", "-")}`}>{status}</span>;
}

function Metric({ value, label, detail }: { value: string; label: string; detail?: string }) {
  return (
    <div className="metric">
      <div className="metric-value">{value}</div>
      <div className="metric-label">{label}</div>
      {detail && <div className="metric-detail">{detail}</div>}
    </div>
  );
}

function MapWorkspace({
  selected,
  onSelect,
}: {
  selected: Site;
  onSelect: (site: Site) => void;
}) {
  const mapNode = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

  useEffect(() => {
    if (!mapNode.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: mapNode.current,
      attributionControl: false,
      center: [81.66, 21.23],
      zoom: 9.3,
      style: {
        version: 8,
        sources: {},
        layers: [
          {
            id: "background",
            type: "background",
            paint: {
              "background-color": "#11181c",
            },
          },
        ],
      },
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");

    map.on("load", () => {
      map.addSource("aois", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: mapFeatures,
        },
      });

      map.addLayer({
        id: "aoi-fill",
        type: "fill",
        source: "aois",
        paint: {
          "fill-color": [
            "match",
            ["get", "status"],
            "Confirmed",
            "#9bbd91",
            "Provisional",
            "#c2a969",
            "Tentative",
            "#b78b67",
            "Transient",
            "#718491",
            "#59666d",
          ],
          "fill-opacity": 0.34,
        },
      });

      map.addLayer({
        id: "aoi-line",
        type: "line",
        source: "aois",
        paint: {
          "line-color": "#d5dfd8",
          "line-width": 1.2,
          "line-opacity": 0.75,
        },
      });

      map.on("click", "aoi-fill", (event) => {
        const feature = event.features?.[0];
        const id = feature?.properties?.id;
        const site = sites.find((entry) => entry.id === id);
        if (site) onSelect(site);
      });

      map.on("mouseenter", "aoi-fill", () => {
        map.getCanvas().style.cursor = "pointer";
      });

      map.on("mouseleave", "aoi-fill", () => {
        map.getCanvas().style.cursor = "";
      });

      new maplibregl.Marker({ color: "#e3ebe4" })
        .setLngLat(selected.coordinates)
        .setPopup(
          new maplibregl.Popup({ offset: 18 }).setHTML(
            `<strong>${selected.name}</strong><br/>${selected.status}`
          )
        )
        .addTo(map);

      map.fitBounds(
        [
          [81.28, 21.12],
          [82.72, 22.44],
        ],
        { padding: 40, duration: 0 }
      );
    });

    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, [onSelect, selected]);

  return <div className="map-root" ref={mapNode} />;
}

function Overview({
  selected,
  setSelected,
}: {
  selected: Site;
  setSelected: (site: Site) => void;
}) {
  const queue = sites.filter((site) => site.status !== "Confirmed");

  return (
    <div className="workspace">
      <div className="workspace-main">
        <div className="section-head">
          <div>
            <div className="eyebrow">ANALYST WORKSPACE</div>
            <h1>Multi-temporal Earth Observation</h1>
            <p>Semantic retrieval, change evidence and review in one local workstation.</p>
          </div>
          <div className="toolbar">
            <span className="pill">DEMO DATA</span>
            <span className="pill pill-green">OFFLINE READY</span>
          </div>
        </div>

        <div className="commandbar">
          <span className="command-icon">⌕</span>
          <input
            defaultValue="Find persistent built-up expansion since September 2025"
            aria-label="Semantic search"
          />
          <span className="chip">persistent</span>
          <span className="chip">built-up</span>
          <span className="chip">since Sep 2025</span>
          <button className="button-primary">Search</button>
        </div>

        <div className="metrics-row">
          <Metric value="250" label="Indexed frames" detail="Sentinel-2 L2A" />
          <Metric value="06" label="Active AOIs" detail="5 regions + corridor" />
          <Metric value="18" label="Change candidates" detail="5 awaiting review" />
          <Metric value="0.91" label="Median confidence" detail="reviewed cases" />
          <Metric value="100%" label="Local execution" detail="no external dependency" />
        </div>

        <div className="map-panel">
          <div className="panel-header">
            <div>
              <strong>Spatial evidence</strong>
              <span className="muted"> AOI layer · detected events · reference context</span>
            </div>
            <div className="legend">
              <span><i className="legend-dot confirmed" /> confirmed</span>
              <span><i className="legend-dot provisional" /> provisional</span>
              <span><i className="legend-dot tentative" /> candidate</span>
            </div>
          </div>
          <div className="map-shell">
            <MapWorkspace selected={selected} onSelect={setSelected} />
            <div className="map-readout">
              <div>VIEW</div>
              <strong>Central Chhattisgarh</strong>
              <span>20.8–22.4°N · 81.2–82.8°E</span>
            </div>
          </div>
        </div>
      </div>

      <aside className="evidence-rail">
        <div className="rail-title">Selected evidence</div>
        <div className="evidence-site">
          <div className="eyebrow">{selected.id}</div>
          <h2>{selected.name}</h2>
          <p>{selected.region}</p>
          <Badge status={selected.status} />
        </div>

        <div className="evidence-block">
          <div className="block-label">EARLIEST SUPPORTED CHANGE</div>
          <div className="date-large">{selected.firstChange}</div>
          <div className="muted">Supported by multi-date observation sequence.</div>
        </div>

        <div className="evidence-grid">
          <Metric value={`${selected.changePct}%`} label="Observed area change" />
          <Metric value={`${selected.areaHa}`} label="Affected area · ha" />
        </div>

        <div className="confidence">
          <div className="block-label">EVIDENCE CONFIDENCE</div>
          <div className="confidence-line">
            <strong>{Math.round(selected.confidence * 100)}%</strong>
            <div className="confidence-track">
              <div style={{ width: `${selected.confidence * 100}%` }} />
            </div>
          </div>
        </div>

        <div className="gate-list">
          {[
            ["Quality", "Pass"],
            ["Geometry", "Pass"],
            ["Radiometry", "Pass"],
            ["Season / Context", selected.status === "Transient" ? "Review" : "Pass"],
            ["Size / Shape", "Pass"],
            ["Persistence", selected.status === "Confirmed" ? "Pass" : "Review"],
          ].map(([label, state]) => (
            <div className="gate" key={label}>
              <span>{label}</span>
              <strong className={state === "Pass" ? "pass" : "review"}>{state}</strong>
            </div>
          ))}
        </div>

        <p className="evidence-description">{selected.description}</p>

        <button className="button-wide" onClick={() => setSelected(selected)}>
          Open change analysis →
        </button>

        <div className="source-note">
          <strong>PROVENANCE</strong>
          <span>Sentinel-2 L2A · local COG index · deterministic demo layer</span>
        </div>
      </aside>

      <div className="bottom-table">
        <div className="panel-header">
          <strong>Review queue</strong>
          <span className="muted">{queue.length} items require analyst attention</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Site</th>
              <th>Category</th>
              <th>First supported</th>
              <th>Confidence</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {queue.slice(0, 4).map((site) => (
              <tr key={site.id} onClick={() => setSelected(site)}>
                <td><strong>{site.name}</strong><small>{site.id}</small></td>
                <td>{site.category}</td>
                <td>{site.firstChange}</td>
                <td>{Math.round(site.confidence * 100)}%</td>
                <td><Badge status={site.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SearchView({ setSelected }: { setSelected: (site: Site) => void }) {
  const [query, setQuery] = useState("persistent built-up expansion since September 2025");
  const [active, setActive] = useState<string[]>(["persistent", "built-up", "since Sep 2025"]);

  const results = useMemo(() => {
    const q = query.toLowerCase();
    return sites.filter(
      (site) =>
        site.name.toLowerCase().includes(q.split(" ")[0]) ||
        site.category.toLowerCase().includes("built") ||
        site.status === "Confirmed" ||
        site.status === "Provisional"
    );
  }, [query]);

  return (
    <div className="content-page">
      <div className="section-head">
        <div>
          <div className="eyebrow">SEMANTIC RETRIEVAL</div>
          <h1>Evidence-aware search</h1>
          <p>Natural-language retrieval expressed as auditable evidence constraints.</p>
        </div>
      </div>

      <div className="search-box">
        <input value={query} onChange={(event) => setQuery(event.target.value)} />
        <button className="button-primary">Run retrieval</button>
      </div>

      <div className="chips-row">
        {["persistent", "built-up", "since Sep 2025", "large footprint", "near roads"].map((chip) => {
          const on = active.includes(chip);
          return (
            <button
              className={`filter-chip ${on ? "active" : ""}`}
              key={chip}
              onClick={() => setActive(on ? active.filter((entry) => entry !== chip) : [...active, chip])}
            >
              {chip} {on ? "×" : "+"}
            </button>
          );
        })}
      </div>

      <div className="warning-box">
        <strong>Evidence boundary:</strong> “near roads” is currently <u>Unverifiable</u> in the demo layer because the reference transport dataset is not indexed. No ranking is produced for that condition.
      </div>

      <div className="result-list">
        {results.map((site, index) => (
          <button
            className="result-row"
            key={site.id}
            onClick={() => setSelected(site)}
          >
            <span className="rank">{String(index + 1).padStart(2, "0")}</span>
            <span className="result-main">
              <strong>{site.name}</strong>
              <small>{site.region} · {site.category}</small>
              <span>{site.description}</span>
            </span>
            <span className="result-date">{site.firstChange}</span>
            <span className="result-confidence">{Math.round(site.confidence * 100)}%</span>
            <Badge status={site.status} />
            <span className="arrow">→</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function ChangeView({ selected }: { selected: Site }) {
  const dates = ["2025-09-27", "2025-11-18", "2026-01-14", "2026-03-16", "2026-05-21", "2026-08-19"];

  return (
    <div className="content-page">
      <div className="section-head">
        <div>
          <div className="eyebrow">{selected.id} · CHANGE ANALYSIS</div>
          <h1>{selected.name}</h1>
          <p>{selected.description}</p>
        </div>
        <Badge status={selected.status} />
      </div>

      <div className="before-after">
        <div className="imagery">
          <div className="imagery-label">BEFORE · {dates[0]}</div>
          <div className="sat-image before"><span>ARCHIVE FRAME</span><b>Surface context</b></div>
        </div>
        <div className="imagery">
          <div className="imagery-label">AFTER · {dates[5]}</div>
          <div className="sat-image after"><span>ARCHIVE FRAME</span><b>Detected structural signal</b></div>
        </div>
      </div>

      <div className="change-grid">
        <div className="panel">
          <div className="panel-header"><strong>Temporal evidence</strong><span className="muted">Earliest supported observation highlighted</span></div>
          <div className="timeline">
            {dates.map((date) => (
              <div className={`timeline-point ${date === selected.firstChange ? "first" : ""}`} key={date}>
                <span />
                <strong>{date}</strong>
                <small>{date === selected.firstChange ? "FIRST SUPPORTED" : "observed"}</small>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-header"><strong>Quantified change</strong></div>
          <div className="stat-table">
            <div><span>Affected area</span><strong>{selected.areaHa} ha</strong></div>
            <div><span>Relative change</span><strong>{selected.changePct}%</strong></div>
            <div><span>Confidence</span><strong>{Math.round(selected.confidence * 100)}%</strong></div>
            <div><span>Persistence</span><strong>{selected.status === "Confirmed" ? "4 / 5 frames" : "2 / 5 frames"}</strong></div>
            <div><span>Timing type</span><strong>Supported</strong></div>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header"><strong>False-change suppression gates</strong><span className="muted">Candidate is only promoted when evidence survives all relevant gates</span></div>
        <div className="gate-grid-large">
          {[
            ["Image quality", "PASS", "Cloud / haze screen within threshold"],
            ["Geometry", "PASS", "Co-registration residual acceptable"],
            ["Radiometry", "PASS", "Scene statistics consistent"],
            ["Season / context", selected.status === "Transient" ? "REVIEW" : "PASS", "Temporal context requires analyst interpretation"],
            ["Size / shape", "PASS", "Object geometry is coherent"],
            ["Persistence", selected.status === "Confirmed" ? "PASS" : "REVIEW", "Repeated observation support"],
          ].map(([title, state, detail]) => (
            <div className="gate-card" key={title}>
              <div className="gate-state">{state}</div>
              <strong>{title}</strong>
              <span>{detail}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ReviewView({ setSelected }: { setSelected: (site: Site) => void }) {
  const [items, setItems] = useState(sites.filter((site) => site.status !== "Confirmed"));

  const action = (id: string, status: SiteStatus) => {
    setItems(items.map((item) => (item.id === id ? { ...item, status } : item)));
    const next = sites.find((site) => site.id === id);
    if (next) setSelected({ ...next, status });
  };

  return (
    <div className="content-page">
      <div className="section-head">
        <div>
          <div className="eyebrow">ANALYST REVIEW</div>
          <h1>Review queue</h1>
          <p>Human confirmation remains the final authority for promotion of change candidates.</p>
        </div>
      </div>

      <div className="review-table panel">
        <table>
          <thead>
            <tr><th>Candidate</th><th>Evidence</th><th>First supported</th><th>Status</th><th>Analyst action</th></tr>
          </thead>
          <tbody>
            {items.map((site) => (
              <tr key={site.id}>
                <td><button className="plain-link" onClick={() => setSelected(site)}>{site.name}</button><small>{site.id}</small></td>
                <td>{site.category}<br/><small>{Math.round(site.confidence * 100)}% confidence</small></td>
                <td>{site.firstChange}</td>
                <td><Badge status={site.status} /></td>
                <td>
                  <div className="actions">
                    <button onClick={() => action(site.id, "Confirmed")}>Confirm</button>
                    <button onClick={() => action(site.id, "Rejected")}>Reject</button>
                    <button onClick={() => action(site.id, "Provisional")}>Relabel</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="audit-callout">
        Every analyst decision should retain reviewer, timestamp, previous state, new state and evidence references.
      </div>
    </div>
  );
}

function SimpleView({ title, eyebrow, body, rows }: { title: string; eyebrow: string; body: string; rows: string[] }) {
  return (
    <div className="content-page">
      <div className="section-head">
        <div>
          <div className="eyebrow">{eyebrow}</div>
          <h1>{title}</h1>
          <p>{body}</p>
        </div>
      </div>
      <div className="panel document-panel">
        {rows.map((row, i) => (
          <div className="document-row" key={row}>
            <span>{String(i + 1).padStart(2, "0")}</span>
            <strong>{row}</strong>
            <em>Available locally</em>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState("overview");
  const [selected, setSelected] = useState(sites[0]);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const title = navItems.find(([id]) => id === view)?.[1] ?? "Overview";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">TL</div>
          <div>
            <strong>TerraLens</strong>
            <span>Semantic EO Intelligence</span>
          </div>
        </div>
        <div className="topbar-center">
          <span className="system-dot" />
          LOCAL WORKSTATION
          <span className="separator">/</span>
          DEMO DATASET
          <span className="separator">/</span>
          {title.toUpperCase()}
        </div>
        <div className="topbar-right">
          <span className="sync">● AIR-GAPPED READY</span>
          <button className="avatar">AT</button>
        </div>
      </header>

      <div className="shell">
        <aside className={`sidebar ${sidebarOpen ? "" : "collapsed"}`}>
          <button className="collapse" onClick={() => setSidebarOpen(!sidebarOpen)}>{sidebarOpen ? "‹" : "›"}</button>
          <div className="nav-section">
            <div className="nav-label">WORKSPACE</div>
            {navItems.slice(0, 6).map(([id, label]) => (
              <button className={`nav-item ${view === id ? "active" : ""}`} key={id} onClick={() => setView(id)}>
                <span className="nav-icon">{["⌂", "⌕", "↗", "◌", "◍", "◷"][navItems.findIndex(([x]) => x === id)]}</span>
                {sidebarOpen && label}
              </button>
            ))}
          </div>
          <div className="nav-section">
            <div className="nav-label">OPERATIONS</div>
            {navItems.slice(6).map(([id, label]) => (
              <button className={`nav-item ${view === id ? "active" : ""}`} key={id} onClick={() => setView(id)}>
                <span className="nav-icon">▦</span>
                {sidebarOpen && label}
              </button>
            ))}
          </div>
          {sidebarOpen && (
            <div className="sidebar-footer">
              <div className="footer-label">INGESTION</div>
              <strong>250 frames indexed</strong>
              <span>5 AOIs · local storage</span>
              <div className="progress"><div style={{ width: "100%" }} /></div>
            </div>
          )}
        </aside>

        <main className="main">
          {view === "overview" && <Overview selected={selected} setSelected={setSelected} />}
          {view === "search" && <SearchView setSelected={setSelected} />}
          {view === "change" && <ChangeView selected={selected} />}
          {view === "review" && <ReviewView setSelected={setSelected} />}
          {view === "temporal" && (
            <SimpleView
              title="History / Timelapse"
              eyebrow="MULTI-TEMPORAL"
              body="Step through archived observations and isolate the earliest supported change event."
              rows={["2025-09-27 · baseline", "2025-11-18 · first supported change", "2026-01-14 · persistence check", "2026-03-16 · structural persistence", "2026-05-21 · confirmed", "2026-08-19 · latest observation"]}
            />
          )}
          {view === "similar" && (
            <SimpleView
              title="Similar Sites"
              eyebrow="IMAGE-TO-IMAGE DISCOVERY"
              body="Retrieve visually and semantically related AOIs for analyst comparison."
              rows={["Naya Raipur South · 0.94 similarity", "Korba Industrial Fringe · 0.89 similarity", "Raipur East Works · 0.81 similarity", "Gangrel Reservoir Edge · 0.73 similarity"]}
            />
          )}
          {view === "watch" && (
            <SimpleView
              title="Watch Sets"
              eyebrow="MONITORING"
              body="Persistent AOIs and recurring retrieval definitions retained as local analyst configurations."
              rows={["Urban expansion — Naya Raipur", "Industrial fringe — Korba", "Water-edge transitions — Gangrel", "Surface disturbance — Raipur East"]}
            />
          )}
          {view === "priority" && (
            <SimpleView
              title="Priority Zones"
              eyebrow="TRIAGE"
              body="Spatial candidates organized by evidence strength, persistence and analyst review state."
              rows={["Confirmed persistent change", "High-confidence provisional change", "Tentative candidates", "Insufficient-data areas"]}
            />
          )}
          {view === "provenance" && (
            <SimpleView
              title="Provenance / Audit"
              eyebrow="TRACEABILITY"
              body="Every observation should remain linked to source frame, processing chain, model/data version and analyst action."
              rows={["Observation source · Sentinel-2 L2A", "Processing chain · local COG normalization", "Change engine · deterministic demo evidence", "Decision history · reviewer action log", "Confidence · evidence-derived, not inferred from UI state"]}
            />
          )}
          {view === "models" && (
            <SimpleView
              title="Models / Data"
              eyebrow="MODEL & DATA MANIFEST"
              body="Local manifests make model provenance and dataset identity visible to the analyst."
              rows={["Sentinel-2 L2A · 250 demo frames", "Encoder candidates · local model inventory", "AOI registry · 6 review sites", "Reference layers · local GIS subset", "No external model/API calls in demo mode"]}
            />
          )}
          {view === "system" && (
            <SimpleView
              title="System Status"
              eyebrow="LOCAL INFRASTRUCTURE"
              body="Runtime health and capability boundaries for an offline deployment."
              rows={["Frontend · operational", "Demo data layer · operational", "Map workspace · operational", "API connection · optional / disabled in demo mode", "PostGIS + pgvector · backend integration pending", "External network dependency · none for demo UI"]}
            />
          )}
        </main>
      </div>
    </div>
  );
}
