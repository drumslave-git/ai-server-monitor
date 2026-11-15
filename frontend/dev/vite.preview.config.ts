import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev harness for the Logs page: `vite --config dev/vite.preview.config.ts`, with
// scripts/dev/trace_demo.py running on :8099.
export default defineConfig({
  root: ".",
  plugins: [react()],
  server: { port: 5188, strictPort: true, proxy: { "/api": "http://127.0.0.1:8099" } },
});
