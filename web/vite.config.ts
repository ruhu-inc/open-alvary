import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { docsFiles } from "./docs-files";
export default defineConfig({
  plugins: [react(), docsFiles()],
  // The public site and the developer console (/console) are two pages of one build.
  build: { rollupOptions: { input: { main: "index.html", console: "console.html" } } },
  server: {
    port: 5178,
    strictPort: true,
    // The API service (api.service:app) serves the website's data path, downloads and the legacy API.
    proxy: {
      "/site": "http://127.0.0.1:8018",
      "/downloads": "http://127.0.0.1:8018",
      "/api": "http://127.0.0.1:8018",
    },
  },
});
