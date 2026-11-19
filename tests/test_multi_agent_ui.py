"""Frontend tests for multi-agent review UI (Phase 0 join bar, hash router, claims, Agents & Operators)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "workbench" / "static" / "app.js"
APP_CSS = ROOT / "workbench" / "static" / "app.css"
NODE = shutil.which("node")


def _run_node(script: str) -> dict:
    assert NODE is not None, "node is required for frontend JS unit tests"
    proc = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"node failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _harness_preamble() -> str:
    """Load workbench/static/app.js in a Node VM with minimal DOM/storage fakes (without running start())."""
    return """
const fs = require("fs");
const vm = require("vm");

function makeStorage() {
  const map = new Map();
  return {
    get length() { return map.size; },
    key(i) { return Array.from(map.keys())[i] ?? null; },
    getItem(k) { return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { map.set(String(k), String(v)); },
    removeItem(k) { map.delete(k); },
    clear() { map.clear(); },
    _dump() { return Object.fromEntries(map.entries()); },
  };
}

class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.children = [];
    this.listeners = {};
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.style = {};
    this.classList = {
      add() {},
      remove() {},
      toggle() {},
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  append(...nodes) {
    for (const n of nodes) {
      if (typeof n === "string") this.children.push({ textContent: n, children: [] });
      else if (n) this.children.push(n);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }
  remove() { this.removed = true; }
  focus() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
  allText() {
    const parts = [];
    if (this.textContent) parts.push(this.textContent);
    for (const c of this.children) {
      if (c && typeof c.allText === "function") parts.push(c.allText());
      else if (c && c.textContent) parts.push(c.textContent);
    }
    return parts.join(" ").replace(/\\s+/g, " ").trim();
  }
  findButtons() {
    const out = [];
    if (this.tagName === "BUTTON") out.push(this);
    for (const c of this.children) {
      if (c && typeof c.findButtons === "function") out.push(...c.findButtons());
    }
    return out;
  }
  findInputs() {
    const out = [];
    if (this.tagName === "INPUT" || this.tagName === "SELECT") out.push(this);
    for (const c of this.children) {
      if (c && typeof c.findInputs === "function") out.push(...c.findInputs());
    }
    return out;
  }
}

const mainEl = new FakeNode("main");
const crumbsEl = new FakeNode("nav");
const whoEl = new FakeNode("div");
const toastEl = new FakeNode("div");
const elementsById = { main: mainEl, crumbs: crumbsEl, who: whoEl, toast: toastEl };

const fetchCalls = [];
let fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({}) });
const intervals = [];

