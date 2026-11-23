// Browser tests for the Law check page. They start the local Law check server
// (scripts/law_check_local.py): the real server part, keyed MCP and verifier over a small in-memory
// catalogue, with a scripted stand-in for an AI provider. No key, no secret and no outside call.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const venvPython = path.join(root, ".venv/bin/python");
const python = process.env.LAW_CHECK_PYTHON || (fs.existsSync(venvPython) ? venvPython : "python");
const port = Number(process.env.LAW_CHECK_PORT || 8772);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./specs",
  timeout: 45_000,
  expect: { timeout: 12_000 },
  // One test at a time: every test shares the one local server and its plan's limits, and the
  // whole run takes well under a minute per browser.
  workers: 1,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: `${python} -m scripts.law_check_local --port ${port}`,
    cwd: root,
    url: `${baseURL}/agents/`,
    reuseExistingServer: !process.env.CI,
    timeout: 90_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "firefox", use: { ...devices["Desktop Firefox"], viewport: { width: 1280, height: 800 } } },
    { name: "webkit", use: { ...devices["Desktop Safari"], viewport: { width: 1280, height: 800 } } },
    {
      // The same page with the system's own colours forced on it: states drawn with a background
      // alone vanish here, so the smoke tests run again in this mode.
      name: "forced-colours",
      grep: /@smoke/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 }, forcedColors: "active" },
    },
  ],
});
