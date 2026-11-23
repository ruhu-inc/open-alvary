/**
 * Law check page controller (/agents/site.js).
 *
 * The page is one thread, like any chat: what the visitor sent, each result card and each
 * AI answer, in order, with the composer at the bottom of the window.
 *
 * - Installs the fetch wrapper (LC-16) capturing check_claim JSON-RPC responses
 *   from /agents/mcp before the challenge runtime ever loads.
 * - Verifies every receipt via ./receipt.js before rendering <oa-receipt>.
 * - Keeps the thread in sessionStorage and verifies every saved receipt again on load.
 * - Loads ./runtime/index.js only once there is an AI key, and runs each question through the
 *   runtime's own engine (engine.streamTurn); the runtime's files are not changed.
 * - A result card comes only from a receipt this page verified; the AI's words are drawn as
 *   plain text and never as a card. Zero innerHTML with untrusted data.
 */

import {
  verifyReceipt,
  loadReceiptKeys,
  RECEIPT_KEYS_URL,
} from "./receipt.js";
import {
  formatLongDate,
  wordsLineForReceipt,
  recordLinesForReceipt,
} from "./receipt-card.js";
import { renderReceiptPanel } from "./receipt-panel.js";

const STORAGE_KEY = "oa_law_check_thread_v2";
const OLD_STORAGE_KEY = "oa_law_check_receipts_v1";
const MAX_SAVED_ITEMS = 60;
const MAX_QUESTION_LEN = 4000;
const MAX_PROOF_BYTES = 512 * 1024;
const CITATION_NEEDED = "Enter the law and section, for example Freedom of Information Act 2011, s. 1(1).";
const MCP_PATH = "/agents/mcp";
const LOCAL_KEYS_PATH = "/.well-known/open-alvary-receipt-keys.json";
const MAX_QUOTE_LEN = 2000;

/**
 * Extract `{ receipt, claim }` from an MCP tools/call result object.
 *
 * @param {any} result
 * @returns {{ receipt: any, claim: any } | null}
 */
export function parseCheckClaimEnvelope(result) {
  if (!result || typeof result !== "object") return null;
  if (result.structuredContent && typeof result.structuredContent === "object" && result.structuredContent.receipt) {
    return {
      receipt: result.structuredContent.receipt,
      claim: result.structuredContent.claim ?? null,
    };
  }
  if (Array.isArray(result.content)) {
    for (const part of result.content) {
      if (part && part.type === "text" && typeof part.text === "string") {
        const trimmed = part.text.trim();
        if (trimmed.startsWith("{")) {
          try {
            const parsed = JSON.parse(trimmed);
            if (parsed && typeof parsed === "object" && parsed.receipt) {
              return { receipt: parsed.receipt, claim: parsed.claim ?? null };
            }
          } catch {
            // ignore non-JSON text part
          }
        }
      }
    }
  }
  return null;
}

function parseJsonRpcText(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return null;
  if (trimmed.startsWith("{")) {
    try {
      return JSON.parse(trimmed);
    } catch {
      return null;
    }
  }
  // SSE fallback: find JSON-RPC response line containing result or error
  for (const line of trimmed.split("\n")) {
    const l = line.trim();
    if (!l.startsWith("data:")) continue;
    const payload = l.slice(5).trim();
    if (!payload || payload === "[DONE]") continue;
    try {
      const obj = JSON.parse(payload);
      if (obj && ("result" in obj || "error" in obj)) return obj;
    } catch {
      // continue
    }
  }
  return null;
}

function parseToolError(result) {
  const sc = result?.structuredContent && typeof result.structuredContent === "object"
    ? result.structuredContent
    : null;
  const textParts = Array.isArray(result?.content)
    ? result.content.filter((c) => c?.type === "text").map((c) => c.text)
    : [];
  const text = textParts.join("\n").trim();
  let code = typeof sc?.code === "string" ? sc.code : null;
  if (!code && text) {
    const m = /^([a-z0-9_]+)\s*:/i.exec(text);
    if (m) code = m[1].toLowerCase();
  }
  let seconds = typeof sc?.retry_after_seconds === "number"
    ? sc.retry_after_seconds
    : typeof sc?.seconds === "number"
      ? sc.seconds
      : null;
  if (seconds === null && text) {
    const m = /(\d+)\s*seconds?/i.exec(text);
    if (m) seconds = Number(m[1]);
  }
  return { code: code || "temporarily_unavailable", seconds, text };
}