const sandbox = {
  console,
  URLSearchParams,
  Date,
  Math,
  JSON,
  Set,
  Map,
  Array,
  Object,
  String,
  Number,
  Boolean,
  RegExp,
  Promise,
  Error,
  encodeURIComponent,
  decodeURIComponent,
  structuredClone,
  Node: FakeNode,
  navigator: { platform: "MacIntel", userAgent: "Mac" },
  localStorage: makeStorage(),
  sessionStorage: makeStorage(),
  location: { hash: "#/", search: "", replace() {} },
  history: { replaceState() {} },
  confirm: () => true,
  prompt: () => null,
  setTimeout,
  clearTimeout,
  setInterval: (fn, ms) => { const id = { fn, ms, cleared: false }; intervals.push(id); return id; },
  clearInterval: (id) => { if (id) id.cleared = true; },
  fetch: async (path, options) => {
    fetchCalls.push({ path, options });
    return fetchHandler(path, options);
  },
  document: {
    title: "",
    body: new FakeNode("body"),
    documentElement: { clientWidth: 1200 },
    createElement: (tag) => new FakeNode(tag),
    createElementNS: (_ns, tag) => new FakeNode(tag),
    createTextNode: (text) => ({ textContent: String(text), children: [] }),
    getElementById: (id) => elementsById[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
  },
  window: {
    scrollTo() {},
    addEventListener() {},
    ReviewEditor: { RANKS: [1, 2, 3, 4], commands: {} },
  },
};

const raw = fs.readFileSync("workbench/static/app.js", "utf8");
const source = raw.replace(/\\nstart\\(\\);\\s*$/, "\\nthis.app = app;\\n");
vm.runInNewContext(source, sandbox, { filename: "workbench/static/app.js" });
"""


def test_app_js_syntax_and_dom_only():
    if NODE is not None:
        proc = subprocess.run([NODE, "--check", str(APP_JS)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr

    src = APP_JS.read_text(encoding="utf-8")
    # Only the pre-existing server-escaped machine_html line uses innerHTML.
    inner_html_lines = [line.strip() for line in src.splitlines() if "innerHTML" in line]
    assert len(inner_html_lines) == 1
    assert "page.machine_html" in inner_html_lines[0]

    # Verify CSS includes the new multi-agent styles.
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".claim-banner" in css
    assert ".claim-fields" in css


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_phase0_refresh_join_all_leading_characters_and_guards():
    result = _run_node(
        _harness_preamble()
        + """
function runCase(first, beforeOverrides = {}, viewOverrides = {}) {
  const slot = new FakeNode("div");
  const view = {
    joinSlot: slot,
    join: null,
    joinDismissed: false,
    page: { previous_end: { page: 11, text: "the Minister shall prescribe", ends_sentence: false, hyphen: false, ...beforeOverrides } },
    units: [first],
    ...viewOverrides,
  };
  sandbox.refreshJoin(view);
  return {
    text: slot.allText(),
    buttons: slot.findButtons().map((b) => b.textContent),
    joinAfterRender: view.join,
    slot,
    view,
  };
}

const lowerAscii = runCase({ kind: "paragraph", text: "regulations for this Act." });
const lowerAccent = runCase({ kind: "paragraph", text: "élaboré conformément à la loi." });
const upperAscii = runCase({ kind: "paragraph", text: "Ministry of Justice shall publish." });
const upperAccent = runCase({ kind: "paragraph", text: "Également applicable aux contrats." });
const digitStart = runCase({ kind: "paragraph", text: "2024 as amended by Parliament." });
const heading = runCase({ kind: "heading", text: "continuation title" });
const listItem = runCase({ kind: "list_item", text: "continuation item" });
const marked = runCase({ kind: "paragraph", marker: "(1)", text: "continuation marked" });
const endsSentence = runCase({ kind: "paragraph", text: "continuing" }, { ends_sentence: true });

// Clicking "Join with a space" sets view.join only after reviewer clicks.
const clickBtn = upperAscii.slot.findButtons().find((b) => b.textContent === "Join with a space");
clickBtn.listeners.click[0]();

console.log(JSON.stringify({
  lowerAscii: { text: lowerAscii.text, buttons: lowerAscii.buttons, join: lowerAscii.joinAfterRender },
  lowerAccent: { text: lowerAccent.text, buttons: lowerAccent.buttons, join: lowerAccent.joinAfterRender },
  upperAscii: { text: upperAscii.text, buttons: upperAscii.buttons, join: upperAscii.joinAfterRender, joinAfterClick: upperAscii.view.join },
  upperAccent: { text: upperAccent.text, buttons: upperAccent.buttons },
  digitStart: { text: digitStart.text, buttons: digitStart.buttons },
  heading: heading.text,
  listItem: listItem.text,
  marked: marked.text,
  endsSentence: endsSentence.text,
}));
"""
    )
    assert "Join with a space" in result["lowerAscii"]["buttons"]
    assert result["lowerAscii"]["join"] is None
    assert "Join with a space" in result["lowerAccent"]["buttons"]
    assert result["lowerAccent"]["join"] is None

    assert "The same paragraph?" in result["upperAscii"]["text"]
    assert result["upperAscii"]["join"] is None
    assert result["upperAscii"]["joinAfterClick"] == "space"

    assert "The same paragraph?" in result["upperAccent"]["text"]
    assert "The same paragraph?" in result["digitStart"]["text"]

    assert result["heading"] == ""
    # A list item with no number of its own may run on from the page before, as a paragraph may
    # (3294b9d8); one with its own marker may not.
    assert "The same paragraph?" in result["listItem"]
    assert result["marked"] == ""
    assert result["endsSentence"] == ""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_hash_router_query_stripping_and_per_tab_session_label():
    result = _run_node(
        _harness_preamble()
        + """
const q1 = sandbox.parseHash("#/queue?label=claude-m1-07");
const q2 = sandbox.parseHash("#/claim?label=claude-m1-07&task=status");
const q3 = sandbox.parseHash("#/doc/rw-law-1/page/3?foo=1");

const capturedHash = sandbox.captureTabLabel("#/claim?label=claude-m1-07", "");
const localDumpAfterHash = sandbox.localStorage._dump();
const sessionDumpAfterHash = sandbox.sessionStorage._dump();

// Simulate sign-in redirect stripping query parameters: sessionStorage still has the tab's label.
const afterRedirect = sandbox.captureTabLabel("#/claim", "");

// Simulate location.search label capture.
sandbox.sessionStorage.clear();
const capturedSearch = sandbox.captureTabLabel("#/", "?label=codex-m2-03");

console.log(JSON.stringify({
  q1Parts: q1.parts,
  q1Label: q1.query.get("label"),
  q2Parts: q2.parts,
  q2Label: q2.query.get("label"),
  q2Task: q2.query.get("task"),
  q3Parts: q3.parts,
  capturedHash,
  localDumpAfterHash,
  sessionDumpAfterHash,
  afterRedirect,
  capturedSearch,
}));
"""
    )
    assert result["q1Parts"] == ["queue"]
    assert result["q1Label"] == "claude-m1-07"
    assert result["q2Parts"] == ["claim"]
    assert result["q2Label"] == "claude-m1-07"
    assert result["q2Task"] == "status"
    assert result["q3Parts"] == ["doc", "rw-law-1", "page", "3"]
    assert result["capturedHash"] == "claude-m1-07"
    assert result["localDumpAfterHash"] == {}  # NEVER stored in localStorage
    assert result["sessionDumpAfterHash"] == {"oa-tab-label": "claude-m1-07"}
    assert result["afterRedirect"] == "claude-m1-07"
    assert result["capturedSearch"] == "codex-m2-03"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_claim_tokens_grace_window_and_api_header_wiring():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  const now = Date.parse("2026-11-20T12:00:00Z");
  const expired2h = new Date(now - 2 * 3600 * 1000).toISOString();
  const expired25h = new Date(now - 25 * 3600 * 1000).toISOString();
  const future1h = new Date(now + 3600 * 1000).toISOString();

  sandbox.storeClaimTokens({
    work_id: "work-a",
    token: "tok-a",
    label: "claude-m1-01",
    expires_at: expired2h,
    documents: [{ id: "doc-a-en" }, { id: "doc-a-fr" }],
  });
  sandbox.storeClaimTokens({
    work_id: "work-old",
    token: "tok-old",
    label: "claude-m1-01",
    expires_at: expired25h,
    documents: [{ id: "doc-old" }],
  });

  sandbox.pruneClaimTokens(now);
  const afterPrune = {
    docAEn: sandbox.getClaimForDoc("doc-a-en"),
    docAFr: sandbox.getClaimForDoc("doc-a-fr"),
    docOld: sandbox.getClaimForDoc("doc-old"),
  };

  // api() attaches X-Claim-Token for /api/documents/{act_id}... and /api/claims/{work_id}...
  sandbox.app.csrf = "csrf-123";
  fetchHandler = async (path) => {
    if (path === "/api/documents/doc-b/pages/1") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ revision: 1, work_id: "work-b", claim_token: "tok-b", claim_expires_at: future1h }),
      };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };

  await sandbox.api("/api/documents/doc-a-en/pages/1", { method: "POST", body: {} });
  await sandbox.api("/api/claims/work-a/renew", { method: "POST", body: {} });
  await sandbox.api("/api/documents/doc-b/pages/1", { method: "POST", body: {} });

  const implicitStored = sandbox.getClaimForDoc("doc-b");
  sandbox.removeClaimTokensForWork("work-a");
  const afterRelease = {
    docAEn: sandbox.getClaimForDoc("doc-a-en"),
    docAFr: sandbox.getClaimForDoc("doc-a-fr"),
  };

  console.log(JSON.stringify({
    afterPrune,
    headersSent: fetchCalls.map((c) => ({ path: c.path, token: c.options.headers["X-Claim-Token"] || null })),
    implicitStored,
    afterRelease,
  }));
})();
"""
    )
    assert result["afterPrune"]["docAEn"]["token"] == "tok-a"
    assert result["afterPrune"]["docAFr"]["token"] == "tok-a"
    assert result["afterPrune"]["docOld"] is None  # pruned after 24h grace window

    assert result["headersSent"][0] == {"path": "/api/documents/doc-a-en/pages/1", "token": "tok-a"}
    assert result["headersSent"][1] == {"path": "/api/claims/work-a/renew", "token": "tok-a"}
    assert result["headersSent"][2] == {"path": "/api/documents/doc-b/pages/1", "token": None}

    assert result["implicitStored"]["token"] == "tok-b"
    assert result["implicitStored"]["work_id"] == "work-b"
    assert result["afterRelease"] == {"docAEn": None, "docAFr": None}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_lightweight_claim_view_skips_queue_and_banner_starts_heartbeat():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  sandbox.app.me = { email: "claude@example.org", roles: ["reviewer"], name: "Claude" };
  sandbox.location.hash = "#/claim?label=claude-m1-07";
  await sandbox.route();
  const pathsCalledOnClaimRoute = fetchCalls.map((c) => c.path);
  const labelInput = mainEl.findInputs().find((i) => i.attrs.name === "label");

  // Banner when held by self: renders text, Release button, and starts 5-minute heartbeat.
  sandbox.storeClaimTokens({
    work_id: "rw-law-1",
    token: "tok-1",
    label: "claude-m1-07",
    expires_at: "2026-11-20T14:32:00Z",
    documents: [{ id: "rw-law-1-en" }],
  });
  const mineBanner = sandbox.claimBanner("rw-law-1-en", {
    work_id: "rw-law-1",
    account: "claude@example.org",
    label: "claude-m1-07",
    expires_at: "2026-11-20T14:32:00Z",
  });
  const heartbeatIntervalMs = intervals.length ? intervals[intervals.length - 1].ms : null;

  // Banner when held by another agent.
  const otherBanner = sandbox.claimBanner("rw-law-2-en", {
    work_id: "rw-law-2",
    account: "codex@example.org",
    label: "codex-m2-01",
    expires_at: "2026-11-20T15:10:00Z",
  });

  console.log(JSON.stringify({
    pathsCalledOnClaimRoute,
    prefilledLabel: labelInput ? labelInput.value : null,
    mineText: mineBanner.allText(),
    mineButtons: mineBanner.findButtons().map((b) => b.textContent),
    heartbeatIntervalMs,
    otherText: otherBanner.allText(),
  }));
})();
"""
    )
    assert result["pathsCalledOnClaimRoute"] == []  # NO GET /api/queue call!
    assert result["prefilledLabel"] == "claude-m1-07"
    assert "You hold this work until 14:32 (claude-m1-07)" in result["mineText"]
    assert "Release" in result["mineButtons"]
    assert result["heartbeatIntervalMs"] == 300_000  # 5 minutes
    assert "Held by codex-m2-01 (codex@example.org) until 15:10" in result["otherText"]


def test_admin_agents_and_operators_wiring():
    src = APP_JS.read_text(encoding="utf-8")
    # Agents nav & view endpoints
    assert 'href: "#/agents"' in src
    assert '"/api/claims"' in src
    assert '"/api/agents/report"' in src
    assert '"/api/agents/pause-all"' in src
    assert '"/api/labels/block"' in src
    assert '"/api/labels/unblock"' in src
    # Operators flags & sessions endpoints
    assert "may_change_approved" in src
    assert "/sessions/end-all" in src
    assert "/sessions/${encodeURIComponent(s.id)}/end" in src
    # Work queue claim badge & claim-next endpoint
    assert "/api/queue/claim-next" in src
    assert "held by ${t.claim.label} until ${formatClock(t.claim.expires_at)}" in src
