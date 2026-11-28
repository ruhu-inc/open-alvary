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
// The most steps listed under one answer; a longer turn is cut at this many.
const MAX_STEPS = 40;
const OLD_STORAGE_KEY = "oa_law_check_receipts_v1";
const MAX_SAVED_ITEMS = 60;
const MAX_QUESTION_LEN = 4000;
const MAX_PROOF_BYTES = 512 * 1024;
const CITATION_NEEDED = "Enter the law and section, for example Freedom of Information Act 2011, s. 1(1).";
const MCP_PATH = "/agents/mcp";
const LOCAL_KEYS_PATH = "/.well-known/open-alvary-receipt-keys.json";
const MAX_QUOTE_LEN = 2000;
// The longest an AI answer may take before the page stops it. The runtime gives its agents 90
// seconds; a provider that keeps a request open past that is stopped here.
const AI_TURN_LIMIT_MS = 120_000;

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
/**
 * Lets the page stop an AI answer. While a turn runs, every call to an AI provider (a POST to
 * ".../chat/completions") and to Law check's own tools carries the turn's signal, so Stop, or the
 * time limit, ends the calls in flight and refuses the next ones. The vendored runtime is not
 * changed. Returns ``{ begin(signal), end() }``.
 */
export function installTurnStopper({ targetGlobal = globalThis, mcpPath = MCP_PATH } = {}) {
  const originalFetch = targetGlobal.fetch;
  let active = null;
  const handle = {
    begin(signal) {
      active = signal || null;
    },
    end() {
      active = null;
    },
  };
  if (typeof originalFetch !== "function") return handle;
  const isTurnRequest = (input, init) => {
    const method = (init?.method || (typeof input === "object" && input?.method) || "GET").toUpperCase();
    if (method !== "POST") return false;
    const url = typeof input === "string" ? input : input?.url || String(input || "");
    return /\/chat\/completions(?:[?#]|$)/.test(url) || matchesMcpUrl(input, mcpPath);
  };
  targetGlobal.fetch = function lawCheckTurnStopper(input, init) {
    const signal = active;
    if (!signal || !isTurnRequest(input, init)) return originalFetch.call(this, input, init);
    if (signal.aborted) return Promise.reject(new DOMException("The answer was stopped.", "AbortError"));
    let combined = signal;
    if (init?.signal) {
      if (typeof AbortSignal.any === "function") combined = AbortSignal.any([init.signal, signal]);
      else {
        const both = new AbortController();
        for (const s of [init.signal, signal]) s.addEventListener("abort", () => both.abort(s.reason), { once: true });
        combined = both.signal;
      }
    }
    return originalFetch.call(this, input, { ...(init || {}), signal: combined });
  };
  return handle;
}

/**
 * What the page says when the AI provider could not answer, in plain words. ``error`` is what the
 * runtime threw, often "LLM HTTP 429: {...}" with the provider's own JSON. Returns
 * ``{ text, detail }``: ``detail`` is the provider's own message, without its JSON, or "".
 */
export function providerErrorNotice(error, { model = "" } = {}) {
  const raw = String(error?.message || error || "").trim();
  const status = Number(/\bLLM HTTP (\d{3})\b/.exec(raw)?.[1] || /"code"\s*:\s*(\d{3})/.exec(raw)?.[1] || 0);
  // The provider's own words, even when the runtime cut its reply short mid-message.
  const said = /"message"\s*:\s*"((?:[^"\\]|\\.)*)"?/.exec(raw)?.[1]?.replace(/\\n/g, " ").replace(/\\(.)/g, "$1");
  const plain = raw.replace(/^LLM HTTP \d{3}:\s*/, "");
  const detail = shortenWords(said || (/^[\[{]/.test(plain) ? "" : plain), 200);
  const named = model ? ` \u201c${model.replace(/^models\//, "")}\u201d` : "";
  let text;
  if (error?.code === "LLM_NETWORK_ERROR" || /failed to fetch|networkerror|load failed/i.test(raw)) {
    text = "Could not reach your AI provider. Check your connection, then try again.";
  } else if (status === 401 || status === 403 || /api key not valid|invalid api key|invalid x-api-key|incorrect api key/i.test(raw)) {
    text = "Your AI provider did not accept the key. Check that it is right and still active in the AI settings.";
  } else if (status === 404 || /model.*(?:not found|does not exist)|no such model/i.test(raw)) {
    text = `Your AI provider does not offer the model${named}. Choose another model in the AI settings.`;
  } else if (status === 429 || /quota|rate limit|resource_exhausted|too many requests/i.test(raw)) {
    const wait = Math.ceil(Number(/retry in (\d+(?:\.\d+)?)\s*s/i.exec(raw)?.[1] || 0));
    text = /per ?day|daily|PerDay/i.test(raw)
      ? "Your AI key has used up today's allowance with its provider. Try again tomorrow, or use a key with a higher limit."
      : `Your AI key has reached its provider's limit for now. ${
          wait > 0 && wait < 600 ? `Wait about ${wait} seconds` : "Wait a minute"
        }, then try again. One question can take 10 to 15 requests, so a free key runs out quickly.`;
  } else if ([500, 502, 503, 504, 529].includes(status) || /overloaded|unavailable|high demand/i.test(raw)) {
    text = "Your AI provider is busy and could not answer just now. Try again in a minute, or choose another model in the AI settings.";
  } else if (status === 400) {
    text = `Your AI provider refused the request. The model${named} may not support tools: choose another in the AI settings.`;
  } else {
    text = "Your AI provider could not answer. Try again, or check the key and model in the AI settings.";
  }
  return { text, detail };
}

class TurnStopped extends Error {}
class TurnTimedOut extends Error {}

function isFoundStatus(status) {
  return status === "matches" || status === "words_found" || status === "words_found_with_omissions";
}

function shortenWords(text, max) {
  const words = String(text || "").replace(/\s+/g, " ").trim();
  return words.length > max ? `${words.slice(0, max - 1).trimEnd()}\u2026` : words;
}

/**
 * The passages a text puts in quotation marks, 40 characters or longer. Marks are paired in order,
 * so the words between two short quotes ("any citizen", but … says "any person") are never taken
 * for a passage.
 */
export function passagesInQuotes(text) {
  const passages = [];
  for (const m of String(text || "").matchAll(/["\u201c]([^"\u201c\u201d]*)["\u201d]/g)) {
    const passage = m[1].trim();
    if (passage.length >= 40 && passage.length <= 2000) passages.push(passage);
  }
  return passages;
}

/** The passages a visitor's message puts in quotation marks: at most three are checked. */
export function quotedInMessage(text) {
  return passagesInQuotes(text).slice(0, 3);
}

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
  if (!citation || citation.length > 600) return null;
  if (!/(?:^|[^\p{L}])(?:ss?|secs?|sect|sections?|arts?|articles?|regs?|regulations?|rules?|paras?|paragraphs?)\.?\s*\d/iu.test(citation)) {
    return null;
  }
  return { citation, quote };
}

// Quotation marks that may open and close a pasted quote, each with the marks that may close it.
const QUOTE_PAIRS = {
  '"': ['"', "”", "“"],
  "“": ["”", '"'],
  "„": ["”", "“", '"'],
  "‘": ["’", "'"],
  "'": ["'", "’"],
  "«": ["»"],
};

/**
 * The quoted words without quotation marks wrapped around the whole of them, as a quote is often
 * pasted from a memo ("…" or "…".). The marks are not part of the law's words, so the check would
 * otherwise never find them. Marks inside the words are kept.
 */
export function stripOuterQuotes(text) {
  const raw = String(text ?? "").trim();
  const chars = [...raw];
  if (chars.length < 3) return raw;
  const closers = QUOTE_PAIRS[chars[0]];
  if (!closers) return raw;
  let end = chars.length - 1;
  // A full stop or comma written after the closing mark goes with it.
  if (/[.,;]/.test(chars[end]) && closers.includes(chars[end - 1])) end -= 1;
  if (!closers.includes(chars[end]) || end < 2) return raw;
  const inner = chars.slice(1, end);
  // '"person" means any "citizen"' starts and ends with a mark but is not wrapped in one: when the
  // words carry marks of the same kind, nothing is taken off.
  if (inner.some((c) => c === chars[0] || closers.includes(c))) return raw;
  return inner.join("").trim() || raw;
}

/** How many characters a field sends: the words without the spaces around them. */
export function sentLength(value) {
  return [...String(value ?? "").trim()].length;
}

function tooLongMessage(length, max) {
  return `That is ${length.toLocaleString("en")} characters. Shorten it to ${max.toLocaleString("en")} or fewer.`;
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

/** "A", "A and B", "A, B and C". */
export function listInWords(names) {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

const SCOPE_CODE = /^[a-z]{2,3}(?:[-_][a-z0-9]{1,8})*$/i;

/** The jurisdiction and language a check may send: each only when it is a well-formed code. */
export function pickClaimScope(jurisdiction, language) {
  const scope = {};
  if (typeof jurisdiction === "string" && SCOPE_CODE.test(jurisdiction)) scope.jurisdiction = jurisdiction.toLowerCase();
  if (typeof language === "string" && SCOPE_CODE.test(language)) scope.language = language.toLowerCase();
  return scope;
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

// The agents and tools of Open Alvary's own two networks, with the words the page uses for
// them. Only these names are ever shown: any other name comes from outside and is not.
const KNOWN_AGENTS = {
  law_desk: "Law desk",
  finder: "Finder",
  verifier: "Verifier",
  law_check: "Law check",
  locator: "Locator",
};
const KNOWN_TOOLS = {
  search_catalogue: "searched the published laws (search_catalogue)",
  resolve_citation: "looked up the section cited (resolve_citation)",
  get_anchor: "read the section's exact words (get_anchor)",
  retrieve_evidence: "found matching passages (retrieve_evidence)",
  check_claim: "sent a quote to Open Alvary's check, where code compares the words and signs a receipt (check_claim)",
  website_open_section: "added a link to the section on this page",
  website_show_receipt: "pointed to a result card on this page",
};

function knownAgent(name) {
  return KNOWN_AGENTS[String(name || "").toLowerCase()] || "";
}

function knownTool(name) {
  return KNOWN_TOOLS[String(name || "").toLowerCase().replace(/^mcp_/, "")] || "";
}

/**
 * One line for the list of steps under an AI answer, or "" for an event that is not a step.
 * Names come only from Open Alvary's own networks (KNOWN_AGENTS, KNOWN_TOOLS); anything else
 * is described without its name, so no model or peer text reaches the page.
 */
export function stepLineForEvent(event) {
  const who = knownAgent(event?.source?.agent) || "An agent";
  const kind = event?.tool?.kind;
  switch (event?.type) {
    case "tool.started": {
      if (kind === "agent") return `${who} asked ${knownAgent(event.tool?.name) || "another agent"} for help.`;
      if (kind === "remote network" || kind === "linked network") return `${who} contacted another agent site, after asking you.`;
      const tool = knownTool(event.tool?.name);
      return tool ? `${who} ${tool}.` : `${who} used a tool.`;
    }
    case "tool.failed": {
      const name = knownAgent(event.tool?.name) || (knownTool(event.tool?.name) ? String(event.tool.name).replace(/^mcp_/, "") : "");
      return `${name ? `The step ${name}` : "A step"} did not finish.`;
    }
    case "agent.failed":
      return `${who} could not finish.`;
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

// Words for comparing an answer's passage with a checked quote: letters and digits only, in order.
function comparableWords(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

// Passages an answer presents as the law's words: text in quotation marks of 40 characters or
// more, and every quote block.
function quotedPassages(block) {
  if (block.tagName === "BLOCKQUOTE") return [block.textContent];
  return passagesInQuotes(block.textContent);
}

const QUOTE_MARKS = {
  found: "Open Alvary found these words in the published text",
  missing: "Open Alvary did not find these words in the published text",
  unchecked: "Open Alvary did not check these words",
};

/**
 * Mark each passage of an answer that quotes a law: found or not found by a check of this turn,
 * or not checked at all. ``quotes`` are the turn's checks, ``{quote, found}``. The mark is the
 * page's, from the signed results, never the model's text.
 */
export function markAnswerQuotes(container, quotes = []) {
  const checks = (Array.isArray(quotes) ? quotes : [])
    .filter((q) => typeof q?.quote === "string" && comparableWords(q.quote).length >= 20)
    .map((q) => ({ words: comparableWords(q.quote), found: Boolean(q.found) }));
  for (const block of container.querySelectorAll(":scope > p, :scope > blockquote, :scope > ul > li, :scope > ol > li")) {
    const passages = quotedPassages(block);
    if (!passages.length) continue;
    let mark = "found";
    for (const passage of passages) {
      const words = comparableWords(passage);
      const check = checks.find((c) => words.includes(c.words) || c.words.includes(words));
      const state = !check ? "unchecked" : check.found ? "found" : "missing";
      // One passage not found, then one not checked, outweighs the others.
      if (state === "missing" || (state === "unchecked" && mark === "found")) mark = state;
    }
    block.classList.add("lc-quote-mark", `lc-quote-${mark}`);
    const label = document.createElement("span");
    label.className = "lc-quote-label";
    label.textContent = QUOTE_MARKS[mark];
    block.appendChild(label);
  }
}

/**
 * Draw the AI's text as paragraphs, lists and quote blocks. Text only: no HTML, links or images
 * from the model are ever made into elements.
 */
export function renderAnswerText(container, text, { quotes = null } = {}) {
  container.replaceChildren();
  const blocks = String(text || "").replace(/\r\n?/g, "\n").split(/\n{2,}/);
  const paragraph = (lines) => {
    const p = document.createElement("p");
    lines.forEach((line, i) => {
      if (i > 0) p.appendChild(document.createElement("br"));
      appendInline(p, line.replace(/^#{1,6}\s+/, ""));
    });
    container.appendChild(p);
  };
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
    // Lines that start with ">" are a quote block; the lines around them stay paragraphs.
    let run = [];
    let quoting = false;
    const flush = () => {
      if (!run.length) return;
      if (quoting) {
        const quote = document.createElement("blockquote");
        run.forEach((line, i) => {
          if (i > 0) quote.appendChild(document.createElement("br"));
          appendInline(quote, line);
        });
        container.appendChild(quote);
      } else {
        paragraph(run);
      }
      run = [];
    };
    for (const line of lines) {
      const isQuote = /^\s*>/.test(line);
      if (isQuote !== quoting) {
        flush();
        quoting = isQuote;
      }
      run.push(isQuote ? line.replace(/^\s*>\s?/, "") : line);
    }
    flush();
  }
  if (quotes) markAnswerQuotes(container, quotes);
}

const $ = (id) => document.getElementById(id);

function shorten(text, max) {
  const chars = [...String(text || "")];
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : chars.join("");
}

class LawCheckController {
  constructor() {
    this.records = [];
    // Each published law the catalogue search has suggested, by title, with its country, so a check
    // names the law's own country, never one assumed.
    this.lawInfo = new Map();
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
    this.mode = "ask";
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

    // 1. Install the LC-16 fetch wrapper at once, before any runtime loads, and the turn stopper
    // (Stop and the time limit) around it.
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

    this.turnStopper = installTurnStopper();

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

    // The examples wait for this: a click before the list arrives runs once it has.
    this.recordsReady = this._loadRecords();
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
    const previous = this.mode;
    this.mode = mode === "ask" ? "ask" : "quote";
    if (chosen) this.modeChosen = true;
    const quote = this.mode === "quote";
    const input = $("composer-input");
    const citation = $("citation-input");
    // Each mode keeps its own draft: a question is never sent as quoted words, nor quoted words as a
    // question, and switching back brings the draft back.
    if (previous && previous !== this.mode && input) {
      this._drafts = this._drafts || {};
      this._drafts[previous] = input.value;
      input.value = this._drafts[this.mode] || "";
      this._drafts[this.mode] = "";
    }
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
    // While an AI answer is coming, the button stops it.
    const stoppable = Boolean(this.aiTurnStop);
    send.textContent = stoppable
      ? "Stop"
      : this.isBusy
        ? quote ? "Checking…" : "Working…"
        : quote ? (narrow ? "Check" : "Check quote") : "Send";
    send.classList.toggle("is-stop", stoppable);
    if (stoppable) send.setAttribute("aria-label", "Stop the answer");
    else send.removeAttribute("aria-label");
    const blocked = !stoppable && (this.isBusy || this.isPaused || Boolean(this.rateLimitTimer) || this.overLimit);
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
    const len = sentLength(input.value);
    const max = this.mode === "quote" ? MAX_QUOTE_LEN : MAX_QUESTION_LEN;
    const showAt = max - 200;
    if (len >= showAt) {
      counter.hidden = false;
      counter.textContent = `${len.toLocaleString("en")} of ${max.toLocaleString("en")} characters`;
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
      // Returning from Otari or OpenRouter. The page finishes the sign-in here, so it can say
      // how it went; the runtime then finds nothing left to finish and starts on the new key.
      this._setMode("ask", { updateHash: false });
      this._finishSignIn()
        .then(() => this._ensureRuntimeLoaded())
        .then(() => this._refreshProviderUi?.());
    } else if (hash === "ask") {
      this._setMode("ask", { updateHash: false });
    } else if (hash === "check" || hash === "recheck" || hash === "verify") {
      this._setMode("quote", { updateHash: false });
    } else {
      // No address given: Ask AI comes first (owner, 2026-11-25); Check a quote is one click away.
      this._setMode("ask", { updateHash: false, chosen: false });
    }
  }

  async _finishSignIn() {
    let outcome = null;
    try {
      const [{ OpenRouterConnect }, { OtariConnect }] = await Promise.all([
        import("./runtime/openrouter-connect.js"),
        import("./runtime/otari-connect.js"),
      ]);
      outcome = (await OpenRouterConnect.finish()) || (await OtariConnect.finish());
    } catch {
      outcome = { status: "failed", message: "The sign-in could not be finished. Paste a key in AI key settings instead." };
    }
    if (!outcome) return;
    // The runtime's messages point at its own settings panel, which this page does not show.
    const message = String(outcome.message || "").replace(/⚙ AI model settings/g, "AI key settings");
    this._appendNotice(message, outcome.status === "connected" ? "grey" : "amber", {
      actions: outcome.status === "connected" ? [] : [{ label: "AI key settings", onClick: () => $("open-provider-modal")?.click() }],
    });
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
      const nextClaim = pickClaimScope(claim?.jurisdiction, claim?.language);
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
    this._countPublished();

    if (datalist) {
      datalist.replaceChildren();
      for (const rec of this.records) {
        const opt = document.createElement("option");
        opt.value = rec.example?.section ? `${rec.title}, ${rec.example.section}` : `${rec.title}, `;
        datalist.appendChild(opt);
      }
    }
  }

  async _countPublished() {
    // Law check covers every text Open Alvary publishes (owner, 2026-11-27); the line says how many,
    // from the site's own live count, and names no number when that count cannot be read.
    const summary = document.getElementById("coverage-summary");
    if (!summary) return;
    try {
      const resp = await fetch("/site/v1/stats", { headers: { Accept: "application/json" } });
      if (!resp.ok) return;
      const catalogue = (await resp.json())?.catalogue || {};
      const count = Number(catalogue.verified_texts);
      const names = (Array.isArray(catalogue.jurisdictions) ? catalogue.jurisdictions : [])
        .map((j) => (typeof j?.name === "string" ? j.name.trim() : ""))
        .filter((name) => name && name.length <= 60);
      if (!Number.isInteger(count) || count < 1) return;
      const where = names.length ? ` from ${listInWords(names)}` : "";
      summary.textContent = `Laws and judgments${where}: ${count.toLocaleString("en")} published texts.`;
    } catch {
      // The line keeps its general wording.
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

    // Live suggestions from search_catalogue, across every published text.
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
            // Only a text Open Alvary publishes can be checked, so a record without one is not suggested.
            if (!title || item?.has_full_text !== true) continue;
            if (!this.lawInfo.has(title.toLowerCase())) {
              this.lawInfo.set(title.toLowerCase(), { jurisdiction: item.jurisdiction_id });
            }
            if (seen.has(title.toLowerCase())) continue;
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
        this._setFieldError(input, tooLongMessage(sentLength(input.value), max));
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
    // The Send button is Stop while an answer is coming. Enter in the box does not stop it.
    $("composer-send")?.addEventListener("click", (e) => {
      if (!this.aiTurnStop) return;
      e.preventDefault();
      this._stopAiTurn();
    });

    const runExample = async (altered) => {
      // A judge may click an example before Law check's list of laws has arrived.
      if (!this.records.some((r) => r.example)) await this.recordsReady;
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
    // The example questions. With no key yet, sending one shows how to add a key.
    for (const id of ["try-question", "try-question-2"]) {
      $(id)?.addEventListener("click", (event) => {
        this._setMode("ask");
        input.value = event.currentTarget.dataset.question
          || "What does the Freedom of Information Act 2011 say about who may ask for information? Quote the words.";
        input.focus();
        form.requestSubmit();
      });
    }
  }

  _claimDefaults(citation) {
    // The law's own country and language when the page knows them (a worked example, or a law the
    // catalogue search suggested); otherwise neither is sent, and the check finds the law by its name.
    const cited = String(citation || "").toLowerCase();
    const rec = this.records.find((r) => r.title && cited.startsWith(String(r.title).toLowerCase()));
    if (rec) return pickClaimScope(rec.jurisdiction, rec.language);
    let best = null;
    for (const [title, info] of this.lawInfo) {
      if (cited.startsWith(title) && (!best || title.length > best.title.length)) best = { title, ...info };
    }
    // The country only: a law may be published in several languages, and the quote may be from any of them.
    return best ? pickClaimScope(best.jurisdiction, null) : {};
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
      // Quotation marks around the whole quote are not the law's words: they are left out.
      const quote = stripOuterQuotes(text);
      let quoteMessage = null;
      if (!quote) quoteMessage = "Paste the quoted words to check.";
      else if (sentLength(quote) > MAX_QUOTE_LEN) quoteMessage = tooLongMessage(sentLength(quote), MAX_QUOTE_LEN);
      this._setFieldError(input, quoteMessage);
      if (!citationOk || quoteMessage) {
        (citationOk ? input : citationInput).focus();
        this._announce(citationOk ? quoteMessage : CITATION_NEEDED);
        return;
      }
      if (serviceBlocked) return;
      const claim = { citation, quote, ...this._claimDefaults(citation) };
      this._beginTurn();
      this._appendUser({ cite: citation, quote });
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
    // Suggested models, read from OpenRouter's public model list on 2026-11-27: each provider's
    // current tool-calling models, the low-cost one first. "Show all models" asks the chosen
    // provider for its own full list, so a newer model can always be picked or typed.
    const PRESETS = {
      otari: {
        name: "Otari",
        router: "https://api.otari.ai/api/v1",
        model: "alibaba/qwen3.8-flash",
        keyUrl: "https://otari.ai/dashboard#/keys",
        keyLabel: "Otari",
        connect: "otari",
        models: [{ id: "alibaba/qwen3.8-flash", label: "Qwen3.8 Flash · suggested" }],
      },
      openrouter: {
        name: "OpenRouter",
        router: "https://openrouter.ai/api/v1",
        model: "qwen/qwen3.8-flash",
        keyUrl: "https://openrouter.ai/settings/keys",
        keyLabel: "OpenRouter",
        connect: "openrouter",
        models: [
          { id: "qwen/qwen3.8-flash", label: "Qwen3.8 Flash" },
          { id: "deepseek/deepseek-v4.1-flash", label: "DeepSeek V4.1 Flash" },
          { id: "google/gemini-3.8-flash", label: "Gemini 3.8 Flash" },
          { id: "openai/gpt-6.2-sol", label: "GPT-6.2 Sol" },
          { id: "anthropic/claude-sonnet-5.5", label: "Claude Sonnet 5.5" },
          { id: "moonshotai/kimi-k3.1-pro", label: "Kimi K3.1 Pro" },
          { id: "google/gemma-4-31b-it:free", label: "Gemma 4 31B · free" },
        ],
      },
      gemini: {
        name: "Google Gemini",
        router: "https://generativelanguage.googleapis.com/v1beta/openai",
        model: "models/gemini-3.8-flash",
        keyUrl: "https://aistudio.google.com/apikey",
        keyLabel: "Google AI Studio",
        models: [
          { id: "models/gemini-3.8-flash", label: "Gemini 3.8 Flash" },
          { id: "models/gemini-3.5-flash-lite", label: "Gemini 3.5 Flash-Lite" },
          { id: "models/gemini-4-hydrogen", label: "Gemini 4 Hydrogen" },
          { id: "models/gemini-4-neon", label: "Gemini 4 Neon" },
        ],
      },
      openai: {
        name: "OpenAI",
        router: "https://api.openai.com/v1",
        model: "gpt-6.2-sol",
        keyUrl: "https://platform.openai.com/api-keys",
        keyLabel: "OpenAI",
        models: [
          { id: "gpt-6-luna", label: "GPT-6 Luna · low cost" },
          { id: "gpt-6.1-terra", label: "GPT-6.1 Terra" },
          { id: "gpt-6.2-sol", label: "GPT-6.2 Sol" },
          { id: "gpt-6.2-astra", label: "GPT-6.2 Astra" },
        ],
      },
      anthropic: {
        name: "Claude (Anthropic)",
        router: "https://api.anthropic.com/v1",
        model: "claude-sonnet-5-5",
        keyUrl: "https://console.anthropic.com/settings/keys",
        keyLabel: "Anthropic Console",
        models: [
          { id: "claude-haiku-5-5", label: "Claude Haiku 5.5 · low cost" },
          { id: "claude-sonnet-5-5", label: "Claude Sonnet 5.5" },
          { id: "claude-opus-5-5", label: "Claude Opus 5.5" },
          { id: "claude-fable-5-2", label: "Claude Fable 5.2" },
        ],
      },
      custom: {
        name: "Custom provider",
        router: "",
        model: "",
        keyUrl: "",
        keyLabel: "",
        models: [],
      },
    };
    const DEFAULT_PRESET = "otari";

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

    const modelChoices = document.getElementById("provider-model-choices");
    const modelOptions = document.getElementById("provider-model-options");
    const modelsBtn = document.getElementById("provider-models-btn");
    const modelsStatus = document.getElementById("provider-models-status");
    const connectBox = document.getElementById("provider-connect");
    const connectBtn = document.getElementById("provider-connect-btn");
    const connectStatus = document.getElementById("provider-connect-status");

    let activePreset = DEFAULT_PRESET;
    // The saved key for the provider now chosen in the dialog, never another provider's: a key goes
    // only to the provider it was saved for. Each provider keeps its own saved profile.
    let savedKey = "";
    let savedProfiles = [];
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
      return router ? "custom" : DEFAULT_PRESET;
    };

    // Every model a provider button can fill in. Switching provider replaces one of these, but
    // never a model the visitor typed.
    const suggestedIds = new Set(
      Object.values(PRESETS).flatMap((preset) => [preset.model, ...preset.models.map((m) => m.id)]).filter(Boolean),
    );

    // A model the page filled in, from a provider button or a saved profile, may be replaced when
    // the provider changes; one the visitor typed may not.
    const swappableModel = (value) => {
      const current = String(value || "").trim();
      return !current || suggestedIds.has(current) || savedProfiles.some((p) => p.model === current);
    };

    const syncModelChoices = () => {
      const current = modelInput.value.trim();
      for (const btn of modelChoices?.querySelectorAll("button") || []) {
        btn.setAttribute("aria-pressed", btn.dataset.model === current ? "true" : "false");
      }
    };

    const renderModelChoices = (info) => {
      if (!modelChoices) return;
      modelChoices.replaceChildren();
      for (const choice of info.models) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "lc-model-chip";
        btn.dataset.model = choice.id;
        btn.textContent = choice.label;
        btn.title = choice.id;
        btn.addEventListener("click", () => {
          modelInput.value = choice.id;
          modelInput.dispatchEvent(new Event("input", { bubbles: true }));
          this._setFieldError(modelInput, null);
        });
        modelChoices.appendChild(btn);
      }
      modelChoices.hidden = info.models.length === 0;
      syncModelChoices();
    };

    const connectAvailable = () => Boolean(globalThis.isSecureContext && globalThis.crypto?.subtle && globalThis.top === globalThis.self);

    const sameRouter = (a, b) => String(a || "").trim().replace(/\/+$/, "").toLowerCase() === String(b || "").trim().replace(/\/+$/, "").toLowerCase();
    // The saved profile for the provider chosen now: a named provider by its address, a custom one
    // only for the exact address typed.
    const profileForChoice = () => {
      if (activePreset === "custom") {
        const url = urlInput.value.trim();
        return url ? savedProfiles.find((p) => p.key && sameRouter(p.router, url)) || null : null;
      }
      return savedProfiles.find((p) => p.key && detectPreset(p.router) === activePreset) || null;
    };
    const showSavedKey = () => {
      const profile = profileForChoice();
      savedKey = profile?.key || "";
      keyInput.required = !savedKey;
      keyInput.placeholder = savedKey ? "Paste a new key to replace it" : "Paste your API key";
      if (savedNote) {
        savedNote.textContent = savedKey
          ? `A key ending ${savedKey.slice(-4)} is saved for ${PRESETS[activePreset].name}. Leave the field empty to keep it.`
          : "";
        savedNote.hidden = !savedKey;
        keyInput.setAttribute("aria-describedby", savedKey ? "provider-key-hint provider-key-saved" : "provider-key-hint");
      }
      if (removeBtn) removeBtn.hidden = !savedKey;
      return profile;
    };

    const selectPreset = (presetKey, { keepValues = false } = {}) => {
      activePreset = PRESETS[presetKey] ? presetKey : DEFAULT_PRESET;
      const info = PRESETS[activePreset];
      for (const btn of presetBtns) {
        btn.setAttribute("aria-pressed", btn.dataset.preset === activePreset ? "true" : "false");
      }
      urlField.hidden = activePreset !== "custom";
      urlInput.required = activePreset === "custom";
      if (!keepValues) {
        // Custom starts with an empty address, never the previous provider's.
        urlInput.value = info.router;
        // A model the visitor typed is kept; only a suggested or saved model is swapped.
        if (swappableModel(modelInput.value)) modelInput.value = info.model;
      }
      renderModelChoices(info);
      // The full list belongs to one provider; a new provider starts without it.
      modelOptions?.replaceChildren();
      if (modelsStatus) modelsStatus.textContent = "";
      if (modelsBtn) modelsBtn.hidden = activePreset === "custom" && !urlInput.value.trim();
      if (connectBox) {
        connectBox.hidden = !info.connect || !connectAvailable();
        if (connectBtn) connectBtn.textContent = `Connect with ${info.connect === "openrouter" ? "OpenRouter" : "Otari"}`;
        if (connectStatus) {
          connectStatus.hidden = true;
          connectStatus.textContent = "";
        }
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
      const saved = showSavedKey();
      // A provider with a saved key comes back with the model saved beside it.
      if (!keepValues && saved?.model) {
        if (swappableModel(modelInput.value)) modelInput.value = saved.model;
        syncModelChoices();
      }
    };

    modelInput.addEventListener("input", syncModelChoices);

    // "Connect with Otari" and "Connect with OpenRouter": the runtime's own sign-ins. The
    // provider sends the visitor back here, and the page finishes the sign-in as it loads.
    connectBtn?.addEventListener("click", async () => {
      const info = PRESETS[activePreset];
      const report = (text, tone) => {
        if (!connectStatus) return;
        connectStatus.textContent = text;
        connectStatus.dataset.tone = tone || "";
        connectStatus.hidden = false;
      };
      try {
        if (info.connect === "openrouter") {
          const { OpenRouterConnect } = await import("./runtime/openrouter-connect.js");
          await OpenRouterConnect.connect(OpenRouterConnect.keyLabel("Open Alvary Law check"), report);
        } else if (info.connect === "otari") {
          const { OtariConnect } = await import("./runtime/otari-connect.js");
          await OtariConnect.connect(report);
        }
      } catch {
        report("The sign-in could not start. Paste a key below instead.", "warn");
      }
    });

    // Ask the chosen provider for every model it offers. The key goes only to that provider,
    // as it does for each question.
    const listModels = async () => {
      const info = PRESETS[activePreset];
      const router = (activePreset === "custom" ? urlInput.value.trim() : info.router).replace(/\/$/, "");
      const key = keyInput.value.trim() || savedKey;
      if (!router || !/^https:\/\//i.test(router)) {
        modelsStatus.textContent = "Enter the provider's https:// address first.";
        return;
      }
      // OpenRouter lists its models to anyone; the others need the key.
      if (!key && activePreset !== "openrouter") {
        modelsStatus.textContent = "Paste your key first: the provider lists its models only to its own customers.";
        keyInput.focus();
        return;
      }
      const headers = {};
      if (activePreset === "anthropic") {
        headers["x-api-key"] = key;
        headers["anthropic-version"] = "2023-06-01";
        headers["anthropic-dangerous-direct-browser-access"] = "true";
      } else if (key) {
        headers.Authorization = `Bearer ${key}`;
      }
      modelsBtn.disabled = true;
      modelsStatus.textContent = "Asking the provider for its models…";
      try {
        const response = await fetch(`${router}/models`, { headers, cache: "no-store", credentials: "omit" });
        if (!response.ok) throw new Error(String(response.status));
        const body = await response.json();
        const list = Array.isArray(body?.data) ? body.data : Array.isArray(body?.models) ? body.models : [];
        const ids = [...new Set(list.map((m) => String(m?.id || m?.name || "").trim()).filter(Boolean))].sort();
        modelOptions.replaceChildren();
        for (const id of ids) {
          const option = document.createElement("option");
          option.value = id;
          modelOptions.appendChild(option);
        }
        modelsStatus.textContent = ids.length
          ? `${ids.length} models listed. Type in the Model field to search them.`
          : "The provider listed no models. Type the model name instead.";
        if (ids.length) modelInput.focus();
      } catch {
        modelsStatus.textContent = "The provider did not list its models to this page. Type the model name instead.";
      } finally {
        modelsBtn.disabled = false;
      }
    };
    modelsBtn?.addEventListener("click", listModels);
    urlInput.addEventListener("input", () => {
      if (modelsBtn && activePreset === "custom") modelsBtn.hidden = !urlInput.value.trim();
      // A custom key belongs to its address: a changed address shows the key saved for it, if any.
      if (activePreset === "custom") showSavedKey();
    });

    const refreshProviderUi = async () => {
      try {
        const byok = await loadByok();
        const profile = byok.getSelectedByokProfile();
        const hasKey = Boolean(profile?.key && profile?.router);
        this.hasProviderKey = hasKey;
        if (pillLabel) {
          pillLabel.textContent = hasKey
            ? `${profile.name || "AI"} · ${String(profile.model || "ready").replace(/^models\//, "")}`
            : "Add AI key (optional)";
        }
        if (pillDot) {
          pillDot.classList.toggle("is-connected", hasKey);
        }
        this.providerName = hasKey ? profile.name || "your AI provider" : "";
        this.providerModel = hasKey ? profile.model || "" : "";
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
      try {
        savedProfiles = (await loadByok()).loadByokStore().profiles || [];
      } catch {
        savedProfiles = [];
      }
      keyInput.value = "";
      if (profile?.key) {
        const pKey = detectPreset(profile.router);
        urlInput.value = profile.router || PRESETS[pKey].router;
        // The saved key is never put back in the field: only its last four characters are shown.
        selectPreset(pKey, { keepValues: true });
        modelInput.value = profile.model || PRESETS[pKey].model;
        syncModelChoices();
      } else {
        modelInput.value = "";
        selectPreset(DEFAULT_PRESET);
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
      // The key removed is the one for the provider shown in the dialog. If it was the one in use,
      // no other provider's key is picked in its place: the visitor chooses again.
      const profile = profileForChoice();
      const wasSelected = profile?.id && profile.id === byok.getSelectedByokProfile()?.id;
      if (profile?.id) byok.deleteByokProfile(profile.id);
      if (wasSelected) byok.selectByokProfile("");
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
      // Saved over this provider's own profile, so another provider's key is never replaced.
      const existing = profileForChoice();
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

  // With nothing left in the thread, the page shows its welcome and examples again.
  _showWelcomeIfEmpty() {
    if ($("thread-list")?.children.length) return;
    const welcome = $("welcome");
    if (welcome) welcome.hidden = false;
    $("lc-chat")?.classList.remove("has-thread");
    document.body.classList.remove("lc-threaded");
    const newChat = $("new-chat");
    if (newChat) newChat.hidden = true;
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

  _appendAi(text, summary, { save = true, reveal = true, steps = [], quotes = null } = {}) {
    const li = document.createElement("li");
    li.className = "lc-msg lc-msg-ai";
    const label = document.createElement("p");
    label.className = "lc-ai-label";
    label.textContent = "AI's answer. Only quotes with a result card were checked.";
    const body = document.createElement("div");
    body.className = "lc-ai-text";
    // Each quoted passage is marked from this turn's signed checks: found, not found or not checked.
    const checks = Array.isArray(quotes) ? quotes.slice(0, 50) : null;
    renderAnswerText(body, text, { quotes: checks });
    li.append(label, body);
    if (summary?.text) {
      const foot = document.createElement("p");
      foot.className = "lc-ai-summary";
      if (summary.tone) foot.dataset.tone = summary.tone;
      foot.textContent = summary.text;
      li.appendChild(foot);
    }
    const shown = Array.isArray(steps) ? steps.filter((line) => typeof line === "string").slice(0, MAX_STEPS) : [];
    if (shown.length) {
      // The agents' work, step by step, so a person can see who did what before the answer.
      const details = document.createElement("details");
      details.className = "lc-steps";
      const summaryEl = document.createElement("summary");
      summaryEl.textContent = `How the agents worked: ${shown.length} ${shown.length === 1 ? "step" : "steps"}`;
      const list = document.createElement("ol");
      for (const line of shown) {
        const item = document.createElement("li");
        item.textContent = line;
        list.appendChild(item);
      }
      details.append(summaryEl, list);
      li.appendChild(details);
    }
    this._appendItem(li, { reveal });
    if (save) {
      this._save({
        t: "ai",
        text: shorten(text, 12000),
        summary: summary || null,
        steps: shown,
        quotes: checks ? checks.map((q) => ({ quote: shorten(q.quote, 2000), found: Boolean(q.found) })) : null,
      });
    }
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
          {
            label: "Check a quote instead",
            onClick: (item) => {
              item.remove();
              this._showWelcomeIfEmpty();
              this._setMode("quote", { focus: true });
            },
          },
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
        : entry.source === "yours"
          ? "Your quote, checked by Open Alvary on the section the AI found"
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
      const wl = wordsLineForReceipt(entry.receipt, entry.claim).text;
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
        // Answers saved before quotes were marked carry no checks, and are drawn without marks.
        const quotes = Array.isArray(item.quotes) ? item.quotes : null;
        this._appendAi(item.text, item.summary, { save: false, reveal: false, steps: item.steps, quotes });
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
    // A chosen file is named as such: there is nothing to copy again.
    const incomplete =
      label === "pasted text"
        ? "This is not a complete receipt. Copy it again from where you saved it."
        : "This file is not a Law check proof. Choose the .json file that Save proof made.";

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
    // Law check covers every published text, so this means Open Alvary has no published text of it.
    this._appendNotice(
      "Open Alvary does not publish this text, or this part of it. Check the law's name and the section, or look the law up among the published texts.",
      "grey",
      { actions: [{ label: "Browse published texts", onClick: () => window.open("/browse", "_blank", "noopener") }] },
    );
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
        // The runtime's own view of the agents and its trace, empty until it has started.
        const app = $("agent-app");
        if (app) app.hidden = false;
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
    // Stop (the Send button while an answer is coming) and the time limit both end the turn here.
    const stop = new AbortController();
    this.aiTurnStop = stop;
    // The browser tests set a shorter limit on the page; nothing else does.
    const limitMs = Number(globalThis.lawCheckTurnLimitMs) > 0 ? Number(globalThis.lawCheckTurnLimitMs) : AI_TURN_LIMIT_MS;
    const limit = setTimeout(() => stop.abort("timeout"), limitMs);
    this.turnStopper?.begin(stop.signal);
    this._setBusy(true);
    const status = this._appendStatus("Thinking…");
    const turn = { checked: 0, found: 0, notFound: 0, failed: 0, quotes: [], parts: [] };
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
      const steps = [];
      const stream = this.runtime.engine.streamTurn(text, {});
      const events = stream[Symbol.asyncIterator]();
      const stopped = new Promise((resolve) => {
        stop.signal.addEventListener("abort", () => resolve({ stopped: true }), { once: true });
      });
      for (;;) {
        // The page stops waiting at once; the calls in flight are ended by the turn stopper.
        const next = await Promise.race([events.next(), stopped]);
        if (next.stopped) {
          events.return?.()?.catch?.(() => {});
          throw stop.signal.reason === "timeout" ? new TurnTimedOut() : new TurnStopped();
        }
        if (next.done) break;
        const event = next.value;
        if (event.type === "turn.completed") {
          result = event.result;
          continue;
        }
        if (event.visibility === "developer" || event.type === "agent.output.delta") continue;
        const step = stepLineForEvent(event);
        if (step && steps.length < MAX_STEPS) steps.push(step);
        const line = statusLineForEvent(event);
        const who = KNOWN_AGENTS[String(event.source?.agent || "").toLowerCase()];
        if (line) status.update(who ? `${who}: ${line}` : line);
      }
      turn.steps = steps;
      if (!result) throw new Error("The turn ended without an answer.");
      // Every receipt the agents fetched is verified and drawn before the answer is shown under them.
      await Promise.allSettled(this.pendingAgentWork);
      this.turnStopper?.end();
      status.remove();
      // A quote the visitor wrote in the question is checked too, on the section the agents found,
      // so a wrong quote of theirs gets its own card and not only the AI's correction.
      await this._checkVisitorQuotes(text, turn);
      const answer = cleanAnswerText(result.text) || "The AI gave no written answer.";
      this._appendAi(answer, this._answerSummary(turn, answer), { steps: turn.steps, quotes: turn.quotes });
      this._announce(
        turn.checked > 0
          ? `Answer ready. ${turn.checked} ${turn.checked === 1 ? "quote" : "quotes"} checked: ${turn.found} found, ${turn.notFound} not found.`
          : "Answer ready. Nothing in this answer was checked.",
      );
    } catch (err) {
      this.turnStopper?.end();
      if (!(err instanceof TurnStopped || err instanceof TurnTimedOut)) await Promise.allSettled(this.pendingAgentWork);
      status.remove();
      // The AI starts the next question without this unfinished one.
      if (err instanceof TurnStopped) {
        this._appendNotice("Stopped. Your question is back in the box.", "grey");
        this._putQuestionBack(text);
        return;
      }
      const settings = { label: "AI key settings", onClick: () => $("open-provider-modal")?.click() };
      if (err instanceof TurnTimedOut) {
        this._appendNotice(
          `Your AI provider took longer than ${
            limitMs >= 60000 ? `${Math.round(limitMs / 60000)} minutes` : `${Math.round(limitMs / 1000)} seconds`
          }, so the question was stopped. Try again, or choose a faster model in the AI settings.`,
          "amber",
          { actions: [settings] },
        );
        this._putQuestionBack(text);
        return;
      }
      const notice = providerErrorNotice(err, { model: this.providerModel || "" });
      this._appendNotice(notice.detail ? `${notice.text} The provider said: \u201c${notice.detail}\u201d` : notice.text, "red", {
        actions: [settings],
      });
      this._putQuestionBack(text);
    } finally {
      clearTimeout(limit);
      this.turnStopper?.end();
      this.aiTurnStop = null;
      this.activeAgentTurn = null;
      this.pendingAgentWork = [];
      this._setBusy(false);
    }
  }

  _stopAiTurn() {
    if (this.aiTurnStop && !this.aiTurnStop.signal.aborted) this.aiTurnStop.abort("stopped");
  }

  // A question that got no answer goes back in the box, unless something new was typed there.
  _putQuestionBack(text) {
    const input = $("composer-input");
    if (!input || input.value.trim() || this.mode !== "ask") return;
    input.value = text;
    this._updateCounter();
  }

  async _checkVisitorQuotes(text, turn) {
    const passages = quotedInMessage(text).filter((passage) => {
      const words = comparableWords(passage);
      return !turn.quotes.some((q) => comparableWords(q.quote) === words);
    });
    if (!passages.length) return;
    const part = turn.parts[0];
    if (!part) {
      this._appendNotice(
        "The quote in your question was not checked: the AI named no section for it. Use Check a quote to check it against a section you choose.",
        "grey",
      );
      return;
    }
    for (const quote of passages) {
      const claim = { anchor_id: part.anchor_id, quote, ...pickClaimScope(part.jurisdiction, null) };
      const entry = await this._runDirectCheck(claim, { source: "yours" });
      if (entry) turn.quotes.push({ quote, found: isFoundStatus(entry.receipt.status) });
    }
  }

  _answerSummary(turn, answer = "") {
    if (turn.checked === 0 && turn.failed === 0) {
      // A reply that quotes no law (a greeting, a question back) has nothing to check: said plainly,
      // not as a warning. One that quotes a law unchecked is the warning.
      const quotes = passagesInQuotes(answer).length > 0 || /^\s*>/m.test(answer);
      return quotes
        ? { text: "Nothing in this answer was checked.", tone: "red" }
        : { text: "This answer quotes no law, so there was nothing to check.", tone: "" };
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
      const found = isFoundStatus(status);
      const part = envelope.receipt.part;
      if (part?.anchor_id) {
        this.activeAgentTurn.parts.push({ anchor_id: part.anchor_id, jurisdiction: envelope.receipt.claim?.jurisdiction });
      }
      if (found) this.activeAgentTurn.found += 1;
      else this.activeAgentTurn.notFound += 1;
      if (typeof claim?.quote === "string") this.activeAgentTurn.quotes.push({ quote: claim.quote, found });
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
