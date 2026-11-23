/**
 * Law check page controller (/agents/site.js).
 *
 * - Installs the fetch wrapper (LC-16) capturing check_claim JSON-RPC responses
 *   from /agents/mcp before the challenge runtime ever loads.
 * - Verifies every receipt via ./receipt.js before rendering <oa-receipt>.
 * - Persists verified receipts in sessionStorage and re-verifies on load.
 * - Lazy-loads ./runtime/index.js only when "Ask a question" is opened.
 * - Zero innerHTML with untrusted data.
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

const STORAGE_KEY = "oa_law_check_receipts_v1";
const MCP_PATH = "/agents/mcp";
const LOCAL_KEYS_PATH = "/.well-known/open-alvary-receipt-keys.json";
const MAX_QUOTE_LEN = 2000;
const COUNTER_SHOW_AT = 1800;

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
    case "crypto_unavailable":
      return "This browser cannot verify receipts.";
    default:
      return "This is not a complete receipt. Copy it again from where you saved it.";
  }
}

class LawCheckController {
  constructor() {
    this.records = [];
    this.jwks = null;
    this.entries = []; // { receipt, claim, verification, source, groupLabel }
    this.cardByReceiptId = new Map();
    this.cardsBySourceId = new Map();
    this.runtimeLoaded = false;
    this.runtimeLoading = false;
    this.isPaused = false;
    this.isChecking = false;
    this.rateLimitTimer = null;
    this.directCallCount = 0;
    this.activeAgentTurn = null;
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
    const isWide = typeof window !== "undefined" && window.innerWidth > 1100;
    // Never moves keyboard focus; scrolls only on wide screens (§10.10, §10.11)
    card.expandAndReveal(isWide);
    return { ok: true, receipt_id };
  }

  init() {
    if (typeof document === "undefined") return;
    if (!document.getElementById("check-form")) return;

    // 1. Install LC-16 fetch interceptor immediately, before any runtime loads.
    installCheckClaimInterceptor({
      onCheckClaim: ({ envelope, arguments: args }) => {
        if (this.directCallCount > 0) return; // direct form/recheck handles its own result
        this._handleInterceptedAgentReceipt(envelope, args);
      },
      onToolError: (errInfo) => {
        if (this.directCallCount > 0) return;
        this._handleInterceptedAgentError(errInfo);
      },
    });

    this._bindMobileNav();
    this._bindTabs();
    this._bindForm();
    this._bindRecheckForm();
    this._bindConnectivity();
    this._bindAlternativeEvents();

    if (!globalThis.crypto?.subtle) {
      this._showGlobalBanner("This browser cannot verify receipts.", "red");
      this._setInteractiveDisabled(true);
      return;
    }

    this._loadRecords();
    this._restoreSessionReceipts();
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

  _bindTabs() {
    const tablist = document.querySelector('[role="tablist"]');
    const tabs = [...document.querySelectorAll('[role="tab"][data-tab]')];
    const panels = [...document.querySelectorAll('[role="tabpanel"][data-panel]')];
    if (!tablist || !tabs.length) return;

    const activate = (name, { updateHash = true } = {}) => {
      for (const tab of tabs) {
        const selected = tab.dataset.tab === name;
        tab.setAttribute("aria-selected", selected ? "true" : "false");
        tab.tabIndex = selected ? 0 : -1;
      }
      for (const panel of panels) {
        const active = panel.dataset.panel === name;
        if (active) {
          panel.removeAttribute("hidden");
        } else {
          panel.setAttribute("hidden", "until-found");
        }
      }
      if (updateHash && typeof history !== "undefined") {
        try {
          history.replaceState(null, "", `${location.pathname}${location.search}#${name}`);
        } catch {
          // ignore
        }
      }
      if (name === "ask") {
        this._ensureRuntimeLoaded();
      }
    };
    this._activateTab = activate;

    for (const tab of tabs) {
      tab.addEventListener("click", () => activate(tab.dataset.tab));
      // Selection by activation: Enter/Space activates; arrows move focus (roving tabindex fallback)
      tab.addEventListener("keydown", (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        const idx = tabs.indexOf(tab);
        if (e.key === "ArrowRight") {
          e.preventDefault();
          const next = tabs[(idx + 1) % tabs.length];
          next.focus();
        } else if (e.key === "ArrowLeft") {
          e.preventDefault();
          const prev = tabs[(idx - 1 + tabs.length) % tabs.length];
          prev.focus();
        } else if (e.key === "Home") {
          e.preventDefault();
          tabs[0].focus();
        } else if (e.key === "End") {
          e.preventDefault();
          tabs[tabs.length - 1].focus();
        } else if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate(tab.dataset.tab);
        }
      });
    }

    // Sync tab selection when browser Find-in-page matches inside a hidden="until-found" panel
    for (const panel of panels) {
      panel.addEventListener("beforematch", () => {
        activate(panel.dataset.panel);
      });
    }
  }

  _syncInitialRoute() {
    const params = new URLSearchParams(location.search);
    const hash = location.hash.replace(/^#/, "");
    const isOpenRouterCallback = params.has("code");
    const isDev = params.get("dev") === "1";

    if (isDev) {
      const agentsDetails = document.getElementById("agents-disclosure");
      if (agentsDetails) agentsDetails.open = true;
    }

    if (isOpenRouterCallback || hash === "ask") {
      this._activateTab?.("ask", { updateHash: false });
    } else if (hash === "recheck" || hash === "verify") {
      this._activateTab?.("check", { updateHash: false });
      const verifyDetails = document.getElementById("verify-proof-disclosure");
      if (verifyDetails) verifyDetails.open = true;
    } else {
      this._activateTab?.("check", { updateHash: false });
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
      this._runDirectCheck(nextClaim, { source: "form" });
    });
  }

  async _loadRecords() {
    const datalist = document.getElementById("law-suggestions");
    const countEl = document.getElementById("record-count");
    try {
      const resp = await fetch("./records.json", { headers: { Accept: "application/json" } });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      this.records = Array.isArray(data?.records) ? data.records : [];
    } catch {
      this.records = [];
    }

    if (countEl && this.records.length > 0) {
      countEl.textContent = String(this.records.length);
    }

    if (datalist) {
      datalist.replaceChildren();
      for (const rec of this.records) {
        const opt = document.createElement("option");
        opt.value = rec.example?.section ? `${rec.title}, ${rec.example.section}` : `${rec.title}, s. 1`;
        datalist.appendChild(opt);
      }
    }
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

  _bindForm() {
    const form = document.getElementById("check-form");
    const citationInput = document.getElementById("citation-input");
    const datalist = document.getElementById("law-suggestions");
    const quoteInput = document.getElementById("quote-input");
    const counter = document.getElementById("quote-counter");
    const submitBtn = document.getElementById("check-submit");
    const trueBtn = document.getElementById("try-true-quote");
    const alteredBtn = document.getElementById("try-altered-quote");

    const validateCitation = ({ announce = false } = {}) => {
      const val = citationInput.value.trim();
      if (!val) {
        this._setFieldError(citationInput, "Enter a citation, for example Freedom of Information Act 2011, s. 1(1).", { announce });
        return false;
      }
      this._setFieldError(citationInput, null);
      return true;
    };

    const validateQuote = ({ announce = false } = {}) => {
      const val = quoteInput.value;
      const len = [...val].length;
      if (len >= COUNTER_SHOW_AT) {
        counter.hidden = false;
        counter.textContent = `${len} of ${MAX_QUOTE_LEN} characters`;
        counter.classList.toggle("over-limit", len > MAX_QUOTE_LEN);
      } else {
        counter.hidden = true;
      }

      if (!val.trim()) {
        this._setFieldError(quoteInput, "Paste the quoted words to check.", { announce });
        return false;
      }
      if (len > MAX_QUOTE_LEN) {
        this._setFieldError(
          quoteInput,
          `Quote is ${len} characters. Shorten it to ${MAX_QUOTE_LEN} characters or fewer.`,
          { announce },
        );
        submitBtn.setAttribute("aria-disabled", "true");
        return false;
      }
      this._setFieldError(quoteInput, null);
      if (!this.isChecking && !this.isPaused && !this.rateLimitTimer) {
        submitBtn.removeAttribute("aria-disabled");
      }
      return true;
    };

    // Live typeahead via search_catalogue on /agents/mcp for large corpora
    let searchTimer = null;
    citationInput.addEventListener("input", () => {
      if (citationInput.getAttribute("aria-invalid") === "true" && citationInput.value.trim()) {
        this._setFieldError(citationInput, null);
      }
      const q = citationInput.value.split(",")[0].trim();
      if (q.length < 3 || !datalist) return;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        try {
          const resp = await fetch(MCP_URL, {
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
          });
          if (!resp.ok) return;
          const rpc = await resp.json();
          const parsed = JSON.parse(rpc?.result?.content?.[0]?.text || "{}");
          const items = Array.isArray(parsed?.items) ? parsed.items : [];
          if (!items.length) return;
          const seen = new Set();
          datalist.replaceChildren();
          for (const item of items) {
            const title = item.title || item.citation;
            if (!title || seen.has(title)) continue;
            seen.add(title);
            const opt = document.createElement("option");
            opt.value = `${title}, s. 1`;
            datalist.appendChild(opt);
          }
        } catch {
          // Keep existing records.json suggestions silently
        }
      }, 280);
    });

    citationInput.addEventListener("blur", () => validateCitation({ announce: true }));
    quoteInput.addEventListener("blur", () => validateQuote({ announce: true }));
    quoteInput.addEventListener("input", () => {
      const len = [...quoteInput.value].length;
      if (len >= COUNTER_SHOW_AT) {
        counter.hidden = false;
        counter.textContent = `${len} of ${MAX_QUOTE_LEN} characters`;
        counter.classList.toggle("over-limit", len > MAX_QUOTE_LEN);
      } else {
        counter.hidden = true;
      }
      if (len > MAX_QUOTE_LEN) {
        this._setFieldError(
          quoteInput,
          `Quote is ${len} characters. Shorten it to ${MAX_QUOTE_LEN} characters or fewer.`,
        );
        submitBtn.setAttribute("aria-disabled", "true");
      } else if (quoteInput.getAttribute("aria-invalid") === "true" && quoteInput.value.trim()) {
        this._setFieldError(quoteInput, null);
        if (!this.isChecking && !this.isPaused && !this.rateLimitTimer) {
          submitBtn.removeAttribute("aria-disabled");
        }
      }
    });

    // IME-safe Enter handling on single-line inputs
    citationInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.isComposing || e.keyCode === 229)) {
        e.preventDefault();
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (this.isPaused || this.isChecking || this.rateLimitTimer) return;
      if (submitBtn.getAttribute("aria-disabled") === "true") return;

      const citOk = validateCitation();
      const quoteOk = validateQuote();
      if (!citOk || !quoteOk) {
        const firstInvalid = !citOk ? citationInput : quoteInput;
        firstInvalid.focus();
        return;
      }

      const rawCitation = citationInput.value.trim();
      const matchedRec = this.records.find((r) => rawCitation.toLowerCase().includes(r.title.toLowerCase()));
      const claim = {
        citation: rawCitation,
        quote: quoteInput.value.trim(),
        jurisdiction: matchedRec?.jurisdiction || "ng",
        language: matchedRec?.language || "en",
      };
      await this._runDirectCheck(claim, { source: "form" });
    });

    const runExample = (altered) => {
      const rec = this.records.find((r) => r.example) || this.records[0];
      if (!rec || !rec.example) return;
      citationInput.value = `${rec.title}, ${rec.example.section}`;
      quoteInput.value = altered ? rec.example.altered_quote : rec.example.true_quote;
      this._setFieldError(citationInput, null);
      this._setFieldError(quoteInput, null);
      form.requestSubmit();
    };

    trueBtn?.addEventListener("click", () => runExample(false));
    alteredBtn?.addEventListener("click", () => runExample(true));

    this._bindProviderModal();
    this._bindSidePanelAndSlashMenu();
  }

  _setVerifiedPanelOpen(open) {
    const ws = document.getElementById("lc-workspace");
    const toggleBtn = document.getElementById("toggle-verified-panel");
    const countEl = document.getElementById("verified-count");
    const list = document.getElementById("receipts-list");
    const count = list ? list.querySelectorAll("oa-receipt, .lc-notice-card").length : 0;
    if (countEl) countEl.textContent = String(count);
    if (toggleBtn) {
      toggleBtn.hidden = count === 0;
      toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (ws) ws.setAttribute("data-panel-open", open ? "true" : "false");
  }

  _bindSidePanelAndSlashMenu() {
    const toggleBtn = document.getElementById("toggle-verified-panel");
    const closeBtn = document.getElementById("close-verified-panel");
    const ws = document.getElementById("lc-workspace");
    const slashMenu = document.getElementById("slash-menu");
    const citationInput = document.getElementById("citation-input");
    const quoteInput = document.getElementById("quote-input");

    toggleBtn?.addEventListener("click", () => {
      const isOpen = ws?.getAttribute("data-panel-open") === "true";
      this._setVerifiedPanelOpen(!isOpen);
    });
    closeBtn?.addEventListener("click", () => this._setVerifiedPanelOpen(false));

    const runSlashCommand = (cmd, sourceEl) => {
      if (sourceEl && sourceEl.value.trim().startsWith("/")) sourceEl.value = "";
      if (slashMenu) slashMenu.hidden = true;
      if (cmd === "quote") {
        this._activateTab?.("check");
        citationInput?.focus();
      } else if (cmd === "ask") {
        this._activateTab?.("ask");
      } else if (cmd === "verify") {
        this._activateTab?.("check");
        const verifyDetails = document.getElementById("verify-proof-disclosure");
        if (verifyDetails) {
          verifyDetails.open = true;
          verifyDetails.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }
    };

    let activeSlashInput = null;
    for (const field of [citationInput, quoteInput]) {
      if (!field || !slashMenu) continue;
      field.addEventListener("input", () => {
        const val = field.value.trimStart();
        if (val.startsWith("/") && !val.includes(" ")) {
          activeSlashInput = field;
          const query = val.slice(1).toLowerCase();
          let any = false;
          for (const btn of slashMenu.querySelectorAll(".lc-slash-item")) {
            const match = btn.dataset.cmd.startsWith(query);
            btn.hidden = !match;
            if (match) any = true;
          }
          slashMenu.hidden = !any;
        } else if (activeSlashInput === field) {
          slashMenu.hidden = true;
        }
      });
      field.addEventListener("keydown", (e) => {
        if (!slashMenu || slashMenu.hidden) return;
        if (e.key === "Escape") {
          slashMenu.hidden = true;
          e.preventDefault();
        } else if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) {
          const first = [...slashMenu.querySelectorAll(".lc-slash-item")].find((b) => !b.hidden);
          if (first) {
            e.preventDefault();
            runSlashCommand(first.dataset.cmd, field);
          }
        }
      });
    }

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
    const ctaBtn = document.getElementById("setup-provider-cta");
    const switchCheckBtn = document.getElementById("switch-to-check-cta");
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
    const presetBtns = [...document.querySelectorAll(".lc-preset-btn")];
    const pillLabel = document.getElementById("provider-pill-label");
    const pillDot = document.getElementById("provider-dot");
    const requiredCard = document.getElementById("provider-required-card");
    const agentApp = document.getElementById("agent-app");
    if (!dialog || !form) return;

    let activePreset = "openrouter";
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
        if (info.router) urlInput.value = info.router;
        if (info.model) modelInput.value = info.model;
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
        if (pillLabel) {
          pillLabel.textContent = hasKey
            ? `${profile.name || "AI"} · ${profile.model || "ready"}`
            : "Set up AI key";
        }
        if (pillDot) {
          pillDot.classList.toggle("is-connected", hasKey);
        }
        if (requiredCard) requiredCard.hidden = hasKey;
        if (agentApp) agentApp.hidden = !hasKey;
        if (removeBtn) removeBtn.hidden = !hasKey;
        return profile;
      } catch {
        return null;
      }
    };

    const openDialog = async () => {
      const profile = await refreshProviderUi();
      if (profile?.key) {
        const pKey = detectPreset(profile.router);
        selectPreset(pKey, { keepValues: true });
        keyInput.value = profile.key || "";
        modelInput.value = profile.model || PRESETS[pKey].model;
        urlInput.value = profile.router || PRESETS[pKey].router;
      } else {
        selectPreset("openai");
        keyInput.value = "";
      }
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
      dialog.addEventListener("pointerup", (e) => { if (pressedOutside && outside(e)) dialog.close(); });
    }

    dialog.addEventListener("close", () => {
      dialog.returnValue = "";
    });

    for (const btn of presetBtns) {
      btn.addEventListener("click", () => selectPreset(btn.dataset.preset));
    }

    toggleKeyBtn?.addEventListener("click", () => {
      const showing = keyInput.type === "text";
      keyInput.type = showing ? "password" : "text";
      toggleKeyBtn.textContent = showing ? "Show" : "Hide";
      toggleKeyBtn.setAttribute("aria-pressed", showing ? "false" : "true");
    });

    openBtn?.addEventListener("click", openDialog);
    ctaBtn?.addEventListener("click", openDialog);
    closeBtn?.addEventListener("click", () => dialog.close());
    cancelBtn?.addEventListener("click", () => dialog.close());
    switchCheckBtn?.addEventListener("click", () => this._activateTab?.("check"));

    removeBtn?.addEventListener("click", async () => {
      const byok = await loadByok();
      const profile = byok.getSelectedByokProfile();
      if (profile?.id) byok.deleteByokProfile(profile.id);
      byok.saveTabByokSetup(null);
      dialog.close();
      await refreshProviderUi();
      this._announce("AI provider key removed from this browser.");
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const key = keyInput.value.trim();
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
      if (!ok) return;

      const byok = await loadByok();
      const existing = byok.getSelectedByokProfile();
      const name = PRESETS[activePreset].name;
      byok.upsertByokProfile(
        { id: existing?.id, name, router, model, key },
        { select: true },
      );
      byok.saveTabByokSetup({ router, model, key });
      dialog.close();
      await refreshProviderUi();
      this._announce(`Connected to ${name}.`);
      // If Ask AI is already active, reload so the runtime picks up the new key cleanly
      if (this.activeTab === "ask") {
        location.reload();
      }
    });

    refreshProviderUi();
  }

  _bindRecheckForm() {
    const form = document.getElementById("recheck-form");
    const input = document.getElementById("recheck-input");
    const diffBox = document.getElementById("recheck-diff");
    const fileInput = document.getElementById("recheck-file");
    const dropzone = document.getElementById("verify-dropzone");
    if (!form || !input) return;

    const loadProofFile = async (file) => {
      if (!file) return;
      try {
        const text = await file.text();
        input.value = text.trim();
        this._setFieldError(input, null);
        form.requestSubmit();
      } catch {
        this._setFieldError(input, "Could not read that file. Choose a saved .json proof file.", { announce: true });
      }
    };

    fileInput?.addEventListener("change", () => {
      const file = fileInput.files?.[0];
      if (file) loadProofFile(file);
    });

    if (dropzone) {
      dropzone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropzone.classList.add("is-dragover");
      });
      dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragover"));
      dropzone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropzone.classList.remove("is-dragover");
        const file = e.dataTransfer?.files?.[0];
        if (file) loadProofFile(file);
      });
    }

    input.addEventListener("blur", () => {
      if (!input.value.trim()) {
        this._setFieldError(input, "Paste saved proof JSON or choose a .json file to verify.", { announce: true });
      } else {
        this._setFieldError(input, null);
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (this.isPaused || this.isChecking) return;
      diffBox.hidden = true;
      diffBox.replaceChildren();

      const raw = input.value.trim();
      if (!raw) {
        this._setFieldError(input, "Paste saved proof JSON or choose a .json file to verify.", { announce: true });
        input.focus();
        return;
      }

      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch {
        this._setFieldError(
          input,
          "This is not a complete receipt. Copy it again from where you saved it.",
          { announce: true },
        );
        input.focus();
        return;
      }

      const oldReceipt = parsed?.receipt && typeof parsed.receipt === "object" ? parsed.receipt : parsed;
      const oldClaim = parsed?.receipt && typeof parsed.receipt === "object" ? parsed.claim || null : null;

      if (!oldReceipt || typeof oldReceipt !== "object" || !oldReceipt.schema_version) {
        this._setFieldError(
          input,
          "This is not a complete receipt. Copy it again from where you saved it.",
          { announce: true },
        );
        input.focus();
        return;
      }

      const jwks = await this._ensureJwks();
      if (!jwks) {
        this._prependNotice("Could not load Open Alvary's key list to check the signature. Try again.", "amber", {
          retryFn: () => form.requestSubmit(),
        });
        return;
      }

      const check = await verifyReceipt(oldReceipt, jwks, { claim: oldClaim });
      if (!check.ok) {
        this._setFieldError(input, verificationErrorMessage(check.reason), { announce: true });
        input.focus();
        return;
      }
      this._setFieldError(input, null);

      if (!oldReceipt.part) {
        this._setFieldError(
          input,
          "This receipt names no section. Check the citation again on the first tab.",
          { announce: true },
        );
        return;
      }

      let repeatClaim;
      if (oldClaim) {
        repeatClaim = { ...oldClaim };
        delete repeatClaim.reading;
      } else {
        const matched = oldReceipt.findings?.matched_text;
        if (!matched || [...matched].length > MAX_QUOTE_LEN) {
          this._setFieldError(
            input,
            "Paste the full {receipt, claim} export from Download or Copy receipt, or check the quote again on the first tab.",
            { announce: true },
          );
          return;
        }
        repeatClaim = {
          anchor_id: oldReceipt.part.anchor_id,
          quote: matched,
        };
        if (oldReceipt.claim?.date_sent && oldReceipt.claim?.assessed_at) {
          repeatClaim.as_of = oldReceipt.claim.assessed_at;
        }
        if (oldReceipt.claim?.jurisdiction) {
          repeatClaim.jurisdiction = oldReceipt.claim.jurisdiction;
        }
        if (oldReceipt.part.language) {
          repeatClaim.language = oldReceipt.part.language;
        }
      }

      const fresh = await this._runDirectCheck(repeatClaim, { source: "recheck" });
      if (fresh) {
        this._renderRecheckDiff(oldReceipt, fresh.receipt);
      }
    });
  }

  _renderRecheckDiff(oldReceipt, newReceipt) {
    const diffBox = document.getElementById("recheck-diff");
    if (!diffBox) return;
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
        if (newState === "not_in_force") {
          messages.push("No longer recorded as in force.");
        } else {
          messages.push(`Record status changed from ${oldState} to ${newState}.`);
        }
      }
    }

    if (!messages.length) {
      messages.push(`Same words, same record as when checked on ${issuedDate}.`);
    }

    diffBox.replaceChildren();
    const ul = document.createElement("ul");
    for (const msg of messages) {
      const li = document.createElement("li");
      li.textContent = msg;
      ul.appendChild(li);
    }
    diffBox.appendChild(ul);
    diffBox.hidden = false;
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

  async _runDirectCheck(claim, { source = "form" } = {}) {
    const submitBtn = document.getElementById(source === "recheck" ? "recheck-submit" : "check-submit");
    const originalBtnLabel = submitBtn ? submitBtn.textContent : "Check quote";
    this.isChecking = true;
    if (submitBtn) {
      submitBtn.textContent = "Checking…";
      submitBtn.setAttribute("aria-disabled", "true");
    }

    const placeholder = this._prependCheckingPlaceholder();
    const slowTimer = setTimeout(() => {
      const p = placeholder.querySelector("p");
      if (p) p.textContent = "Still checking…";
    }, 5000);

    const controller = new AbortController();
    const timeoutTimer = setTimeout(() => controller.abort(), 20000);

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
          id: Date.now(),
          method: "tools/call",
          params: { name: "check_claim", arguments: claim },
        }),
        signal: controller.signal,
      });

      if (!resp.ok) {
        placeholder.remove();
        this._prependNotice("Could not check now. Try again.", "red", {
          retryFn: () => this._runDirectCheck(claim, { source }),
        });
        return null;
      }

      const rpc = parseJsonRpcText(await resp.text());
      const result = rpc?.result;
      if (!result || rpc?.error) {
        placeholder.remove();
        this._prependNotice("Could not check now. Try again.", "red", {
          retryFn: () => this._runDirectCheck(claim, { source }),
        });
        return null;
      }

      if (result.isError) {
        placeholder.remove();
        const errInfo = parseToolError(result);
        this._handleDirectToolError(errInfo, claim, source);
        return null;
      }

      const envelope = parseCheckClaimEnvelope(result);
      if (!envelope) {
        placeholder.remove();
        this._prependNotice("Could not check now. Try again.", "red", {
          retryFn: () => this._runDirectCheck(claim, { source }),
        });
        return null;
      }

      const jwks = await this._ensureJwks();
      if (!jwks) {
        placeholder.remove();
        this._prependNotice(
          "Could not load Open Alvary's key list to check the signature. Try again.",
          "amber",
          { retryFn: () => this._runDirectCheck(claim, { source }) },
        );
        return null;
      }

      const verification = await verifyReceipt(envelope.receipt, jwks, { claim: envelope.claim || claim });
      placeholder.remove();

      if (!verification.ok) {
        this._prependNotice(verificationErrorMessage(verification.reason), "red");
        return null;
      }

      const entry = {
        receipt: envelope.receipt,
        claim: envelope.claim || claim,
        verification,
        source,
      };
      this._addVerifiedEntry(entry, { persist: true, announce: true });
      return entry;
    } catch {
      placeholder.remove();
      this._prependNotice("Could not check now. Try again.", "red", {
        retryFn: () => this._runDirectCheck(claim, { source }),
      });
      return null;
    } finally {
      this.directCallCount = Math.max(0, this.directCallCount - 1);
      clearTimeout(slowTimer);
      clearTimeout(timeoutTimer);
      this.isChecking = false;
      if (submitBtn) {
        submitBtn.textContent = originalBtnLabel;
        if (!this.isPaused && !this.rateLimitTimer) {
          submitBtn.removeAttribute("aria-disabled");
        }
      }
    }
  }

  _handleDirectToolError({ code, seconds, text }, claim, source) {
    const quoteInput = document.getElementById("quote-input");
    if (code === "quote_too_short" && source === "form" && quoteInput) {
      this._setFieldError(
        quoteInput,
        "Paste at least 40 characters (about eight words), or the whole section.",
        { announce: true },
      );
      quoteInput.focus();
      return;
    }
    if (code === "quote_too_spread" && source === "form" && quoteInput) {
      this._setFieldError(
        quoteInput,
        text || "The quoted pieces are too far apart in the section.",
        { announce: true },
      );
      quoteInput.focus();
      return;
    }
    if (code === "result_too_large") {
      this._prependNotice("That section is too large to check at once. Name a smaller part.", "grey");
      return;
    }
    if (code === "unavailable") {
      // Plain non-receipt notice with link to /agents/records.json (§10.8)
      this._prependUnavailableNotice();
      return;
    }
    if (code === "paused") {
      this.isPaused = true;
      this._showGlobalBanner(
        "Law check is paused. Try again later. You can still verify a receipt you have.",
        "amber",
      );
      this._setInteractiveDisabled(true);
      return;
    }
    if (code === "rate_limited") {
      const wait = Math.max(1, Number(seconds) || 20);
      this._startRateLimitCountdown(
        wait,
        `Too many checks from your connection just now. Try again in ${wait} seconds.`,
      );
      return;
    }
    if (code === "service_rate_limited") {
      const wait = Math.max(1, Number(seconds) || 30);
      this._prependNotice(
        `Law check is busy right now. Try again in ${wait} seconds. You can still verify a receipt you have.`,
        "amber",
      );
      return;
    }
    this._prependNotice("Could not check now. Try again.", "red", {
      retryFn: () => this._runDirectCheck(claim, { source }),
    });
  }

  _startRateLimitCountdown(seconds, initialMessage) {
    const submitBtn = document.getElementById("check-submit");
    if (submitBtn) submitBtn.setAttribute("aria-disabled", "true");
    const notice = this._prependNotice(initialMessage, "amber");
    let remaining = seconds;
    if (this.rateLimitTimer) clearInterval(this.rateLimitTimer);
    this.rateLimitTimer = setInterval(() => {
      remaining -= 1;
      const p = notice.querySelector("p");
      if (remaining <= 0) {
        clearInterval(this.rateLimitTimer);
        this.rateLimitTimer = null;
        if (submitBtn && !this.isPaused && !this.isChecking) {
          submitBtn.removeAttribute("aria-disabled");
        }
        if (p) p.textContent = "You can check another quote now.";
      } else if (p) {
        p.textContent = `Too many checks from your connection just now. Try again in ${remaining} seconds.`;
      }
    }, 1000);
  }

  async _handleInterceptedAgentReceipt(envelope, args) {
    const jwks = await this._ensureJwks();
    if (!jwks) {
      this._prependNotice("Could not load Open Alvary's key list to check the signature. Try again.", "amber");
      return;
    }
    const claim = envelope.claim || args || null;
    const verification = await verifyReceipt(envelope.receipt, jwks, { claim });
    if (!verification.ok) {
      this._prependNotice(verificationErrorMessage(verification.reason), "red");
      return;
    }
    const entry = {
      receipt: envelope.receipt,
      claim,
      verification,
      source: "agent",
    };
    this._addVerifiedEntry(entry, { persist: true, announce: false });
    if (this.activeAgentTurn) {
      this.activeAgentTurn.checked += 1;
      if (envelope.receipt.status === "matches" || envelope.receipt.status === "words_found" || envelope.receipt.status === "words_found_with_omissions") {
        this.activeAgentTurn.found += 1;
      } else {
        this.activeAgentTurn.notFound += 1;
      }
      this._updateAnswerSummary();
    }
  }

  _handleInterceptedAgentError(errInfo) {
    if (errInfo.code === "paused") {
      this.isPaused = true;
      this._showGlobalBanner(
        "Law check is paused. Try again later. You can still verify a receipt you have.",
        "amber",
      );
      this._setInteractiveDisabled(true);
      return;
    }
    if (errInfo.code === "unavailable") {
      this._prependUnavailableNotice();
    } else {
      this._prependNotice("Could not check now.", "red");
    }
    if (this.activeAgentTurn) {
      this.activeAgentTurn.failed += 1;
      this._updateAnswerSummary();
    }
  }

  _addVerifiedEntry(entry, { persist = true, announce = false } = {}) {
    const list = document.getElementById("receipts-list");
    const empty = document.getElementById("empty-receipts");
    if (empty) empty.hidden = true;

    // Collapse earlier form cards when a new form card arrives
    if (entry.source === "form") {
      for (const existing of list.querySelectorAll("oa-receipt")) {
        existing._collapsible = true;
        existing.collapsed = true;
      }
    }

    const card = document.createElement("oa-receipt");
    card.setData({
      receipt: entry.receipt,
      claim: entry.claim,
      verification: entry.verification,
      source: entry.source,
      collapsible: entry.source !== "agent",
      collapsed: false,
    });
    list.prepend(card);
    this._setVerifiedPanelOpen(true);

    if (entry.receipt.receipt_id) {
      this.cardByReceiptId.set(entry.receipt.receipt_id, card);
    }
    const sid = entry.receipt.part?.source_id;
    if (sid) {
      const arr = this.cardsBySourceId.get(sid) || [];
      arr.unshift(card);
      this.cardsBySourceId.set(sid, arr);
    }

    this.entries.unshift(entry);
    if (persist) this._saveSessionReceipts();

    if (window.innerWidth <= 1100 && typeof card.scrollIntoView === "function") {
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    if (announce) {
      const title = entry.receipt.part
        ? `${entry.receipt.part.title}, ${entry.receipt.part.pinpoint}`
        : entry.claim?.citation || entry.receipt.status;
      const wl = wordsLineForReceipt(entry.receipt).text;
      const rl = recordLinesForReceipt(entry.receipt)[0]?.text || "";
      this._announce(`${title}. ${wl}. ${rl}`);
    }
  }

  _saveSessionReceipts() {
    try {
      const slim = this.entries.slice(0, 30).map((e) => ({
        receipt: e.receipt,
        claim: e.claim,
        source: e.source,
      }));
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(slim));
    } catch {
      // sessionStorage unavailable or full
    }
  }

  async _restoreSessionReceipts() {
    let saved;
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      saved = JSON.parse(raw);
      if (!Array.isArray(saved) || !saved.length) return;
    } catch {
      return;
    }

    const jwks = await this._ensureJwks();
    if (!jwks) return;

    // Re-verify every stored receipt before rendering (§10.3)
    for (let i = saved.length - 1; i >= 0; i--) {
      const item = saved[i];
      if (!item?.receipt) continue;
      const verification = await verifyReceipt(item.receipt, jwks, { claim: item.claim });
      if (verification.ok) {
        this._addVerifiedEntry(
          { receipt: item.receipt, claim: item.claim || null, verification, source: item.source || "form" },
          { persist: false, announce: false },
        );
      }
    }
  }

  _prependCheckingPlaceholder() {
    const list = document.getElementById("receipts-list");
    const empty = document.getElementById("empty-receipts");
    if (empty) empty.hidden = true;

    const box = document.createElement("div");
    box.className = "lc-notice-card";
    box.setAttribute("role", "status");
    const p = document.createElement("p");
    p.textContent = "Checking a section…";
    const bar1 = document.createElement("div");
    bar1.className = "lc-skeleton-bar";
    const bar2 = document.createElement("div");
    bar2.className = "lc-skeleton-bar short";
    box.append(p, bar1, bar2);
    list.prepend(box);
    return box;
  }

  _prependNotice(message, tone = "grey", { retryFn = null } = {}) {
    const list = document.getElementById("receipts-list");
    const empty = document.getElementById("empty-receipts");
    if (empty) empty.hidden = true;

    const box = document.createElement("div");
    box.className = "lc-notice-card";
    box.dataset.tone = tone;
    const p = document.createElement("p");
    p.textContent = message;
    box.appendChild(p);
    if (typeof retryFn === "function") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "lc-btn-secondary";
      btn.textContent = "Try again";
      btn.addEventListener("click", () => {
        box.remove();
        retryFn();
      });
      box.appendChild(btn);
    }
    list.prepend(box);
    this._announce(message);
    return box;
  }

  _prependUnavailableNotice() {
    const list = document.getElementById("receipts-list");
    const empty = document.getElementById("empty-receipts");
    if (empty) empty.hidden = true;

    const box = document.createElement("div");
    box.className = "lc-notice-card";
    box.dataset.tone = "grey";
    const p = document.createElement("p");
    p.textContent = "Law check does not cover this record. ";
    const a = document.createElement("a");
    a.href = "/agents/records.json";
    a.textContent = "See covered records";
    p.appendChild(a);
    box.appendChild(p);
    list.prepend(box);
    this._announce("Law check does not cover this record.");
    return box;
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

  _setInteractiveDisabled(disabled) {
    const checkBtn = document.getElementById("check-submit");
    if (checkBtn) {
      if (disabled) checkBtn.setAttribute("aria-disabled", "true");
      else checkBtn.removeAttribute("aria-disabled");
    }
  }

  _announce(text) {
    const live = document.getElementById("live-status");
    if (!live) return;
    live.textContent = "";
    requestAnimationFrame(() => {
      live.textContent = text;
    });
  }

  _updateAnswerSummary() {
    const el = document.getElementById("answer-summary");
    if (!el || !this.activeAgentTurn) return;
    const { checked, failed, done } = this.activeAgentTurn;
    el.hidden = false;
    if (done && checked === 0 && failed === 0) {
      el.dataset.tone = "red";
      el.textContent = "Nothing in this answer was checked.";
      return;
    }
    delete el.dataset.tone;
    const parts = [`This answer: ${checked} ${checked === 1 ? "quote" : "quotes"} checked`];
    if (failed > 0) parts.push(`${failed} could not be checked`);
    el.textContent = `${parts.join(", ")}. Sentences without a receipt were not checked.`;
  }

  async _ensureRuntimeLoaded() {
    if (this.runtimeLoaded || this.runtimeLoading) return;
    this.runtimeLoading = true;
    const statusBox = document.getElementById("ask-status");
    try {
      const runtime = await import("./runtime/index.js");
      const { trace } = await runtime.boot("#agent-app", {
        originUrl: `${location.origin}/agents`,
        sandboxWorkerUrl: "/agents/runtime/sandbox-worker.js",
      });
      this.runtimeLoaded = true;
      this._wireRuntimeUx(trace);
    } catch {
      if (statusBox) {
        statusBox.hidden = false;
        statusBox.dataset.tone = "amber";
        statusBox.replaceChildren();
        const p = document.createElement("p");
        p.textContent = "The assistant could not be loaded. Checking a quote still works.";
        statusBox.appendChild(p);
      }
    } finally {
      this.runtimeLoading = false;
    }
  }

  _wireRuntimeUx(trace) {
    const app = document.getElementById("agent-app");
    if (app) {
      // Replace default runtime greeting (§10.5)
      const firstBot = app.querySelector(".ns-msg.bot");
      if (firstBot && /Ask me anything/i.test(firstBot.textContent || "")) {
        firstBot.textContent =
          "Ask what a published law says. I quote the text and check each quote. I do not give legal advice.";
      }
      // Ensure chat input has an accessible name (§10.5)
      const chatInput = app.querySelector(".ns-input, textarea, input[type='text']");
      if (chatInput && !chatInput.getAttribute("aria-label")) {
        chatInput.setAttribute("aria-label", "Ask a question about a published law");
      }
    }

    if (trace && typeof trace.subscribe === "function") {
      trace.subscribe((ev) => {
        if (ev?.type === "run_start" || ev?.kind === "run_start") {
          this.activeAgentTurn = { checked: 0, found: 0, notFound: 0, failed: 0, done: false };
          this._updateAnswerSummary();
        } else if (ev?.type === "run_end" || ev?.kind === "run_end") {
          if (this.activeAgentTurn) {
            this.activeAgentTurn.done = true;
            this._updateAnswerSummary();
            const { checked, found, notFound } = this.activeAgentTurn;
            this._announce(
              checked > 0
                ? `Answer ready. ${checked} receipts: ${found} found, ${notFound} not found.`
                : "Answer ready. Nothing in this answer was checked.",
            );
          }
        }
      });
    }
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
