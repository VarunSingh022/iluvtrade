import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The dev server proxies the API so the browser sees one origin and the
    // session cookie is first-party. Without this the HttpOnly cookie would be
    // cross-site and dropped, and development would need a different auth path
    // from production — which is how a CSRF hole gets introduced by accident.
    proxy: {
      "/api": {
        target: process.env.ILUVTRADE_API_ORIGIN ?? "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
