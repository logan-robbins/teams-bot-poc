import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Alfred web dev server.
// Proxies /sink/* to the Python FastAPI transcript sink so the browser
// talks to a single origin in dev. Override SINK_URL in .env.local to
// retarget (defaults to http://127.0.0.1:8765).
const SINK_URL = process.env.SINK_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/sink": {
        target: SINK_URL,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/sink/, ""),
      },
    },
  },
  build: {
    target: "es2022",
    sourcemap: true,
  },
});
