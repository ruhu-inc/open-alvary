// The public site's document page, built for the review workbench's preview (served by the
// workbench at /preview/<document>, assets under /static/public-preview/). Fixed file names
// keep the committed build stable.
import { cpSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const OUT = "../workbench/static/public-preview";

export default defineConfig({
  plugins: [
    react(),
    {
      // Of the site's public files only the licence notices ship with the preview: it uses no icons
      // or pictures, but bundles fonts and libraries whose notices must travel with them.
      name: "licence-notices",
      apply: "build",
      closeBundle() {
        for (const dir of ["font-licenses", "licenses"]) cpSync(`public/${dir}`, `${OUT}/${dir}`, { recursive: true });
      },
    },
  ],
  base: "/static/public-preview/",
  publicDir: false,
  build: {
    outDir: OUT,
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
