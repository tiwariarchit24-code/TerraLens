/** Frontend configuration. The only place environment variables are read. */
const env = import.meta.env;

export const config = {
  /** "demo": self-contained demo data layer; "api": the TerraLens FastAPI backend */
  dataMode: (env.VITE_DATA_MODE === "api" ? "api" : "demo") as "demo" | "api",
  apiBase: (env.VITE_API_BASE as string | undefined) ?? "/api",
  /** reference "today" for relative dates ("since January") in demo mode */
  referenceDate: "2026-09-30",
  appVersion: "0.9.0",
  /** static assets (frames, overlays) are resolved relative to the deployed base */
  asset: (p: string) => `${env.BASE_URL}${p.replace(/^\//, "")}`,
};
