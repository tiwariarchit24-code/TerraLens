import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" keeps every asset URL relative, so the production build can be served from
// any path (static host, file server, or mounted under the FastAPI app later).
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1500 },
  server: { host: "127.0.0.1", port: 5173 },
  test: { environment: "jsdom", globals: false },
} as never);
