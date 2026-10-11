// The public site's document page, built for the review workbench's preview (served by the
// workbench at /preview/<document>, assets under /static/public-preview/). Fixed file names
// keep the committed build stable.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  base: "/static/public-preview/",
  build: {
    outDir: "../workbench/static/public-preview",
    emptyOutDir: true,
    rollupOptions: {
      input: "preview.html",
      output: {
        entryFileNames: "preview.js",
        chunkFileNames: "[name].js",
        assetFileNames: "[name][extname]",
      },
    },
  },
});
