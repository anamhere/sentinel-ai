import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "0.0.0.0",   // accessible on LAN/mobile
    cors: true,
    proxy: {
      // optional: proxy /api to backend during dev
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
