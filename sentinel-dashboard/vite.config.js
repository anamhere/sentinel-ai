import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    cors: true,
    proxy: {
      // ── API endpoints go through Vite proxy ──────────────────────────────
      // This means in App.jsx API_BASE = "" and fetch("/stats") works fine.
      "/stats":       { target: "http://localhost:8000", changeOrigin: true },
      "/history":     { target: "http://localhost:8000", changeOrigin: true },
      "/alerts":      { target: "http://localhost:8000", changeOrigin: true },
      "/logs":        { target: "http://localhost:8000", changeOrigin: true },
      "/health":      { target: "http://localhost:8000", changeOrigin: true },
      "/refresh":     { target: "http://localhost:8000", changeOrigin: true },
      "/alert_files": { target: "http://localhost:8000", changeOrigin: true },
      // ── /video is intentionally NOT proxied ──────────────────────────────
      // Vite's http-proxy buffers multipart/x-mixed-replace streams which
      // causes visible lag and frame stutter. The React app sends the stream
      // request directly to http://localhost:8000/video via STREAM_BASE.
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
