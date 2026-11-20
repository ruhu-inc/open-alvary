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
      replace() {},
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  append(...nodes) {
    // As the real Element.append: a string becomes text, and so do null and undefined ("null").
    for (const n of nodes) {
      if (n === null || n === undefined) this.children.push({ textContent: String(n), children: [] });
      else if (typeof n === "string") this.children.push({ textContent: n, children: [] });
      else { n.parentNode = this; this.children.push(n); }
    }
  }
  replaceWith(node) {
    this.replacedWith = node;
    if (this.parentNode) {
      const at = this.parentNode.children.indexOf(this);
      if (at >= 0) this.parentNode.children[at] = node;
    }
  }
  get options() { return this.children.filter((c) => c && c.tagName === "OPTION"); }
  findLinks() {
    const out = [];
    if (this.tagName === "A") out.push(this);
    for (const c of this.children) {
      if (c && typeof c.findLinks === "function") out.push(...c.findLinks());
    }
    return out;
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

  // api() attaches X-Claim-Token for /api/documents/{act_id}... and /api/claims/{work_id}... (claims on)
  sandbox.app.me = { email: "claude@example.org", roles: ["reviewer"], claims: "warn" };
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
  sandbox.app.me = { email: "claude@example.org", roles: ["reviewer"], name: "Claude", claims: "enforce", agent: true };
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
    assert "You hold this work until 14:32 UTC (claude-m1-07)" in result["mineText"]
    assert "Release" in result["mineButtons"]
    assert result["heartbeatIntervalMs"] == 300_000  # 5 minutes
    assert "Held by codex-m2-01 (codex@example.org) until 15:10 UTC" in result["otherText"]
    assert "null" not in result["otherText"]


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
    assert "held by ${t.claim.label} until ${formatClockUtc(t.claim.expires_at)}" in src


def _json_response(status: int, body: str) -> str:
    """A fetch answer for the harness: {ok, status, json}."""
    return f"({{ ok: {str(200 <= status < 300).lower()}, status: {status}, json: async () => ({body}) }})"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_claims_off_shows_nothing_about_claims_and_sends_no_token():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  const me = { csrf: "c", name: "Owner", email: "owner@example.org", roles: ["reviewer", "admin"], claims: "off", agent: false, agents: false };
  fetchHandler = async (path) => {
    if (path === "/api/me") return { ok: true, status: 200, json: async () => me };
    if (path.startsWith("/api/queue")) return { ok: true, status: 200, json: async () => ({ counts: {}, tasks: [
      { id: "rw-1", title: "Law 1", priority: "full", kind: "review", started: true, pages: [1], count: 1,
        claim: { work_id: "rw-1", account: "codex@example.org", label: "codex-m2-01", expires_at: "2026-11-20T15:10:00Z" } },
    ], offset: 0, total: 1 }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };

  // Agents link follows /api/me's "agents", not the admin role.
  await sandbox.loadMe();
  const navWithoutAgents = whoEl.findLinks().map((a) => a.attrs.href);
  me.agents = true;
  await sandbox.loadMe();
  const navWithAgents = whoEl.findLinks().map((a) => a.attrs.href);

  fetchCalls.length = 0;
  await sandbox.queueView();
  const queueText = mainEl.allText();
  const queuePaths = fetchCalls.map((c) => c.path);

  sandbox.location.hash = "#/claim";
  await sandbox.claimView();
  const claimText = mainEl.allText();
  const claimLinks = mainEl.findLinks().map((a) => a.attrs.href);

  // A token left over from when claims were on: no banner, no heartbeat, no header.
  sandbox.storeClaimTokens({ work_id: "rw-1", token: "tok-old", label: "claude-m1-07", expires_at: "2026-11-20T14:32:00Z", documents: ["rw-1"] });
  const banner = sandbox.claimBanner("rw-1", { work_id: "rw-1", account: "owner@example.org", label: "claude-m1-07", expires_at: "2026-11-20T14:32:00Z" });
  sandbox.startClaimHeartbeat("rw-1");
  fetchCalls.length = 0;
  await sandbox.api("/api/documents/rw-1/pages/1", { method: "POST", body: {} });

  console.log(JSON.stringify({
    navWithoutAgents, navWithAgents, queueText, queuePaths, claimText, claimLinks,
    banner: banner === null,
    intervals: intervals.length,
    headerSent: fetchCalls[0].options.headers["X-Claim-Token"] || null,
  }));
})();
"""
    )
    assert "#/agents" not in result["navWithoutAgents"]
    assert "#/agents" in result["navWithAgents"]
    assert "Claim next" not in result["queueText"]
    assert "held by" not in result["queueText"]
    assert len(result["queuePaths"]) == 1 and result["queuePaths"][0].startswith("/api/queue?")
    assert "Claims are switched off on this workbench" in result["claimText"]
    assert "Label" not in result["claimText"]
    assert "#/queue" in result["claimLinks"]
    assert result["banner"] is True
    assert result["intervals"] == 0
    assert result["headerSent"] is None


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_claim_refusal_on_save_is_shown_and_keeps_the_text():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  sandbox.app.me = { email: "ada@example.org", roles: ["reviewer"], claims: "enforce", agent: false };
  sandbox.app.csrf = "c";
  sandbox.location.hash = "#/doc/rw-1/page/2";

  const makeView = () => ({
    actId: "rw-1", pageNumber: 2, address: "#/doc/rw-1/page/2",
    page: { flags: [] }, units: [{ kind: "paragraph", text: "My corrected words" }], join: null,
    resolutions: {}, comment: "", expected: 3,
    detail: { pages: [{ page: 2 }], readiness: { routed: false } },
    dirtyBadge: new FakeNode("span"),
  });
  const press = async (view, answer) => {
    fetchCalls.length = 0;
    fetchHandler = async (path, options) => (options && options.method === "POST") ? answer : { ok: true, status: 200, json: async () => ({}) };
    sandbox.localStorage.setItem("oa-draft:rw-1:2", "{\\"units\\":[]}");
    sandbox.app.dirty = true;
    const bar = sandbox.saveBar(view);
    const button = bar.findButtons().find((b) => b.textContent === "Save as checked");
    try { await button.listeners.click[0](); } catch { /* the reload draws into an empty fake */ }
    return {
      toast: toastEl.textContent,
      dirty: sandbox.app.dirty,
      draft: sandbox.localStorage.getItem("oa-draft:rw-1:2") !== null,
      reloaded: fetchCalls.some((c) => (c.options.method || "GET") === "GET"),
      units: view.units.map((u) => u.text),
    };
  };

  const held = await press(makeView(), ANSWER_HELD);
  const blocked = await press(makeView(), ANSWER_BLOCKED);
  const conflict = await press(makeView(), ANSWER_CONFLICT);

  // Mark checked in the whole-document view.
  const markChecked = async (answer) => {
    fetchHandler = async () => answer;
    const section = sandbox.readPage("rw-1", { page: 5, revision: 2, units: [], status: "edited", open_high: 0 }, true);
    const button = section.findButtons().find((b) => b.textContent === "Mark checked");
    await button.listeners.click[0]({ currentTarget: { replaceWith() {} } });
    return toastEl.textContent;
  };
  const markHeld = await markChecked(ANSWER_HELD);
  const markConflict = await markChecked(ANSWER_CONFLICT);

  console.log(JSON.stringify({ held, blocked, conflict, markHeld, markConflict }));
})();
""".replace(
            "ANSWER_HELD",
            _json_response(
                409, '{ code: "claim_held", message: "Another reviewer holds this work until 14:32 UTC." }'
            ),
        )
        .replace(
            "ANSWER_BLOCKED",
            _json_response(403, '{ code: "label_blocked", message: "This label is blocked." }'),
        )
        .replace("ANSWER_CONFLICT", _json_response(409, '{ message: "Someone saved this page first." }'))
    )
    for key, words in (
        ("held", "Another reviewer holds this work until 14:32 UTC."),
        ("blocked", "This label is blocked."),
    ):
        refused = result[key]
        assert refused["toast"] == words
        assert refused["dirty"] is True
        assert refused["draft"] is True
        assert refused["reloaded"] is False
        assert refused["units"] == ["My corrected words"]
    # A 409 with no code is still the revision conflict, reloaded as before.
    assert result["conflict"]["toast"] == "Someone saved this page first. Reloading their version."
    assert result["conflict"]["dirty"] is False
    assert result["conflict"]["reloaded"] is True
    assert result["markHeld"] == "Another reviewer holds this work until 14:32 UTC."
    assert result["markConflict"] == "Page 5 changed meanwhile; open it to check"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_claim_banner_writes_no_null_and_says_utc():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  const other = { work_id: "rw-2", account: "codex@example.org", label: "codex-m2-01", expires_at: "2026-11-20T15:10:00Z" };
  sandbox.app.me = { email: "ada@example.org", roles: ["reviewer"], claims: "warn" };
  const plain = sandbox.claimBanner("rw-2-en", other);
  sandbox.app.me = { email: "boss@example.org", roles: ["reviewer", "approver"], claims: "warn" };
  const approver = sandbox.claimBanner("rw-2-en", other);
  const expires = approver.children[0].children[0].children.find((c) => c.className === "claim-expires");

  fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({ counts: {}, tasks: [
    { id: "rw-2", title: "Law 2", priority: "full", kind: "review", started: true, pages: [1], count: 1, claim: other },
  ], offset: 0, total: 1 }) });
  await sandbox.queueView();

  console.log(JSON.stringify({
    plain: plain.allText(),
    approver: approver.allText(),
    approverButtons: approver.findButtons().map((b) => b.textContent),
    title: expires.attrs.title,
    queue: mainEl.allText(),
  }));
})();
"""
    )
    assert "null" not in result["plain"]
    assert result["plain"].endswith("until 15:10 UTC")
    assert "null" not in result["approver"]
    assert "Confirm release" in result["approverButtons"]
    assert result["title"] == "2026-11-20T15:10:00Z"
    assert "held by codex-m2-01 until 15:10 UTC" in result["queue"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_lost_token_take_up_again_and_heartbeat_stops_on_refusal_or_sign_out():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  sandbox.app.me = { email: "claude@example.org", roles: ["reviewer"], claims: "enforce", agent: true };
  sandbox.app.csrf = "c";
  sandbox.setTabLabel("claude-m1-07");
  const serverClaim = { work_id: "rw-3", account: "claude@example.org", label: "claude-m1-07", task: "status", expires_at: "2026-11-20T14:32:00Z" };

  // (1) No token stored: say so, offer to take it up again, and start no heartbeat.
  const lost = sandbox.claimBanner("rw-3-en", serverClaim);
  const parent = new FakeNode("div");
  parent.append(lost);
  const lostText = lost.allText();
  const lostIntervals = intervals.length;
  fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({
    work_id: "rw-3", task: "status", token: "tok-new", expires_at: "2026-11-20T15:32:00Z",
    documents: [{ id: "rw-3-en" }, { id: "rw-3-fr" }], rotated: true }) });
  fetchCalls.length = 0;
  await lost.findButtons().find((b) => b.textContent === "Take it up again").listeners.click[0]();
  const takeUpCall = { path: fetchCalls[0].path, body: JSON.parse(fetchCalls[0].options.body) };
  const stored = [sandbox.getClaimForDoc("rw-3-en"), sandbox.getClaimForDoc("rw-3-fr")].map((c) => c && c.token);
  const redrawn = parent.children[0].allText();
  const heartbeatAfterTakeUp = intervals.length > lostIntervals && !intervals[intervals.length - 1].cleared;

  // (2) A refused renewal stops the heartbeat and puts the server's words in the banner.
  const mine = sandbox.claimBanner("rw-3-en", { ...serverClaim, expires_at: "2026-11-20T15:32:00Z" });
  const beat = intervals[intervals.length - 1];
  fetchHandler = async () => ({ ok: false, status: 409, json: async () => ({ code: "claim_lost", message: "Your claim on this work has ended: claim it again." }) });
  await beat.fn();
  const afterRefusal = { cleared: beat.cleared, text: mine.allText(), timer: sandbox.app.heartbeatTimer };

  // (3) Signed out (a 401 from the workbench): the heartbeat stops.
  sandbox.claimBanner("rw-3-en", { ...serverClaim, expires_at: "2026-11-20T15:32:00Z" });
  const beat2 = intervals[intervals.length - 1];
  fetchHandler = async (path) => path === "/api/me"
    ? { ok: false, status: 401, json: async () => ({ oidc: true }) }
    : { ok: false, status: 401, json: async () => ({ message: "Sign in again." }) };
  await beat2.fn();
  const afterSignOut = { cleared: beat2.cleared, timer: sandbox.app.heartbeatTimer };

  // Leaving the document: route() stops it.
  sandbox.app.me = { email: "claude@example.org", roles: ["reviewer"], claims: "enforce", agent: true };
  sandbox.claimBanner("rw-3-en", { ...serverClaim, expires_at: "2026-11-20T15:32:00Z" });
  const beat3 = intervals[intervals.length - 1];
  fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({ counts: {}, tasks: [], offset: 0, total: 0 }) });
  sandbox.location.hash = "#/queue";
  await sandbox.route();

  console.log(JSON.stringify({
    lostText, lostIntervals, takeUpCall, stored, redrawn, heartbeatAfterTakeUp,
    afterRefusal, afterSignOut, leftCleared: beat3.cleared,
  }));
})();
"""
    )
    assert (
        "This work is claimed under your label, but this browser has no claim token for it."
        in result["lostText"]
    )
    assert "You hold this work" not in result["lostText"]
    assert result["lostIntervals"] == 0
    assert result["takeUpCall"] == {
        "path": "/api/claims/rw-3",
        "body": {"label": "claude-m1-07", "task": "status"},
    }
    assert result["stored"] == ["tok-new", "tok-new"]
    assert "You hold this work until 15:32 UTC (claude-m1-07)" in result["redrawn"]
    assert result["heartbeatAfterTakeUp"] is True
    assert result["afterRefusal"]["cleared"] is True
    assert result["afterRefusal"]["timer"] is None
    assert "Your claim on this work has ended: claim it again." in result["afterRefusal"]["text"]
    assert "You hold this work" not in result["afterRefusal"]["text"]
    assert result["afterSignOut"] == {"cleared": True, "timer": None}
    assert result["leftCleared"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_work_gone_quiet_says_when_it_goes_back_and_offers_to_keep_it():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  sandbox.app.me = { email: "claude@example.org", roles: ["reviewer"], claims: "enforce", agent: true };
  sandbox.app.csrf = "c";
  sandbox.setTabLabel("claude-m1-07");
  sandbox.storeClaimTokens({ work_id: "rw-3", token: "tok-old", label: "claude-m1-07",
    expires_at: "2026-11-20T14:32:00Z", documents: [{ id: "rw-3-en" }] });
  const serverClaim = { work_id: "rw-3", account: "claude@example.org", label: "claude-m1-07", task: "review", expires_at: "2026-11-20T14:32:00Z" };
  const parent = new FakeNode("div");
  const mine = sandbox.claimBanner("rw-3-en", serverClaim);
  parent.append(mine);
  const beat = intervals[intervals.length - 1];
  const words = "Nothing on this work has been opened or saved for 60 minutes, so it goes back to the queue at 14:32 UTC. Take it up again to carry on.";
  fetchHandler = async () => ({ ok: false, status: 409, json: async () => ({ code: "claim_idle", message: words }) });
  await beat.fn();
  const idle = { cleared: beat.cleared, text: mine.allText(), buttons: mine.findButtons().map((b) => b.textContent) };
  fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({
    work_id: "rw-3", task: "review", token: "tok-new", expires_at: "2026-11-20T15:40:00Z",
    documents: [{ id: "rw-3-en" }], rotated: true }) });
  await mine.findButtons().find((b) => b.textContent === "Take it up again").listeners.click[0]();
  console.log(JSON.stringify({ idle, kept: parent.children[0].allText(), token: sandbox.getClaimForDoc("rw-3-en").token }));
})();
"""
    )
    assert result["idle"]["cleared"] is True
    assert "goes back to the queue at 14:32 UTC" in result["idle"]["text"]
    assert "Take it up again" in result["idle"]["buttons"]
    assert "You hold this work until 15:40 UTC (claude-m1-07)" in result["kept"]
    assert result["token"] == "tok-new"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_persons_unlabelled_claim_is_their_own_whatever_the_tab_label():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  sandbox.app.me = { email: "ada@example.org", roles: ["reviewer"], claims: "warn", agent: false };
  sandbox.app.csrf = "c";
  sandbox.setTabLabel("claude-m1-07"); // typed in the Claim next panel
  const serverClaim = { work_id: "rw-4", account: "ada@example.org", label: "unlabelled", expires_at: "2026-11-20T14:32:00Z" };

  // No token stored: still hers, with Release, and no heartbeat (her next save brings a token).
  const noToken = sandbox.claimBanner("rw-4-en", serverClaim);
  const intervalsNoToken = intervals.length;

  // A save takes the claim by itself: the token is stored under "unlabelled", not the tab's label.
  fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({
    revision: 4, claim_token: "tok-u", claim_expires_at: "2026-11-20T15:00:00Z", claim_work_id: "rw-4",
    claim_documents: ["rw-4-en", "rw-4-fr"], claim_label: "unlabelled" }) });
  await sandbox.api("/api/documents/rw-4-en/pages/1", { method: "POST", body: {} });
  const stored = sandbox.getClaimForDoc("rw-4-fr");
  const withToken = sandbox.claimBanner("rw-4-en", serverClaim);
  const intervalsWithToken = intervals.length;

  console.log(JSON.stringify({
    noToken: noToken.allText(),
    noTokenButtons: noToken.findButtons().map((b) => b.textContent),
    intervalsNoToken,
    storedLabel: stored && stored.label,
    withToken: withToken.allText(),
    intervalsWithToken,
  }));
})();
"""
    )
    assert "You hold this work until 14:32 UTC" in result["noToken"]
    assert "no claim token" not in result["noToken"]
    assert "Held by" not in result["noToken"]
    assert "Release" in result["noTokenButtons"]
    assert result["intervalsNoToken"] == 0
    assert result["storedLabel"] == "unlabelled"
    assert "You hold this work until 14:32 UTC" in result["withToken"]
    assert result["intervalsWithToken"] == 1


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_no_audit_task_offered_and_audit_save_says_so():
    result = _run_node(
        _harness_preamble()
        + """
(async () => {
  sandbox.app.me = { email: "claude@example.org", roles: ["reviewer"], claims: "enforce", agent: true };
  sandbox.app.csrf = "c";
  sandbox.location.hash = "#/claim?task=audit";
  const panel = sandbox.claimNextPanel({ standalone: true });
  const task = panel.findInputs().find((i) => i.attrs.name === "task");

  sandbox.location.hash = "#/doc/rw-5/page/1";
  const view = {
    actId: "rw-5", pageNumber: 1, address: "#/doc/rw-5/page/1",
    page: { flags: [] }, units: [{ kind: "paragraph", text: "The auditor's reading" }], join: null,
    resolutions: {}, comment: "", expected: 7,
    detail: { pages: [{ page: 1 }], readiness: { routed: false } },
    dirtyBadge: new FakeNode("span"),
  };
  fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({ audit_id: 12, audited: true, verdict: "minor", diff: {} }) });
  fetchCalls.length = 0;
  sandbox.app.dirty = true;
  const bar = sandbox.saveBar(view);
  await bar.findButtons().find((b) => b.textContent === "Save as checked").listeners.click[0]();

  console.log(JSON.stringify({
    taskOptions: task.children.map((o) => o.value),
    taskValue: task.value,
    toast: toastEl.textContent,
    calls: fetchCalls.length,
    units: view.units.map((u) => u.text),
    expected: view.expected,
  }));
})();
"""
    )
    assert result["taskOptions"] == ["review", "status"]
    assert result["taskValue"] == "review"
    assert result["toast"] == "Audit saved"
    assert "undefined" not in result["toast"]
    assert result["calls"] == 1  # the page is not reloaded over the auditor's reading
    assert result["units"] == ["The auditor's reading"]
    assert result["expected"] == 7


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pending_status_research_is_listed_before_approval():
    result = _run_node(
        _harness_preamble()
        + """
sandbox.app.me = { email: "ada@example.org", roles: ["reviewer"], claims: "off" };
const readiness = {
  ready: false, routed: false, pages: 3, unchecked_pages: [], unresolved_flag_pages: [], open_high_flags: [],
  stale_pages: [], missing_checks: [], details: { confirmed: true, status: "confirmed" }, text: { errors: 0 },
  status_pending: true, status_pending_message: "Legal status: research pending.",
};
const data = { readiness, approvals: [], materials: {}, act: { type: "law" }, text_check: null };
const panel = sandbox.approvalPanel("rw-6", data, false);
const checklist = sandbox.approvalChecklist("rw-6", data, false, null);
console.log(JSON.stringify({ panel: panel.allText(), checklist: checklist.allText() }));
"""
    )
    assert "Before approval: Legal status: research pending." in result["panel"]
    assert "Legal status: research pending." in result["checklist"]