function scrollBehavior() {
  return globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

function matchesMcpUrl(input, mcpPath) {
  const rawUrl = typeof input === "string"
    ? input
    : input && typeof input === "object" && "url" in input
      ? String(input.url)
      : "";
  if (!rawUrl) return false;
  try {
    const base = typeof location !== "undefined" && location.origin
      ? location.origin
      : "https://open.alvary.ai";
    const parsed = new URL(rawUrl, base);
    return parsed.pathname === mcpPath || parsed.pathname.endsWith(mcpPath);
  } catch {
    return rawUrl === mcpPath || rawUrl.endsWith(mcpPath);
  }
}

/**
 * Install the LC-16 fetch wrapper that observes `check_claim` MCP responses
 * from `/agents/mcp` without altering the Response stream returned to callers.
 */
export function installCheckClaimInterceptor({
  targetGlobal = globalThis,
  mcpPath = MCP_PATH,
  onCheckClaim = null,
  onToolError = null,
} = {}) {
  const originalFetch = targetGlobal.fetch;
  if (typeof originalFetch !== "function") {
    return { uninstall() {} };
  }

  const wrappedFetch = async function lawCheckFetchWrapper(input, init) {
    let checkClaimArgs = null;
    const method = (init?.method || (typeof input === "object" && input?.method) || "GET").toUpperCase();

    if (method === "POST" && matchesMcpUrl(input, mcpPath) && typeof init?.body === "string") {
      try {
        const reqBody = JSON.parse(init.body);
        if (
          reqBody &&
          reqBody.method === "tools/call" &&
          reqBody.params &&
          reqBody.params.name === "check_claim"
        ) {
          checkClaimArgs = reqBody.params.arguments || {};
        }
      } catch {
        // Not valid JSON; pass through untouched
      }
    }

    const resp = await originalFetch.call(this, input, init);

    if (checkClaimArgs !== null && resp && resp.ok && typeof resp.clone === "function") {
      try {
        const cloned = resp.clone();
        const text = await cloned.text();
        const rpc = parseJsonRpcText(text);
        const result = rpc?.result;
        if (result) {
          if (result.isError) {
            const errInfo = parseToolError(result);
            onToolError?.({ ...errInfo, arguments: checkClaimArgs });
          } else {
            const envelope = parseCheckClaimEnvelope(result);
            if (envelope) {
              onCheckClaim?.({ envelope, arguments: checkClaimArgs });
            }
          }
        }
      } catch {
        // Never break the underlying MCP caller on observation errors
      }
    }

    return resp;
  };

  targetGlobal.fetch = wrappedFetch;
  return {
    uninstall() {
      if (targetGlobal.fetch === wrappedFetch) {
        targetGlobal.fetch = originalFetch;
      }
    },
  };
}

function verificationErrorMessage(reason) {
  switch (reason) {
    case "invalid_signature":
    case "invalid_signature_encoding":
    case "invalid_signature_length":
    case "claim_digest_mismatch":
      return "The signature does not match. Do not rely on this receipt.";
    case "key_revoked":
      return "This receipt was signed with a key that has been withdrawn. Re-check to get a new one.";
    case "unsupported_version":
      return "This receipt is in a format this page does not know.";
    case "issued_in_future":
      return "This receipt is dated later than this device's clock. Check the clock, then try again.";
    case "unknown_key":
      return "This receipt was signed with a key Open Alvary does not list. Do not rely on it.";
    case "key_not_yet_valid":
    case "key_expired":
      return "This receipt is dated outside the time its signing key was in use. Do not rely on it.";
    case "crypto_unavailable":
      return "This browser cannot verify receipts.";
    default:
      return "This is not a complete receipt. Copy it again from where you saved it.";
  }
}

/**
 * Read a message typed as one line of chat for a citation and a quoted passage, for example
 *   Freedom of Information Act 2011, s. 1(1): "Notwithstanding anything contained in ..."
 * It answers only when the message is plainly that: one quoted passage of 40 characters or more,
 * a short remainder that names a section, and no question. Anything else is left for the AI.
 *
 * @param {string} text
 * @returns {{ citation: string, quote: string } | null}
 */
export function parseCitationAndQuote(text) {
  const raw = String(text || "").trim();
  if (!raw || raw.length > 2600) return null;
  const match = /["“]([^"“”]{40,})["”]/.exec(raw);
  if (!match) return null;
  const quote = match[1].trim();
  const before = raw.slice(0, match.index).trim();
  const after = raw.slice(match.index + match[0].length).trim();
  // Words after the quote, or a question, mean the visitor is saying something about it.
  if (/[\p{L}\p{N}]/u.test(after) || raw.includes("?")) return null;
  const citation = before
    .replace(/[\s:;,–—-]+$/u, "")
    .replace(/\b(?:says|states|provides|reads)$/i, "")
    .replace(/[\s:;,–—-]+$/u, "")
    .trim();
  if (!citation || citation.length > 200) return null;
  if (!/(?:^|[^\p{L}])(?:ss?|secs?|sect|sections?|arts?|articles?|regs?|regulations?|rules?|paras?|paragraphs?)\.?\s*\d/iu.test(citation)) {
    return null;
  }
  return { citation, quote };
}

/** A saved proof pasted as text: the `{receipt, claim}` export or a bare receipt. */
export function looksLikeProof(text) {
  const raw = String(text || "").trim();
  return raw.startsWith("{") && raw.endsWith("}") && /"schema_version"\s*:/.test(raw);
}

/**
 * The AI's final text as the visitor should read it: without the runtime's card blocks, which
 * this page never draws (a model could use one to imitate a result card).
 */
export function cleanAnswerText(raw) {
  let text = typeof raw === "string" ? raw : "";
  text = text.replace(/```cruse-ui[\s\S]*?```/gi, "");
  const open = text.search(/```cruse-ui/i);
  if (open >= 0) text = text.slice(0, open);
  text = text.replace(/<\/?tool_(?:response|call)>/gi, "").trim();
  if (text.startsWith("{") && text.endsWith("}")) {
    try {
      const doc = JSON.parse(text);
      for (const key of ["Answer", "answer", "Response", "response", "text"]) {
        if (doc && typeof doc[key] === "string" && doc[key].trim()) return doc[key].trim();
      }
    } catch {
      // not an envelope: show it as written
    }
  }
  return text;
}

/** A fixed line for each step of a turn. Tool and section names are model text, so none is shown. */
export function statusLineForEvent(event) {
  const name = String(event?.tool?.name || "").toLowerCase();
  switch (event?.type) {
    case "turn.started":
      return "Thinking…";
    case "agent.started":
      return "Working on your question…";
    case "tool.started":
    case "tool.progress":
      if (name.includes("check_claim")) return "Checking a quote against the published text…";
      if (name.includes("resolve_citation")) return "Finding the section…";
      if (name.includes("get_anchor")) return "Reading the section…";
      if (name.includes("search_catalogue") || name.includes("retrieve_evidence")) return "Searching the published laws…";
      if (name.startsWith("website_")) return "Updating this page…";
      if (event.tool?.kind === "remote network" || event.tool?.kind === "linked network") return "Contacting another site…";
      return "Working on your question…";
    case "tool.completed":
    case "tool.failed":
    case "agent.completed":
      return "Putting the answer together…";
    default:
      return "";
  }
}

function appendInline(parent, text) {
  // **bold** and `code` only; everything else is plain text.
  const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;
  let last = 0;
  for (const m of text.matchAll(pattern)) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    const token = m[0];
    const el = document.createElement(token.startsWith("`") ? "code" : "strong");
    el.textContent = token.startsWith("`") ? token.slice(1, -1) : token.slice(2, -2);
    parent.appendChild(el);
    last = m.index + token.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

/**
 * Draw the AI's text as paragraphs and lists. Text only: no HTML, links or images from the
 * model are ever made into elements.
 */
export function renderAnswerText(container, text) {
  container.replaceChildren();
  const blocks = String(text || "").replace(/\r\n?/g, "\n").split(/\n{2,}/);
  for (const block of blocks) {
    const lines = block.split("\n").filter((line) => line.trim());
    if (!lines.length) continue;
    const bullet = lines.every((line) => /^\s*[-*•]\s+/.test(line));
    const numbered = lines.every((line) => /^\s*\d+[.)]\s+/.test(line));
    if (bullet || numbered) {
      const list = document.createElement(numbered ? "ol" : "ul");
      for (const line of lines) {
        const li = document.createElement("li");
        appendInline(li, line.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, ""));
        list.appendChild(li);
      }
      container.appendChild(list);
      continue;
    }
    const p = document.createElement("p");
    lines.forEach((line, i) => {
      if (i > 0) p.appendChild(document.createElement("br"));
      appendInline(p, line.replace(/^#{1,6}\s+/, ""));
    });
    container.appendChild(p);
  }
}

const $ = (id) => document.getElementById(id);

function shorten(text, max) {
  const chars = [...String(text || "")];
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : chars.join("");
}

class LawCheckController {
  constructor() {
    this.records = [];
    this.jwks = null;
    this.items = []; // what the thread holds, as saved in the tab: user, ai, result, divider
    this.cardByReceiptId = new Map();
    // The check shown in full in the receipt panel, on a window wide enough for it.
    this.selectedCard = null;
    this.panelWide = typeof matchMedia === "function" ? matchMedia("(min-width: 1100px)") : null;
    this.cardsBySourceId = new Map();
    this.runtime = null; // { engine, trace } once the challenge runtime has started
    this.runtimeLoaded = false;
    this.runtimeLoading = null;
    this.isPaused = false;
    this.isBusy = false;
    this.overLimit = false;
    this.rateLimitTimer = null;
    this.directCallCount = 0;
    this.activeAgentTurn = null;
    this.pendingAgentWork = [];
    this.mode = "quote";
    this.modeChosen = false;
    this.hasProviderKey = false;
    this.providerName = "";
    this.turnSeq = 0;
    this.statusItem = null;
    this.openAgentsView = false;
    this._rpcId = 0;
    this._lastSearchAt = 0;
  }

  openSection({ source_id, pinpoint } = {}) {
    if (!source_id) return { ok: false, reason: "Missing source_id." };
    const cards = this.cardsBySourceId.get(source_id) || [];
    for (const card of cards) {
      card.addReaderLink(source_id, pinpoint || "");
    }
    return {
      ok: true,
      source_id,
      pinpoint: pinpoint || null,
      url: `/sources/${encodeURIComponent(source_id)}`,
      updated_cards: cards.length,
    };
  }

  showReceipt({ receipt_id } = {}) {
    if (!receipt_id) return { ok: false, reason: "Missing receipt_id." };
    const card = this.cardByReceiptId.get(receipt_id);
    if (!card) return { ok: false, reason: "Receipt not found on this page." };
    // Opens the card and brings it into view; never moves keyboard focus.
    card.expandAndReveal(true);
    return { ok: true, receipt_id };
  }

  init() {
    if (typeof document === "undefined") return;
    if (!$("composer-form")) return;

    // 1. Install the LC-16 fetch wrapper at once, before any runtime loads.
    installCheckClaimInterceptor({
      onCheckClaim: ({ envelope, arguments: args }) => {
        if (this.directCallCount > 0) return; // the page's own check handles its own result
        const work = this._handleInterceptedAgentReceipt(envelope, args);
        this.pendingAgentWork.push(work);
      },
      onToolError: (errInfo) => {
        if (this.directCallCount > 0) return;
        this._handleInterceptedAgentError(errInfo);
      },
    });

    this.panelWide?.addEventListener?.("change", () => this._syncPanel());
    this._bindMobileNav();
    this._bindSlashMenu();
    this._bindComposer();
    this._bindProof();
    this._bindProviderModal();
    this._bindConnectivity();
    this._bindAlternativeEvents();
    document.addEventListener("oa-receipt-announce", (event) => {
      if (event.detail?.text) this._announce(event.detail.text);
    });
    $("new-chat")?.addEventListener("click", () => this._newChat());

    if (!globalThis.crypto?.subtle) {
      this._showGlobalBanner("This browser cannot verify receipts.", "red");
      this.isPaused = true;
      this._refreshComposerState();
      return;
    }

    this._loadRecords();
    this._restoreThread();
    this._syncInitialRoute();
  }

  _bindMobileNav() {
    const btn = document.getElementById("mobile-nav-toggle");
    const nav = document.getElementById("primary-navigation");
    if (!btn || !nav) return;
    btn.addEventListener("click", () => {
      const expanded = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", expanded ? "false" : "true");
      nav.classList.toggle("expanded", !expanded);
    });
  }

  _setMode(mode, { updateHash = true, focus = false, chosen = true } = {}) {
    this.mode = mode === "ask" ? "ask" : "quote";
    if (chosen) this.modeChosen = true;
    const quote = this.mode === "quote";
    const input = $("composer-input");
    const citation = $("citation-input");
    const label = $("composer-label");
    $("mode-quote")?.setAttribute("aria-pressed", quote ? "true" : "false");
    $("mode-ask")?.setAttribute("aria-pressed", quote ? "false" : "true");
    $("citation-field").hidden = !quote;
    $("composer-form").dataset.mode = this.mode;
    label.textContent = quote ? "Quoted words" : "Your question";
    label.classList.toggle("sr-only", !quote);
    input.placeholder = quote
      ? "Paste the exact words from the section"
      : "Ask what a published law says, or type / for commands";
    this._setFieldError(input, null);
    this._setFieldError(citation, null);
    this._updateCounter();
    this._refreshComposerState();
    if (updateHash && typeof history !== "undefined") {
      try {
        history.replaceState(null, "", `${location.pathname}${location.search}#${quote ? "check" : "ask"}`);
      } catch {
        // ignore
      }
    }
    // Start the chat runtime ahead of the first question, but only once there is a key.
    if (!quote && this.hasProviderKey) this._ensureRuntimeLoaded();
    if (focus) (quote && !citation.value.trim() ? citation : input).focus();
  }

  _refreshComposerState() {
    const send = $("composer-send");
    const note = $("composer-note");
    if (!send) return;
    const quote = this.mode === "quote";
    // A phone's row has room for one word beside the mode switch.
    const narrow = Boolean(globalThis.matchMedia?.("(max-width: 520px)").matches);
    send.textContent = this.isBusy
      ? quote ? "Checking…" : "Working…"
      : quote ? (narrow ? "Check" : "Check quote") : "Send";
    const blocked = this.isBusy || this.isPaused || Boolean(this.rateLimitTimer) || this.overLimit;
    if (blocked) send.setAttribute("aria-disabled", "true");
    else send.removeAttribute("aria-disabled");
    if (note) {
      note.textContent = quote
        ? "No AI is used for this check."
        : this.hasProviderKey
          ? `Answers come from ${this.providerName || "your AI provider"}. Open Alvary checks each quote.`
          : "Asking a question needs your own AI key.";
    }
  }

  _setBusy(busy) {
    this.isBusy = busy;
    this._refreshComposerState();
  }

  _updateCounter() {
    const input = $("composer-input");
    const counter = $("quote-counter");
    if (!input || !counter) return 0;
    const len = [...input.value].length;
    const max = this.mode === "quote" ? MAX_QUOTE_LEN : MAX_QUESTION_LEN;
    const showAt = max - 200;
    if (len >= showAt) {
      counter.hidden = false;
      counter.textContent = `${len} of ${max} characters`;
      counter.classList.toggle("over-limit", len > max);
    } else {
      counter.hidden = true;
    }
    this.overLimit = len > max;
    return len;
  }

  _syncInitialRoute() {
    const params = new URLSearchParams(location.search);
    const hash = location.hash.replace(/^#/, "");
    this.openAgentsView = params.get("dev") === "1";

    if (params.has("code")) {
      // Returning from OpenRouter: the runtime finishes that sign-in itself, so it starts even with no key yet.
      this._setMode("ask", { updateHash: false });
      this._ensureRuntimeLoaded().then(() => this._refreshProviderUi?.());
    } else if (hash === "ask") {
      this._setMode("ask", { updateHash: false });
    } else if (hash === "check" || hash === "recheck" || hash === "verify") {
      this._setMode("quote", { updateHash: false });
    } else {
      // No address given: checking a quote, until a saved key says the visitor asks questions.
      this._setMode("quote", { updateHash: false, chosen: false });
    }
  }

  _bindConnectivity() {
    window.addEventListener("offline", () => {
      this._showGlobalBanner("You are offline. Receipts already shown stay here.", "grey");
    });
    window.addEventListener("online", () => {
      if (!this.isPaused) this._hideGlobalBanner();
    });
  }

  _bindAlternativeEvents() {
    document.addEventListener("oa-check-alternative", (event) => {
      const { alternative, claim } = event.detail || {};
      if (!alternative) return;
      const nextClaim = {
        jurisdiction: claim?.jurisdiction || "ng",
        language: claim?.language || "en",
      };
      if (alternative.anchor_id) {
        nextClaim.anchor_id = alternative.anchor_id;
      } else {
        nextClaim.citation = alternative.pinpoint
          ? `${alternative.title}, ${alternative.pinpoint}`
          : alternative.title;
      }
      if (claim?.quote) nextClaim.quote = claim.quote;
      this._beginTurn();
      this._runDirectCheck(nextClaim, { source: "form" });
    });
  }

  async _loadRecords() {
    const datalist = document.getElementById("law-suggestions");
    try {
      const resp = await fetch("./records.json", { headers: { Accept: "application/json" } });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      this.records = Array.isArray(data?.records) ? data.records : [];
    } catch {
      this.records = [];
    }

    // How many of Law check's laws have their text published now, from the site's own record answers
    // (has_full_text): the list names the laws Law check may check, not whether their text is out yet.
    this._countPublished(this.records);

    if (datalist) {
      datalist.replaceChildren();
      for (const rec of this.records) {
        const opt = document.createElement("option");
        opt.value = rec.example?.section ? `${rec.title}, ${rec.example.section}` : `${rec.title}, `;
        datalist.appendChild(opt);
      }
    }
  }

  async _countPublished(records) {
    const summary = document.getElementById("coverage-summary");
    if (!summary || !records.length) return;
    const answers = await Promise.all(
      records.map((rec) =>
        fetch(`/site/v1/sources/${encodeURIComponent(rec.source_id)}`, { headers: { Accept: "application/json" } })
          .then((r) => (r.ok ? r.json() : null))
          .catch(() => null),
      ),
    );
    // Said only when every answer came back; otherwise the line names the laws and no number.
    if (answers.some((a) => a === null)) {
      summary.textContent = "Nigerian laws.";
      return;
    }
    const published = answers.filter((a) => a.has_full_text === true).length;
    summary.textContent =
      published === 0 ? "Nigerian laws; none published yet." : `Nigerian laws; ${published} published so far.`;
  }

  _setFieldError(control, message, { announce = false } = {}) {
    if (!control) return;
    const errId = control.dataset.error;
    const errEl = errId ? document.getElementById(errId) : null;
    const baseDescribed = (control.getAttribute("aria-describedby") || "")
      .split(/\s+/)
      .filter((id) => id && id !== errId);

    if (message) {
      control.setAttribute("aria-invalid", "true");
      if (errEl) {
        errEl.textContent = message;
        errEl.hidden = false;
        baseDescribed.push(errId);
      }
      if (announce) this._announce(message);
    } else {
      control.setAttribute("aria-invalid", "false");
      if (errEl) {
        errEl.textContent = "";
        errEl.hidden = true;
      }
    }
    if (baseDescribed.length) {
      control.setAttribute("aria-describedby", baseDescribed.join(" "));
    } else {
      control.removeAttribute("aria-describedby");
    }
  }

  _bindComposer() {
    const form = $("composer-form");
    const citationInput = $("citation-input");
    const datalist = $("law-suggestions");
    const input = $("composer-input");
    const slashMenu = $("slash-menu");

    // On a phone, with a thread on screen, the composer folds to one row. It opens when a text field in it is
    // focused, clicked or typed in, and folds again when the focus leaves it or a message is sent.
    // Buttons alone do not open it, so a press on Send in the folded row lands on Send.
    const dock = $("composer-dock");
    const isTextField = (el) =>
      el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement && el.type === "text");
    const openDock = (e) => {
      if (isTextField(e.target)) dock.classList.add("is-open");
    };
    for (const type of ["focusin", "input", "keydown", "pointerdown"]) dock.addEventListener(type, openDock);
    // It folds when the focus goes to something outside it, or on a press outside it. Safari gives
    // a clicked button no focus: pressing Send there moves the focus from the box to nothing, or to
    // the page's main region, which contains the composer. Neither is a reason to fold, since
    // folding then would move Send from under the pointer before the click landed.
    dock.addEventListener("focusout", (e) => {
      const next = e.relatedTarget;
      if (next && !dock.contains(next) && !next.contains(dock)) dock.classList.remove("is-open");
    });
    document.addEventListener("pointerdown", (e) => {
      if (!dock.contains(e.target)) dock.classList.remove("is-open");
    });

    $("mode-quote")?.addEventListener("click", () => this._setMode("quote", { focus: true }));
    $("mode-ask")?.addEventListener("click", () => this._setMode("ask", { focus: true }));

    // Live suggestions from search_catalogue, for laws beyond the short list in records.json.
    // Each search spends one of the visitor's calls, so: none while the typed law is already
    // suggested, one at most every 1.5 seconds, and a newer search cancels an older one.
    let searchTimer = null;
    let searchAbort = null;
    citationInput.addEventListener("input", () => {
      citationInput.dataset.edited = "true";
      if (citationInput.getAttribute("aria-invalid") === "true" && citationInput.value.trim()) {
        this._setFieldError(citationInput, null);
      }
      clearTimeout(searchTimer);
      const q = citationInput.value.split(",")[0].trim();
      if (q.length < 4 || q.startsWith("/") || !datalist || this.isPaused) return;
      const typed = q.toLowerCase();
      if ([...datalist.options].some((o) => o.value.toLowerCase().startsWith(typed))) return;
      const wait = Math.max(400, 1500 - (Date.now() - this._lastSearchAt));
      searchTimer = setTimeout(async () => {
        searchAbort?.abort();
        const controller = new AbortController();
        searchAbort = controller;
        this._lastSearchAt = Date.now();
        try {
          const resp = await fetch(MCP_PATH, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "MCP-Protocol-Version": "2025-06-18",
            },
            body: JSON.stringify({
              jsonrpc: "2.0",
              id: ++this._rpcId,
              method: "tools/call",
              params: { name: "search_catalogue", arguments: { query: q, limit: 8 } },
            }),
            signal: controller.signal,
          });
          if (!resp.ok) return;
          const result = parseJsonRpcText(await resp.text())?.result;
          if (!result || result.isError) return;
          const parsed = JSON.parse(result.content?.[0]?.text || "{}");
          const items = Array.isArray(parsed?.items) ? parsed.items : [];
          if (controller.signal.aborted || !items.length) return;
          const seen = new Set([...datalist.options].map((o) => o.value.split(",")[0].trim().toLowerCase()));
          for (const item of items) {
            const title = typeof item?.title === "string" ? item.title.trim() : "";
            if (!title || seen.has(title.toLowerCase())) continue;
            seen.add(title.toLowerCase());
            const opt = document.createElement("option");
            // The law only: the visitor names the section, the page never guesses one.
            opt.value = `${title}, `;
            datalist.appendChild(opt);
          }
        } catch {
          // Keep the suggestions already shown
        }
      }, wait);
    });

    citationInput.addEventListener("blur", () => {
      if (citationInput.dataset.edited && !citationInput.value.trim()) {
        this._setFieldError(citationInput, CITATION_NEEDED, { announce: true });
      }
    });
    // IME-safe Enter on the one-line field
    citationInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.isComposing || e.keyCode === 229)) e.preventDefault();
    });

    input.addEventListener("input", () => {
      input.dataset.edited = "true";
      this._updateCounter();
      if (this.overLimit) {
        const max = this.mode === "quote" ? MAX_QUOTE_LEN : MAX_QUESTION_LEN;
        this._setFieldError(input, `That is ${[...input.value].length} characters. Shorten it to ${max} or fewer.`);
      } else if (input.getAttribute("aria-invalid") === "true" && input.value.trim()) {
        this._setFieldError(input, null);
      }
      this._refreshComposerState();
    });

    // Enter sends and Shift+Enter starts a new line, as in any chat. On a touch keyboard Enter
    // stays a new line, and the Send button sends.
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.defaultPrevented) return;
      if (e.isComposing || e.keyCode === 229) return;
      if (slashMenu && !slashMenu.hidden) return;
      if (globalThis.matchMedia?.("(pointer: coarse)").matches) return;
      e.preventDefault();
      form.requestSubmit();
    });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      this._onSubmit();
    });

    const runExample = (altered) => {
      const rec = this.records.find((r) => r.example) || null;
      if (!rec) {
        this._appendNotice("The examples could not be loaded. Type a law, a section and the quoted words.", "grey");
        return;
      }
      this._setMode("quote");
      citationInput.value = `${rec.title}, ${rec.example.section}`;
      input.value = altered ? rec.example.altered_quote : rec.example.true_quote;
      // The pill disappears with the welcome text, so the focus goes to the composer first.
      input.focus();
      form.requestSubmit();
    };
    $("try-true-quote")?.addEventListener("click", () => runExample(false));
    $("try-altered-quote")?.addEventListener("click", () => runExample(true));
    $("try-question")?.addEventListener("click", () => {
      const rec = this.records.find((r) => r.example) || this.records[0];
      this._setMode("ask");
      input.value = rec
        ? `What does ${rec.title}${rec.example?.section ? `, ${rec.example.section}` : ""} say? Quote the words.`
        : "What does the Freedom of Information Act 2011 say about who may ask for information? Quote the words.";
      input.focus();
      form.requestSubmit();
    });
  }

  _claimDefaults(citation) {
    const rec = this.records.find((r) => citation.toLowerCase().includes(String(r.title || "").toLowerCase()));
    return { jurisdiction: rec?.jurisdiction || "ng", language: rec?.language || "en" };
  }

  async _onSubmit() {
    if (this.isBusy) return;
    const input = $("composer-input");
    const citationInput = $("citation-input");
    const text = input.value.trim();

    // A saved proof pasted into the box is verified, whatever the mode.
    if (looksLikeProof(text)) {
      input.value = "";
      this._updateCounter();
      await this._verifyProofText(text, "pasted text");
      return;
    }

    const serviceBlocked = this.isPaused || Boolean(this.rateLimitTimer);

    if (this.mode === "quote") {
      const citation = citationInput.value.trim();
      const citationOk = Boolean(citation);
      this._setFieldError(citationInput, citationOk ? null : CITATION_NEEDED);
      let quoteMessage = null;
      if (!text) quoteMessage = "Paste the quoted words to check.";
      else if ([...text].length > MAX_QUOTE_LEN) {
        quoteMessage = `That is ${[...text].length} characters. Shorten it to ${MAX_QUOTE_LEN} or fewer.`;
      }
      this._setFieldError(input, quoteMessage);
      if (!citationOk || quoteMessage) {
        (citationOk ? input : citationInput).focus();
        this._announce(citationOk ? quoteMessage : CITATION_NEEDED);
        return;
      }
      if (serviceBlocked) return;
      const claim = { citation, quote: text, ...this._claimDefaults(citation) };
      this._beginTurn();
      this._appendUser({ cite: citation, quote: text });
      this._clearComposerText();
      await this._runDirectCheck(claim, { source: "form" });
      return;
    }

    if (!text) {
      this._setFieldError(input, "Type a question, or switch to Check a quote.", { announce: true });
      input.focus();
      return;
    }
    if ([...text].length > MAX_QUESTION_LEN) {
      this._setFieldError(input, `Shorten your question to ${MAX_QUESTION_LEN} characters or fewer.`, { announce: true });
      input.focus();
      return;
    }
    this._setFieldError(input, null);
    if (serviceBlocked) return;

    // A message that is plainly a citation and a quoted passage is checked directly, with no AI.
    const direct = parseCitationAndQuote(text);
    if (direct) {
      this._beginTurn();
      this._appendUser({ text });
      this._clearComposerText();
      await this._runDirectCheck({ ...direct, ...this._claimDefaults(direct.citation) }, { source: "form" });
      return;
    }

    if (!this.hasProviderKey) {
      // The question stays in the box, so nothing typed is lost while a key is added.
      this._appendKeyNeeded();
      return;
    }

    this._beginTurn();
    this._appendUser({ text });
    this._clearComposerText();
    await this._runAiTurn(text);
  }

  _beginTurn() {
    this.turnSeq += 1;
    // Something was sent, so the earlier "add a key" note has served its purpose.
    $("thread-list").querySelector(".lc-msg-note[data-kind='key-needed']")?.remove();
  }

  _clearComposerText() {
    const input = $("composer-input");
    input.value = "";
    // On a phone sending folds the composer, so the result has the window (law-check.css folds it
    // there only). The focus stays in the box, and the next key or click opens it again. On a touch
    // screen the keyboard is put away as well.
    $("composer-dock")?.classList.remove("is-open");
    if (globalThis.matchMedia?.("(pointer: coarse)").matches) document.activeElement?.blur?.();
    delete input.dataset.edited;
    delete $("citation-input").dataset.edited;
    this._updateCounter();
  }

  /** Put a refused draft back in the composer so it can be corrected, never over something newer. */
  _restoreDraft(claim) {
    const input = $("composer-input");
    const citationInput = $("citation-input");
    if (input.value.trim()) return;
    this._setMode("quote");
    if (claim?.citation && !citationInput.value.trim()) citationInput.value = claim.citation;
    if (claim?.quote) input.value = claim.quote;
    this._updateCounter();
  }

  _bindSlashMenu() {
    const slashMenu = $("slash-menu");
    const citationInput = $("citation-input");
    const quoteInput = $("composer-input");

    const items = slashMenu ? [...slashMenu.querySelectorAll(".lc-slash-item")] : [];
    let activeSlashInput = null;
    let activeIndex = -1;
    const visibleItems = () => items.filter((b) => !b.hidden);

    const setActive = (idx) => {
      const vis = visibleItems();
      activeIndex = vis.length ? (idx + vis.length) % vis.length : -1;
      for (const b of items) b.setAttribute("aria-selected", "false");
      const current = vis[activeIndex];
      if (current) {
        current.setAttribute("aria-selected", "true");
        activeSlashInput?.setAttribute("aria-activedescendant", current.id);
      } else {
        activeSlashInput?.removeAttribute("aria-activedescendant");
      }
    };

    const closeSlashMenu = ({ clear = false } = {}) => {
      if (!slashMenu || slashMenu.hidden) return;
      slashMenu.hidden = true;
      activeIndex = -1;
      if (activeSlashInput) {
        activeSlashInput.removeAttribute("aria-activedescendant");
        if (clear && activeSlashInput.value.trimStart().startsWith("/")) {
          activeSlashInput.value = "";
          // A typed command is not an attempt at the field, so it leaves no error behind.
          delete activeSlashInput.dataset.edited;
        }
      }
    };

    const runSlashCommand = (cmd, sourceEl) => {
      if (sourceEl && sourceEl.value.trimStart().startsWith("/")) {
        sourceEl.value = "";
        delete sourceEl.dataset.edited;
        this._updateCounter();
      }
      closeSlashMenu();
      if (cmd === "quote") this._setMode("quote", { focus: true });
      else if (cmd === "ask") this._setMode("ask", { focus: true });
      else if (cmd === "verify") $("proof-file")?.click();
      else if (cmd === "key") $("open-provider-modal")?.click();
      else if (cmd === "new") this._newChat();
    };

    for (const field of [citationInput, quoteInput]) {
      if (!field || !slashMenu) continue;
      field.setAttribute("aria-controls", "slash-menu");
      field.addEventListener("input", () => {
        const val = field.value.trimStart();
        if (val.startsWith("/") && !/\s/.test(val)) {
          activeSlashInput = field;
          const query = val.slice(1).toLowerCase();
          for (const btn of items) btn.hidden = !btn.dataset.cmd.startsWith(query);
          const shown = visibleItems();
          if (!shown.length) {
            closeSlashMenu();
            return;
          }
          const wasHidden = slashMenu.hidden;
          slashMenu.hidden = false;
          setActive(0);
          if (wasHidden) {
            this._announce(
              `${shown.length} ${shown.length === 1 ? "command" : "commands"}. Up and Down to choose, Enter to run, Escape to close.`,
            );
          }
        } else if (activeSlashInput === field) {
          closeSlashMenu();
        }
      });
      field.addEventListener("keydown", (e) => {
        if (!slashMenu || slashMenu.hidden || activeSlashInput !== field) return;
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setActive(activeIndex + 1);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          setActive(activeIndex - 1);
        } else if (e.key === "Escape") {
          e.preventDefault();
          closeSlashMenu({ clear: true });
        } else if (e.key === "Enter") {
          const current = visibleItems()[activeIndex];
          if (current) {
            e.preventDefault();
            runSlashCommand(current.dataset.cmd, field);
          }
        }
      });
      // Leaving the field closes the menu and removes the half-typed command.
      field.addEventListener("blur", () => closeSlashMenu({ clear: true }));
    }

    // A press on the menu must not take the focus from the field, or the blur above would close it first.
    slashMenu?.addEventListener("pointerdown", (e) => e.preventDefault());
    slashMenu?.addEventListener("click", (e) => {
      const btn = e.target.closest(".lc-slash-item");
      if (btn) runSlashCommand(btn.dataset.cmd, activeSlashInput);
    });
  }

  _bindProviderModal() {
    const PRESETS = {
      openai: {
        name: "OpenAI",
        router: "https://api.openai.com/v1",
        model: "gpt-4o-mini",
        keyUrl: "https://platform.openai.com/api-keys",
        keyLabel: "OpenAI",
      },
      anthropic: {
        name: "Claude (Anthropic)",
        router: "https://api.anthropic.com/v1",
        model: "claude-sonnet-4-6",
        keyUrl: "https://console.anthropic.com/settings/keys",
        keyLabel: "Anthropic Console",
      },
      gemini: {
        name: "Google Gemini",
        router: "https://generativelanguage.googleapis.com/v1beta/openai",
        model: "gemini-2.5-flash",
        keyUrl: "https://aistudio.google.com/apikey",
        keyLabel: "Google AI Studio",
      },
      openrouter: {
        name: "OpenRouter",
        router: "https://openrouter.ai/api/v1",
        model: "openai/gpt-4o-mini",
        keyUrl: "https://openrouter.ai/settings/keys",
        keyLabel: "OpenRouter",
      },
      otari: {
        name: "Otari",
        router: "https://api.otari.ai/api/v1",
        model: "openai/gpt-4o-mini",
        keyUrl: "https://otari.ai/dashboard#/keys",
        keyLabel: "Otari",
      },
      custom: {
        name: "Custom provider",
        router: "",
        model: "",
        keyUrl: "",
        keyLabel: "",
      },
    };

    const dialog = document.getElementById("provider-dialog");
    const openBtn = document.getElementById("open-provider-modal");
    const closeBtn = document.getElementById("provider-dialog-close");
    const cancelBtn = document.getElementById("provider-cancel-btn");
    const removeBtn = document.getElementById("provider-remove-btn");
    const form = document.getElementById("provider-form");
    const keyInput = document.getElementById("provider-key-input");
    const toggleKeyBtn = document.getElementById("toggle-key-visibility");
    const modelInput = document.getElementById("provider-model-input");
    const urlField = document.getElementById("provider-url-field");
    const urlInput = document.getElementById("provider-url-input");
    const keyHint = document.getElementById("provider-key-hint");
    const savedNote = document.getElementById("provider-key-saved");
    const presetBtns = [...document.querySelectorAll(".lc-preset-btn")];
    const pillLabel = document.getElementById("provider-pill-label");
    const pillDot = document.getElementById("provider-dot");
    if (!dialog || !form) return;

    let activePreset = "openrouter";
    let savedKey = "";
    let dirty = false;
    let byokMod = null;
    const loadByok = async () => {
      if (!byokMod) byokMod = await import("./runtime/byok.js");
      return byokMod;
    };

    const detectPreset = (router = "") => {
      const r = String(router).toLowerCase();
      if (r.includes("openai.com")) return "openai";
      if (r.includes("anthropic.com")) return "anthropic";
      if (r.includes("generativelanguage.googleapis.com")) return "gemini";
      if (r.includes("openrouter")) return "openrouter";
      if (r.includes("otari")) return "otari";
      return router ? "custom" : "openai";
    };

    const selectPreset = (presetKey, { keepValues = false } = {}) => {
      activePreset = PRESETS[presetKey] ? presetKey : "openrouter";
      const info = PRESETS[activePreset];
      for (const btn of presetBtns) {
        btn.setAttribute("aria-pressed", btn.dataset.preset === activePreset ? "true" : "false");
      }
      urlField.hidden = activePreset !== "custom";
      urlInput.required = activePreset === "custom";
      if (!keepValues) {
        // Custom starts with an empty address, never the previous provider's.
        urlInput.value = info.router;
        // A model the visitor typed is kept; only a provider's own default is swapped.
        const defaults = Object.values(PRESETS).map((preset) => preset.model).filter(Boolean);
        const current = modelInput.value.trim();
        if (!current || defaults.includes(current)) modelInput.value = info.model;
      }
      if (keyHint) {
        keyHint.replaceChildren();
        if (info.keyUrl) {
          keyHint.append("Get a key from ");
          const a = document.createElement("a");
          a.href = info.keyUrl;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = `${info.keyLabel} ↗`;
          keyHint.appendChild(a);
        } else {
          keyHint.textContent = "Paste the API key for your OpenAI-compatible endpoint.";
        }
      }
    };

    const refreshProviderUi = async () => {
      try {
        const byok = await loadByok();
        const profile = byok.getSelectedByokProfile();
        const hasKey = Boolean(profile?.key && profile?.router);
        this.hasProviderKey = hasKey;
        if (pillLabel) {
          pillLabel.textContent = hasKey
            ? `${profile.name || "AI"} · ${profile.model || "ready"}`
            : "Add AI key (optional)";
        }
        if (pillDot) {
          pillDot.classList.toggle("is-connected", hasKey);
        }
        this.providerName = hasKey ? profile.name || "your AI provider" : "";
        const tryQuestion = document.getElementById("try-question");
        if (tryQuestion) tryQuestion.hidden = !hasKey;
        // A visitor who already saved a key and gave no address starts on Ask AI.
        if (hasKey && !this.modeChosen && this.mode !== "ask") {
          this._setMode("ask", { updateHash: false, chosen: false });
        }
        this._refreshComposerState();
        if (removeBtn) removeBtn.hidden = !hasKey;
        if (hasKey && this.mode === "ask") this._ensureRuntimeLoaded();
        return profile;
      } catch {
        return null;
      }
    };

    this._refreshProviderUi = refreshProviderUi;

    const resetKeyVisibility = () => {
      keyInput.type = "password";
      if (toggleKeyBtn) toggleKeyBtn.textContent = "Show";
    };

    // What opened the dialog, so the focus can go back to it. Safari gives a clicked button no
    // focus, so the browser alone would have nowhere to return it.
    let opener = null;
    let returnFocus = true;
    const openDialog = async (event) => {
      opener = event?.currentTarget instanceof HTMLElement ? event.currentTarget : openBtn;
      returnFocus = true;
      const profile = await refreshProviderUi();
      keyInput.value = "";
      if (profile?.key) {
        const pKey = detectPreset(profile.router);
        selectPreset(pKey, { keepValues: true });
        // The saved key is never put back in the field: only its last four characters are shown.
        savedKey = profile.key;
        keyInput.required = false;
        keyInput.placeholder = "Paste a new key to replace it";
        if (savedNote) {
          savedNote.textContent = `A key ending ${profile.key.slice(-4)} is saved. Leave the field empty to keep it.`;
          savedNote.hidden = false;
          keyInput.setAttribute("aria-describedby", "provider-key-hint provider-key-saved");
        }
        modelInput.value = profile.model || PRESETS[pKey].model;
        urlInput.value = profile.router || PRESETS[pKey].router;
      } else {
        savedKey = "";
        keyInput.required = true;
        keyInput.placeholder = "Paste your API key";
        if (savedNote) {
          savedNote.hidden = true;
          savedNote.textContent = "";
          keyInput.setAttribute("aria-describedby", "provider-key-hint");
        }
        modelInput.value = "";
        selectPreset("openai");
      }
      resetKeyVisibility();
      dirty = false;
      dialog.setAttribute("closedby", "any");
      this._setFieldError(keyInput, null);
      this._setFieldError(modelInput, null);
      this._setFieldError(urlInput, null);
      dialog.showModal();
    };

    // Light-dismiss fallback when closedby="any" is unsupported (e.g. Safari)
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      let pressedOutside = false;
      dialog.addEventListener("pointerdown", (e) => { pressedOutside = outside(e); });
      dialog.addEventListener("pointerup", (e) => { if (pressedOutside && outside(e) && !dirty) dialog.close(); });
    }

    // Once something is typed, a click outside no longer closes the dialog and loses it.
    for (const field of [keyInput, modelInput, urlInput]) {
      field.addEventListener("input", () => {
        dirty = true;
        dialog.setAttribute("closedby", "closerequest");
      });
    }

    dialog.addEventListener("close", () => {
      dialog.returnValue = "";
      keyInput.value = "";
      resetKeyVisibility();
      // After the browser's own handling of the close, so its choice does not undo this one.
      if (returnFocus && opener?.isConnected) setTimeout(() => opener.focus(), 0);
    });

    for (const btn of presetBtns) {
      btn.addEventListener("click", () => selectPreset(btn.dataset.preset));
    }

    toggleKeyBtn?.addEventListener("click", () => {
      const showing = keyInput.type === "text";
      keyInput.type = showing ? "password" : "text";
      toggleKeyBtn.textContent = showing ? "Show" : "Hide";
    });

    openBtn?.addEventListener("click", openDialog);
    closeBtn?.addEventListener("click", () => dialog.close());
    cancelBtn?.addEventListener("click", () => dialog.close());

    removeBtn?.addEventListener("click", async () => {
      const byok = await loadByok();
      const profile = byok.getSelectedByokProfile();
      if (profile?.id) byok.deleteByokProfile(profile.id);
      byok.saveTabByokSetup(null);
      savedKey = "";
      const chatStarted = this.runtimeLoaded || this.runtimeLoading;
      dialog.close();
      await refreshProviderUi();
      this._announce("AI provider key removed from this browser.");
      // A chat that has started still holds the key in memory; reloading drops it.
      if (chatStarted) location.reload();
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const key = keyInput.value.trim() || savedKey;
      const model = modelInput.value.trim();
      const router = activePreset === "custom" ? urlInput.value.trim() : PRESETS[activePreset].router;

      let ok = true;
      if (!key) {
        this._setFieldError(keyInput, "Paste an API key for the chosen provider.", { announce: true });
        ok = false;
      } else {
        this._setFieldError(keyInput, null);
      }
      if (!model) {
        this._setFieldError(modelInput, "Enter a model name.", { announce: ok });
        ok = false;
      } else {
        this._setFieldError(modelInput, null);
      }
      if (activePreset === "custom" && !/^https:\/\//i.test(router)) {
        this._setFieldError(urlInput, "Enter a valid https:// base URL.", { announce: ok });
        ok = false;
      } else {
        this._setFieldError(urlInput, null);
      }
      if (!ok) {
        form.querySelector('[aria-invalid="true"]')?.focus();
        return;
      }

      const byok = await loadByok();
      const existing = byok.getSelectedByokProfile();
      const name = PRESETS[activePreset].name;
      byok.upsertByokProfile(
        { id: existing?.id, name, router, model, key },
        { select: true },
      );
      byok.saveTabByokSetup({ router, model, key });
      const chatStarted = this.runtimeLoaded || this.runtimeLoading;
      // After a save the focus goes to the message box (below), not back to the button.
      returnFocus = false;
      dialog.close();
      // On Ask AI this also starts the chat, now that there is a key.
      await refreshProviderUi();
      this._announce(`Saved. Ask AI now uses ${name}.`);
      // A chat that has already started read its key at start, so it is reloaded to pick up the new one.
      // The thread is kept in the tab and comes back after the reload.
      if (chatStarted) location.reload();
      else this._setMode("ask", { focus: true });
    });

    refreshProviderUi();
  }

  // ---- The thread -------------------------------------------------------------------------------

  _appendItem(li, { reveal = true } = {}) {
    const list = $("thread-list");
    // While a turn is running its status line stays last; everything else goes above it.
    if (this.statusItem?.isConnected && li !== this.statusItem) list.insertBefore(li, this.statusItem);
    else list.appendChild(li);
    const welcome = $("welcome");
    if (welcome) welcome.hidden = true;
    $("lc-chat")?.classList.add("has-thread");
    // From here the page is a chat: the header and composer keep their places and the thread scrolls.
    document.body.classList.add("lc-threaded");
    const newChat = $("new-chat");
    if (newChat) newChat.hidden = false;
    if (reveal) this._reveal(li);
    return li;
  }

  _reveal(el) {
    if (typeof el.scrollIntoView !== "function") return;
    const scroller = $("thread-scroll");
    const room = (scroller ? scroller.clientHeight : window.innerHeight) - 24;
    // A card taller than the free space is shown from its top, where its result is.
    const block = el.getBoundingClientRect().height > room ? "start" : "nearest";
    el.scrollIntoView({ behavior: scrollBehavior(), block });
  }

  _save(item) {
    this.items.push(item);
    if (this.items.length > MAX_SAVED_ITEMS) this.items = this.items.slice(-MAX_SAVED_ITEMS);
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(this.items));
    } catch {
      // sessionStorage unavailable or full
    }
  }

  _appendUser({ text = "", cite = "", quote = "" }, { save = true, reveal = true } = {}) {
    const li = document.createElement("li");
    li.className = "lc-msg lc-msg-user";
    const who = document.createElement("span");
    who.className = "sr-only";
    who.textContent = "You: ";
    li.appendChild(who);
    if (cite) {
      const c = document.createElement("span");
      c.className = "lc-user-cite";
      c.textContent = cite;
      const q = document.createElement("span");
      q.className = "lc-user-quote";
      q.textContent = `“${shorten(quote, 320)}”`;
      li.append(c, q);
    } else {
      const t = document.createElement("span");
      t.className = "lc-user-text";
      t.textContent = shorten(text, 1200);
      li.appendChild(t);
    }
    this._appendItem(li, { reveal });
    if (save) this._save({ t: "user", text: shorten(text, 1200), cite, quote: shorten(quote, 320) });
    return li;
  }

  _appendAi(text, summary, { save = true, reveal = true } = {}) {
    const li = document.createElement("li");
    li.className = "lc-msg lc-msg-ai";
    const label = document.createElement("p");
    label.className = "lc-ai-label";
    label.textContent = "AI's answer. Only quotes with a result card were checked.";
    const body = document.createElement("div");
    body.className = "lc-ai-text";
    renderAnswerText(body, text);
    li.append(label, body);
    if (summary?.text) {
      const foot = document.createElement("p");
      foot.className = "lc-ai-summary";
      if (summary.tone) foot.dataset.tone = summary.tone;
      foot.textContent = summary.text;
      li.appendChild(foot);
    }
    this._appendItem(li, { reveal });
    if (save) this._save({ t: "ai", text: shorten(text, 12000), summary: summary || null });
    return li;
  }

  _appendDivider(text, { save = true, reveal = true } = {}) {
    const li = document.createElement("li");
    li.className = "lc-msg lc-divider";
    li.textContent = text;
    this._appendItem(li, { reveal });
    if (save) this._save({ t: "divider", text });
    return li;
  }

  /** One line saying what is happening now. It is replaced by the result and never saved. */
  _appendStatus(text) {
    this.statusItem?.remove();
    const li = document.createElement("li");
    li.className = "lc-msg lc-msg-status";
    const dot = document.createElement("span");
    dot.className = "lc-status-dot";
    dot.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = text;
    li.append(dot, label);
    this.statusItem = li;
    this._appendItem(li);
    return {
      update: (next) => {
        if (next && label.textContent !== next) label.textContent = next;
      },
      remove: () => {
        li.remove();
        if (this.statusItem === li) this.statusItem = null;
      },
    };
  }

  _appendNotice(message, tone = "grey", { retryFn = null, actions = [], list = null, announce = true } = {}) {
    const li = document.createElement("li");
    li.className = "lc-msg lc-msg-note";
    const box = document.createElement("div");
    box.className = "lc-notice-card";
    box.dataset.tone = tone;
    const p = document.createElement("p");
    p.textContent = message;
    box.appendChild(p);
    if (Array.isArray(list) && list.length) {
      const ul = document.createElement("ul");
      for (const line of list) {
        const item = document.createElement("li");
        item.textContent = line;
        ul.appendChild(item);
      }
      box.appendChild(ul);
    }
    const all = [...actions];
    if (typeof retryFn === "function") {
      all.push({
        label: "Try again",
        onClick: () => {
          li.remove();
          retryFn();
        },
      });
    }
    if (all.length) {
      const row = document.createElement("div");
      row.className = "lc-notice-actions";
      for (const action of all) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = action.primary ? "lc-btn-primary" : "lc-btn-secondary";
        btn.textContent = action.label;
        btn.addEventListener("click", () => action.onClick(li, row));
        row.appendChild(btn);
      }
      box.appendChild(row);
    }
    li.appendChild(box);
    this._appendItem(li);
    if (announce) this._announce(message);
    return li;
  }

  _appendKeyNeeded() {
    $("thread-list").querySelector(".lc-msg-note[data-kind='key-needed']")?.remove();
    const li = this._appendNotice(
      "Asking a question needs an AI provider and your own key. Your question is still in the box. Checking a quote needs neither.",
      "grey",
      {
        actions: [
          { label: "Add AI key", primary: true, onClick: () => $("open-provider-modal")?.click() },
          { label: "Check a quote instead", onClick: (item) => { item.remove(); this._setMode("quote", { focus: true }); } },
        ],
      },
    );
    li.dataset.kind = "key-needed";
  }

  _appendResult(entry, { persist = true, announce = false, reveal = true, collapsed = false } = {}) {
    const list = $("thread-list");
    // A new result closes the cards of earlier turns to their band, which still says the result.
    if (reveal) {
      for (const existing of list.querySelectorAll("oa-receipt")) {
        if (existing.dataset.turn !== String(this.turnSeq)) existing.collapsed = true;
      }
    }

    const li = document.createElement("li");
    li.className = "lc-msg lc-msg-result";
    const caption = document.createElement("p");
    caption.className = "lc-msg-caption";
    caption.textContent =
      entry.source === "agent"
        ? "Checked by Open Alvary, on a section the AI named"
        : entry.source === "recheck"
          ? "Saved proof, checked by Open Alvary"
          : "Checked by Open Alvary. No AI was used.";
    const card = document.createElement("oa-receipt");
    if (this.panelWide?.matches) card.setAttribute("panel", "");
    card.setData({
      receipt: entry.receipt,
      claim: entry.claim,
      verification: entry.verification,
      source: entry.source,
      collapsible: true,
      collapsed,
    });
    card.dataset.turn = String(this.turnSeq);
    // Beside the receipt panel, a card is chosen with this button, or by clicking the card.
    const show = document.createElement("button");
    show.type = "button";
    show.className = "lc-panel-btn";
    show.textContent = "Show the receipt";
    show.addEventListener("click", () => this._selectCard(card, { focusPanel: true }));
    const head = document.createElement("div");
    head.className = "lc-msg-head";
    head.append(caption, show);
    card.addEventListener("click", (event) => {
      const interactive = event.composedPath().some((node) => ["A", "BUTTON", "SUMMARY", "TEXTAREA"].includes(node?.tagName));
      if (!interactive) this._selectCard(card);
    });
    li.append(head, card);
    this._appendItem(li, { reveal });
    if (reveal || !collapsed) this._selectCard(card);

    if (entry.receipt.receipt_id) this.cardByReceiptId.set(entry.receipt.receipt_id, card);
    const sid = entry.receipt.part?.source_id;
    if (sid) {
      const arr = this.cardsBySourceId.get(sid) || [];
      arr.unshift(card);
      this.cardsBySourceId.set(sid, arr);
    }

    if (persist) this._save({ t: "result", receipt: entry.receipt, claim: entry.claim, source: entry.source });

    if (announce) {
      const title = entry.receipt.part
        ? `${entry.receipt.part.title}, ${entry.receipt.part.pinpoint}`
        : entry.claim?.citation || entry.receipt.status;
      const wl = wordsLineForReceipt(entry.receipt).text;
      const rl = recordLinesForReceipt(entry.receipt)[0]?.text || "";
      this._announce(`${title}. ${wl}. ${rl}`);
    }
    return card;
  }

  // Shows a card's check in full in the receipt panel and marks the card as the one shown.
  _selectCard(card, { focusPanel = false } = {}) {
    this.selectedCard = card;
    for (const other of $("thread-list").querySelectorAll("oa-receipt")) {
      other.toggleAttribute("selected", other === card);
    }
    this._syncPanel();
    if (focusPanel && this.panelWide?.matches) $("receipt-panel")?.querySelector('[role="tab"][aria-selected="true"]')?.focus();
  }

  // The panel shows on a wide window once there is a check to show; on a narrow one each card
  // keeps its own details, as before.
  _syncPanel() {
    const panel = $("receipt-panel");
    if (!panel) return;
    const wide = Boolean(this.panelWide?.matches && this.selectedCard?.isConnected);
    for (const card of $("thread-list").querySelectorAll("oa-receipt")) {
      card.toggleAttribute("panel", Boolean(this.panelWide?.matches));
    }
    panel.hidden = !wide;
    document.querySelector(".law-check-main")?.classList.toggle("has-panel", wide);
    if (wide) renderReceiptPanel(panel, this.selectedCard, { tab: panel.dataset.tab });
  }

  async _restoreThread() {
    let saved = null;
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) saved = JSON.parse(raw);
      if (!saved) {
        // Results kept by the earlier two-panel page: newest first, results only.
        const old = JSON.parse(sessionStorage.getItem(OLD_STORAGE_KEY) || "null");
        if (Array.isArray(old)) {
          saved = old
            .filter((e) => e?.receipt)
            .reverse()
            .map((e) => ({ t: "result", receipt: e.receipt, claim: e.claim || null, source: e.source || "form" }));
          sessionStorage.removeItem(OLD_STORAGE_KEY);
        }
      }
    } catch {
      saved = null;
    }
    if (!Array.isArray(saved) || !saved.length) return;

    const hasResults = saved.some((item) => item?.t === "result");
    const jwks = hasResults ? await this._ensureJwks() : null;
    const lastResult = saved.map((item) => item?.t).lastIndexOf("result");
    const kept = [];

    for (let i = 0; i < saved.length; i++) {
      const item = saved[i];
      if (!item || typeof item !== "object") continue;
      if (item.t === "user") {
        this._appendUser(item, { save: false, reveal: false });
        kept.push(item);
      } else if (item.t === "ai" && typeof item.text === "string") {
        this._appendAi(item.text, item.summary, { save: false, reveal: false });
        kept.push(item);
      } else if (item.t === "divider" && typeof item.text === "string") {
        this._appendDivider(item.text, { save: false, reveal: false });
        kept.push(item);
      } else if (item.t === "result" && item.receipt && jwks) {
        // Every saved receipt is verified again before it is drawn (§10.3).
        const verification = await verifyReceipt(item.receipt, jwks, { claim: item.claim });
        if (!verification.ok) continue;
        this._appendResult(
          { receipt: item.receipt, claim: item.claim || null, verification, source: item.source || "form" },
          { persist: false, announce: false, reveal: false, collapsed: i !== lastResult },
        );
        kept.push(item);
      }
    }
    this.items = kept;
    if (kept.some((item) => item.t === "ai") && kept[kept.length - 1]?.t !== "divider") {
      // The page was reloaded: the thread is shown again, but the AI starts without it.
      this._appendDivider("Earlier in this tab. The AI does not remember it.", { save: false, reveal: false });
    }
    // A chat opens at its latest message. Set at once, with no animation.
    const scroller = $("thread-scroll");
    if (scroller && kept.length) scroller.scrollTop = scroller.scrollHeight;
  }

  _newChat() {
    if (this.isBusy) return;
    this.runtime?.engine?.resetHistory?.();
    if (!$("thread-list").children.length) return;
    this._beginTurn();
    this._appendDivider("New chat");
    this._announce("New chat. Earlier results stay above.");
    $("composer-input")?.focus();
  }

  // ---- Saved proofs -----------------------------------------------------------------------------

  _bindProof() {
    const fileInput = $("proof-file");
    const dock = $("composer-dock");
    $("attach-proof")?.addEventListener("click", () => fileInput?.click());
    fileInput?.addEventListener("change", () => {
      const file = fileInput.files?.[0];
      fileInput.value = "";
      if (file) this._loadProofFile(file);
    });
    if (!dock) return;
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    dock.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dock.classList.add("is-dragover");
    });
    dock.addEventListener("dragleave", () => dock.classList.remove("is-dragover"));
    dock.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dock.classList.remove("is-dragover");
      const file = e.dataTransfer?.files?.[0];
      if (file) this._loadProofFile(file);
    });
  }

  async _loadProofFile(file) {
    if (file.size > MAX_PROOF_BYTES) {
      this._appendNotice("That file is too large to be a saved proof. Choose the .json file you saved from a result.", "red");
      return;
    }
    let text = "";
    try {
      text = await file.text();
    } catch {
      this._appendNotice("Could not read that file. Choose the .json file you saved from a result.", "red");
      return;
    }
    await this._verifyProofText(text, file.name || "file");
  }

  async _verifyProofText(raw, label) {
    if (this.isBusy) return;
    this._beginTurn();
    this._appendUser({ text: `Saved proof (${label})` });
    const incomplete = "This is not a complete receipt. Copy it again from where you saved it.";

    let parsed;
    try {
      parsed = JSON.parse(String(raw || "").trim());
    } catch {
      this._appendNotice(incomplete, "red");
      return;
    }
    const oldReceipt = parsed?.receipt && typeof parsed.receipt === "object" ? parsed.receipt : parsed;
    const oldClaim = parsed?.receipt && typeof parsed.receipt === "object" ? parsed.claim || null : null;
    if (!oldReceipt || typeof oldReceipt !== "object" || !oldReceipt.schema_version) {
      this._appendNotice(incomplete, "red");
      return;
    }

    const jwks = await this._ensureJwks();
    if (!jwks) {
      this._appendNotice("Could not load Open Alvary's key list to check the signature. Try again.", "amber", {
        retryFn: () => this._verifyProofText(raw, label),
      });
      return;
    }
    const check = await verifyReceipt(oldReceipt, jwks, { claim: oldClaim });
    if (!check.ok) {
      this._appendNotice(verificationErrorMessage(check.reason), "red");
      return;
    }

    if (this.isPaused) {
      // Checking a signature needs no server, so a saved proof can still be verified while paused.
      this._appendResult(
        { receipt: oldReceipt, claim: oldClaim, verification: check, source: "recheck" },
        { persist: false, announce: true },
      );
      this._appendNotice("The signature is valid. Law check is paused, so the check was not repeated.", "grey");
      return;
    }

    if (!oldReceipt.part) {
      this._appendNotice("The signature is valid, but this receipt names no section, so it cannot be checked again. Check the citation with Check a quote.", "grey");
      return;
    }

    let repeatClaim;
    if (oldClaim) {
      repeatClaim = { ...oldClaim };
      delete repeatClaim.reading;
    } else {
      const matched = oldReceipt.findings?.matched_text;
      if (!matched || [...matched].length > MAX_QUOTE_LEN) {
        this._appendNotice(
          "The signature is valid, but this copy does not hold the quote that was sent. Use the whole saved proof (from Save proof or Copy JSON), or check the quote again with Check a quote.",
          "grey",
        );
        return;
      }
      repeatClaim = { anchor_id: oldReceipt.part.anchor_id, quote: matched };
      if (oldReceipt.claim?.date_sent && oldReceipt.claim?.assessed_at) repeatClaim.as_of = oldReceipt.claim.assessed_at;
      if (oldReceipt.claim?.jurisdiction) repeatClaim.jurisdiction = oldReceipt.claim.jurisdiction;
      if (oldReceipt.part.language) repeatClaim.language = oldReceipt.part.language;
    }

    const fresh = await this._runDirectCheck(repeatClaim, { source: "recheck" });
    if (fresh) this._appendRecheckNote(oldReceipt, fresh.receipt);
  }

  _appendRecheckNote(oldReceipt, newReceipt) {
    const issuedDate = formatLongDate(oldReceipt.issued_at || oldReceipt.claim?.assessed_at);
    const messages = [];
    if (newReceipt.status === "text_not_available") {
      messages.push("There is no published text to check now.");
    } else {
      if (
        newReceipt.findings?.newer_text ||
        (oldReceipt.part?.version_id &&
          newReceipt.part?.version_id &&
          oldReceipt.part.version_id !== newReceipt.part.version_id)
      ) {
        messages.push(`The text has been replaced since ${issuedDate}.`);
      }
      if (newReceipt.status === "wording_differs" && oldReceipt.status !== "wording_differs") {
        messages.push("These words are no longer in the section.");
      }
      const oldState = oldReceipt.findings?.currentness?.state;
      const newState = newReceipt.findings?.currentness?.state;
      if (oldState && newState && oldState !== newState) {
        messages.push(
          newState === "not_in_force"
            ? "No longer recorded as in force."
            : `Record status changed from ${oldState.replace(/_/g, " ")} to ${newState.replace(/_/g, " ")}.`,
        );
      }
    }
    if (!messages.length) messages.push(`Same words, same record as when checked on ${issuedDate}.`);
    this._appendNotice("The saved proof is genuine. Checked again today:", "grey", { list: messages });
  }

  async _ensureJwks() {
    if (this.jwks) return this.jwks;
    try {
      this.jwks = await loadReceiptKeys(LOCAL_KEYS_PATH);
      return this.jwks;
    } catch {
      try {
        this.jwks = await loadReceiptKeys(RECEIPT_KEYS_URL);
        return this.jwks;
      } catch {
        return null;
      }
    }
  }

  // ---- A check made by the page itself (no AI) -----------------------------------------------

  async _runDirectCheck(claim, { source = "form" } = {}) {
    this._setBusy(true);
    const status = this._appendStatus("Checking the published text…");
    const slowTimer = setTimeout(() => status.update("Still checking…"), 5000);
    const controller = new AbortController();
    const timeoutTimer = setTimeout(() => controller.abort(), 20000);
    const retry = { retryFn: () => this._runDirectCheck(claim, { source }) };

    this.directCallCount++;
    try {
      const resp = await fetch(MCP_PATH, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "MCP-Protocol-Version": "2025-06-18",
        },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: ++this._rpcId,
          method: "tools/call",
          params: { name: "check_claim", arguments: claim },
        }),
        signal: controller.signal,
      });

      const rpc = resp.ok ? parseJsonRpcText(await resp.text()) : null;
      const result = rpc?.result;
      if (!result || rpc?.error) {
        status.remove();
        this._appendNotice("Could not check now. Try again.", "red", retry);
        return null;
      }

      if (result.isError) {
        status.remove();
        this._handleDirectToolError(parseToolError(result), claim, source);
        return null;
      }

      const envelope = parseCheckClaimEnvelope(result);
      if (!envelope) {
        status.remove();
        this._appendNotice("Could not check now. Try again.", "red", retry);
        return null;
      }

      const jwks = await this._ensureJwks();
      if (!jwks) {
        status.remove();
        this._appendNotice("Could not load Open Alvary's key list to check the signature. Try again.", "amber", retry);
        return null;
      }

      const verification = await verifyReceipt(envelope.receipt, jwks, { claim: envelope.claim || claim });
      status.remove();
      if (!verification.ok) {
        this._appendNotice(verificationErrorMessage(verification.reason), "red");
        return null;
      }

      const entry = { receipt: envelope.receipt, claim: envelope.claim || claim, verification, source };
      this._appendResult(entry, { persist: true, announce: true });
      return entry;
    } catch {
      status.remove();
      this._appendNotice("Could not check now. Try again.", "red", retry);
      return null;
    } finally {
      this.directCallCount = Math.max(0, this.directCallCount - 1);
      clearTimeout(slowTimer);
      clearTimeout(timeoutTimer);
      this._setBusy(false);
    }
  }

  _handleDirectToolError({ code, seconds, text }, claim, source) {
    if (code === "quote_too_short") {
      if (source === "form") this._restoreDraft(claim);
      this._appendNotice("Paste at least 40 characters (about eight words), or the whole section.", "amber");
      return;
    }
    if (code === "quote_too_spread") {
      if (source === "form") this._restoreDraft(claim);
      this._appendNotice("The quoted pieces are too far apart in the section. Quote a shorter passage.", "amber");
      return;
    }
    if (code === "result_too_large") {
      this._appendNotice("That section is too large to check at once. Name a smaller part.", "grey");
      return;
    }
    if (code === "unavailable") {
      this._appendUnavailableNotice();
      return;
    }
    if (code === "paused") {
      this._setPaused();
      return;
    }
    if (code === "rate_limited") {
      this._startRateLimitCountdown(Math.max(1, Number(seconds) || 20));
      return;
    }
    if (code === "service_rate_limited") {
      const wait = Math.max(1, Number(seconds) || 30);
      this._appendNotice(
        `Law check is busy right now. Try again in ${wait} seconds. You can still verify a saved proof.`,
        "amber",
      );
      return;
    }
    if (code === "invalid_arguments" || code === "invalid_request") {
      if (source === "form") this._restoreDraft(claim);
      this._appendNotice("Law check could not read that. Name the law and one section, then paste the quoted words.", "amber");
      return;
    }
    void text;
    this._appendNotice("Could not check now. Try again.", "red", {
      retryFn: () => this._runDirectCheck(claim, { source }),
    });
  }

  _setPaused() {
    this.isPaused = true;
    this._showGlobalBanner("Law check is paused. Try again later. You can still verify a saved proof.", "amber");
    this._refreshComposerState();
  }

  _startRateLimitCountdown(seconds) {
    const text = (n) => `Too many checks from your connection just now. Try again in ${n} ${n === 1 ? "second" : "seconds"}.`;
    const notice = this._appendNotice(text(seconds), "amber");
    const p = notice.querySelector("p");
    let remaining = seconds;
    if (this.rateLimitTimer) clearInterval(this.rateLimitTimer);
    this.rateLimitTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(this.rateLimitTimer);
        this.rateLimitTimer = null;
        if (p) p.textContent = "You can check another quote now.";
        this._refreshComposerState();
      } else if (p) {
        p.textContent = text(remaining);
      }
    }, 1000);
    this._refreshComposerState();
  }

  _appendUnavailableNotice() {
    const titles = this.records.map((rec) => rec.title).filter(Boolean);
    if (titles.length && titles.length <= 12) {
      this._appendNotice("Law check does not cover this record.", "grey", { list: [`It covers: ${titles.join("; ")}.`] });
    } else {
      this._appendNotice("Law check does not cover this record. See Coverage in the menu for what Open Alvary publishes.", "grey");
    }
  }

  // ---- A question answered by the visitor's AI, on the challenge runtime -------------------------

  _ensureRuntimeLoaded() {
    if (this.runtimeLoaded) return Promise.resolve(true);
    if (this.runtimeLoading) return this.runtimeLoading;
    this.runtimeLoading = (async () => {
      try {
        const runtime = await import("./runtime/index.js");
        const booted = await runtime.boot("#agent-app", {
          originUrl: `${location.origin}/agents`,
          sandboxWorkerUrl: "/agents/runtime/sandbox-worker.js",
        });
        // The runtime's own chat is not shown, so its questions to the visitor are asked in this thread.
        booted.engine.onConsent = (info) => this._askConsent(info);
        this.runtime = { engine: booted.engine, trace: booted.trace };
        this.runtimeLoaded = true;
        const agents = $("agents-disclosure");
        if (agents) {
          agents.hidden = false;
          if (this.openAgentsView) agents.open = true;
        }
        return true;
      } catch {
        return false;
      } finally {
        this.runtimeLoading = null;
      }
    })();
    return this.runtimeLoading;
  }

  _askConsent(info) {
    return new Promise((resolve) => {
      const label = shorten(info?.peerLabel || "another site", 80);
      const origin = shorten(info?.peerOrigin || "", 120);
      this._appendNotice(
        `The AI wants to contact ${label}${origin ? ` (${origin})` : ""} on your behalf.`,
        "amber",
        {
          actions: [
            { label: "Allow", primary: true, onClick: (_item, row) => settle("allow", row) },
            { label: "Decline", onClick: (_item, row) => settle("decline", row) },
          ],
        },
      );
      const settle = (decision, row) => {
        const done = document.createElement("p");
        done.textContent = decision === "allow" ? "Allowed." : "Declined.";
        row.replaceWith(done);
        resolve(decision);
      };
    });
  }

  async _runAiTurn(text) {
    this._setBusy(true);
    const status = this._appendStatus("Thinking…");
    const turn = { checked: 0, found: 0, notFound: 0, failed: 0 };
    this.activeAgentTurn = turn;
    this.pendingAgentWork = [];
    try {
      const ready = await this._ensureRuntimeLoaded();
      if (!ready) {
        status.remove();
        this._appendNotice("The assistant could not be loaded. Checking a quote still works.", "amber");
        return;
      }
      let result = null;
      for await (const event of this.runtime.engine.streamTurn(text, {})) {
        if (event.type === "turn.completed") {
          result = event.result;
          continue;
        }
        if (event.visibility === "developer" || event.type === "agent.output.delta") continue;
        status.update(statusLineForEvent(event));
      }
      if (!result) throw new Error("The turn ended without an answer.");
      // Every receipt the agents fetched is verified and drawn before the answer is shown under them.
      await Promise.allSettled(this.pendingAgentWork);
      status.remove();
      const answer = cleanAnswerText(result.text) || "The AI gave no written answer.";
      this._appendAi(answer, this._answerSummary(turn));
      this._announce(
        turn.checked > 0
          ? `Answer ready. ${turn.checked} ${turn.checked === 1 ? "quote" : "quotes"} checked: ${turn.found} found, ${turn.notFound} not found.`
          : "Answer ready. Nothing in this answer was checked.",
      );
    } catch (err) {
      await Promise.allSettled(this.pendingAgentWork);
      status.remove();
      const detail = shorten(String(err?.message || "").replace(/\s+/g, " ").trim(), 240);
      this._appendNotice(
        `Your AI provider could not answer.${detail ? ` It said: ${detail}` : ""} Check the key and model, then try again.`,
        "red",
        { actions: [{ label: "AI key settings", onClick: () => $("open-provider-modal")?.click() }] },
      );
    } finally {
      this.activeAgentTurn = null;
      this.pendingAgentWork = [];
      this._setBusy(false);
    }
  }

  _answerSummary(turn) {
    if (turn.checked === 0 && turn.failed === 0) {
      return { text: "Nothing in this answer was checked.", tone: "red" };
    }
    const parts = [`This answer: ${turn.checked} ${turn.checked === 1 ? "quote" : "quotes"} checked`];
    if (turn.checked > 0) parts.push(`${turn.found} found, ${turn.notFound} not found`);
    if (turn.failed > 0) parts.push(`${turn.failed} could not be checked`);
    return { text: `${parts.join(", ")}. Sentences without a result card were not checked.`, tone: "" };
  }

  async _handleInterceptedAgentReceipt(envelope, args) {
    const jwks = await this._ensureJwks();
    if (!jwks) {
      this._appendNotice("Could not load Open Alvary's key list to check the signature. Try again.", "amber");
      if (this.activeAgentTurn) this.activeAgentTurn.failed += 1;
      return;
    }
    const claim = envelope.claim || args || null;
    const verification = await verifyReceipt(envelope.receipt, jwks, { claim });
    if (!verification.ok) {
      this._appendNotice(verificationErrorMessage(verification.reason), "red");
      if (this.activeAgentTurn) this.activeAgentTurn.failed += 1;
      return;
    }
    this._appendResult({ receipt: envelope.receipt, claim, verification, source: "agent" }, { persist: true, announce: false });
    if (this.activeAgentTurn) {
      this.activeAgentTurn.checked += 1;
      const status = envelope.receipt.status;
      if (status === "matches" || status === "words_found" || status === "words_found_with_omissions") {
        this.activeAgentTurn.found += 1;
      } else {
        this.activeAgentTurn.notFound += 1;
      }
    }
  }

  _handleInterceptedAgentError(errInfo) {
    if (errInfo.code === "paused") {
      this._setPaused();
      return;
    }
    if (errInfo.code === "unavailable") this._appendUnavailableNotice();
    else this._appendNotice("One quote could not be checked now.", "red");
    if (this.activeAgentTurn) this.activeAgentTurn.failed += 1;
  }

  _showGlobalBanner(text, tone = "amber") {
    const banner = document.getElementById("global-banner");
    if (!banner) return;
    banner.textContent = text;
    banner.dataset.tone = tone;
    banner.hidden = false;
    this._announce(text);
  }

  _hideGlobalBanner() {
    const banner = document.getElementById("global-banner");
    if (!banner) return;
    banner.hidden = true;
    banner.textContent = "";
  }

  _announce(text) {
    const live = document.getElementById("live-status");
    if (!live) return;
    live.textContent = "";
    requestAnimationFrame(() => {
      live.textContent = text;
    });
  }
}

export const LawCheckPage = new LawCheckController();

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => LawCheckPage.init(), { once: true });
  } else {
    LawCheckPage.init();
  }
}
