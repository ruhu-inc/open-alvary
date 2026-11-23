/**
 * Code-built legal-basis gate for the Example Vendor demo peer site (§12).
 *
 * A clause that cites the law is accepted ONLY when deterministic code verifies:
 * 1. The quoted words appear in the clause's own text (compared in normal form).
 * 2. A live check_claim call with that exact citation and quote returns a receipt
 *    whose signature and claim.digest verify via Open Alvary's receipt.js.
 * 3. The receipt's status is "matches" or "words_found", fetched in this session
 *    with no as_of date, and the record is recorded as binding in "ng".
 */

import { verifyReceipt, loadReceiptKeys } from "./receipt.js";

export const OPEN_ALVARY_MCP_URL = "https://open.alvary.ai/agents/mcp";
export const ACCEPTED_LABEL = "Published wording shown";
export const REJECTED_LABEL = "Cannot be accepted: legal basis not shown";

const LIGATURE_MAP = new Map([
  ["\uFB00", "ff"],
  ["\uFB01", "fi"],
  ["\uFB02", "fl"],
  ["\uFB03", "ffi"],
  ["\uFB04", "ffl"],
]);

/**
 * Normal form matching Open Alvary's verifier (§8 Step 3):
 * NFC, strip soft hyphens and zero-width characters, fold common ligatures,
 * straight quotes, dashes to ASCII hyphen, collapse whitespace (incl. NBSP).
 * Case is preserved.
 *
 * @param {string} text
 * @returns {string}
 */
export function normalizeText(text) {
  if (typeof text !== "string") return "";
  let s = text.normalize("NFC");
  // Strip soft hyphen (U+00AD) and zero-width chars (U+200B..U+200D, U+FEFF).
  s = s.replace(/[\u00AD\u200B-\u200D\uFEFF]/g, "");
  // Fold common Latin ligatures.
  s = s.replace(/[\uFB00-\uFB04]/g, (ch) => LIGATURE_MAP.get(ch) ?? ch);
  // Curly quotes to straight quotes.
  s = s.replace(/[\u2018\u2019\u201A\u201B]/g, "'");
  s = s.replace(/[\u201C\u201D\u201E\u201F]/g, '"');
  // Dashes to ASCII hyphen-minus.
  s = s.replace(/[\u2010-\u2015\u2212]/g, "-");
  // Collapse whitespace including NBSP.
  s = s.replace(/[\s\u00A0]+/g, " ").trim();
  return s;
}

/**
 * Parse law_check's fixed JSON array reply into validated candidate objects.
 * Any reply that does not parse as the fixed shape is treated as no candidates.
 *
 * @param {string} rawText
 * @returns {{ ok: boolean, candidates: Array<{claim: number, citation?: string, quote?: string, none?: boolean}>, reason?: string }}
 */
export function parseLawCheckReply(rawText) {
  if (typeof rawText !== "string" || !rawText.trim()) {
    return { ok: false, candidates: [], reason: "Empty reply from law_check." };
  }
  let parsed;
  try {
    parsed = JSON.parse(rawText.trim());
  } catch {
    return { ok: false, candidates: [], reason: "law_check reply was not valid JSON." };
  }
  if (!Array.isArray(parsed)) {
    return { ok: false, candidates: [], reason: "law_check reply was not a JSON array." };
  }
  const candidates = [];
  for (const item of parsed) {
    if (!item || typeof item !== "object" || typeof item.claim !== "number") {
      return { ok: false, candidates: [], reason: "law_check reply item had an invalid shape." };
    }
    if (item.none === true) {
      candidates.push({ claim: item.claim, none: true });
    } else if (
      typeof item.citation === "string" &&
      item.citation.trim() &&
      typeof item.quote === "string" &&
      item.quote.trim()
    ) {
      candidates.push({
        claim: item.claim,
        citation: item.citation.trim(),
        quote: item.quote.trim(),
      });
    } else {
      return { ok: false, candidates: [], reason: "law_check candidate lacked citation or quote." };
    }
  }
  return { ok: true, candidates };
}

/**
 * Call Open Alvary's public /agents/mcp endpoint directly for check_claim (no key, no Authorization header).
 *
 * @param {{ citation: string, quote: string, jurisdiction?: string, language?: string }} args
 * @param {{ mcpUrl?: string, fetchImpl?: typeof fetch }} [options]
 * @returns {Promise<{ ok: boolean, receipt?: Record<string, any>, claim?: Record<string, any>, errorCode?: string, errorMessage?: string }>}
 */
export async function callOpenAlvaryCheckClaim(args, options = {}) {
  const mcpUrl = options.mcpUrl || OPEN_ALVARY_MCP_URL;
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  if (typeof fetchImpl !== "function") {
    return { ok: false, errorCode: "fetch_unavailable", errorMessage: "fetch is unavailable." };
  }
  const payload = {
    jsonrpc: "2.0",
    id: `demo-check-${Date.now()}`,
    method: "tools/call",
    params: {
      name: "check_claim",
      arguments: args,
    },
  };
  let resp;
  try {
    resp = await fetchImpl(mcpUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    return {
      ok: false,
      errorCode: "network_error",
      errorMessage: `Could not reach Open Alvary: ${err.message}`,
    };
  }
  if (!resp.ok) {
    return {
      ok: false,
      errorCode: `http_${resp.status}`,
      errorMessage: `Open Alvary returned HTTP ${resp.status}.`,
    };
  }
  const body = await resp.json();
  if (body?.error) {
    return {
      ok: false,
      errorCode: "jsonrpc_error",
      errorMessage: body.error.message || "JSON-RPC error from check_claim.",
    };
  }
  const result = body?.result;
  if (!result || result.isError) {
    const code = result?.structuredContent?.code || "tool_error";
    const text = result?.content?.[0]?.text || "check_claim returned an error.";
    return { ok: false, errorCode: code, errorMessage: text };
  }
  const structured = result.structuredContent;
  if (structured?.receipt) {
    return { ok: true, receipt: structured.receipt, claim: structured.claim || args };
  }
  const textContent = result.content?.[0]?.text;
  if (typeof textContent === "string") {
    try {
      const parsed = JSON.parse(textContent);
      if (parsed?.receipt) {
        return { ok: true, receipt: parsed.receipt, claim: parsed.claim || args };
      }
    } catch {
      // fall through
    }
  }
  return { ok: false, errorCode: "malformed_response", errorMessage: "No receipt returned by check_claim." };
}

/**
 * Evaluate a drafted clause through the code-built legal-basis gate (§12).
 *
 * @param {{
 *   clause: { text?: string, citation?: string, quote?: string },
 *   callCheckClaim?: (args: Record<string, any>) => Promise<{ ok: boolean, receipt?: any, claim?: any, errorCode?: string, errorMessage?: string }>,
 *   jwks?: { keys: Array<Record<string, any>> },
 *   keysUrl?: string,
 *   fetchImpl?: typeof fetch,
 *   now?: Date | string | number,
 *   consentApproved?: boolean,
 * }} params
 * @returns {Promise<{
 *   accepted: boolean,
 *   label: string,
 *   reason: string | null,
 *   receipt: Record<string, any> | null,
 *   claim: Record<string, any> | null,
 *   verified: Record<string, any> | null,
 * }>}
 */
export async function evaluateLegalBasisGate(params) {
  const { clause, consentApproved = true } = params || {};

  const reject = (reason, extra = {}) => ({
    accepted: false,
    label: REJECTED_LABEL,
    reason,
    receipt: extra.receipt ?? null,
    claim: extra.claim ?? null,
    verified: extra.verified ?? null,
  });

  if (!consentApproved) {
    return reject("Consent to contact open.alvary.ai was declined.");
  }

  if (
    !clause ||
    typeof clause !== "object" ||
    typeof clause.text !== "string" ||
    !clause.text.trim() ||
    typeof clause.citation !== "string" ||
    !clause.citation.trim() ||
    typeof clause.quote !== "string" ||
    !clause.quote.trim()
  ) {
    return reject("Clause must include its text, the section it cites, and the quoted words.");
  }

  // Rule 1: The quoted words must appear in the clause's own text (in normal form).
  const normClause = normalizeText(clause.text);
  const normQuote = normalizeText(clause.quote);
  if (!normQuote || !normClause.includes(normQuote)) {
    return reject("The quoted words do not appear in the clause's own text.");
  }

  // Rule 2: Send that exact citation and quote to check_claim (no as_of date, jurisdiction "ng").
  const sentClaim = {
    citation: clause.citation.trim(),
    quote: clause.quote.trim(),
    jurisdiction: "ng",
    language: "en",
  };

  const checkFn =
    typeof params.callCheckClaim === "function"
      ? params.callCheckClaim
      : (args) => callOpenAlvaryCheckClaim(args, { fetchImpl: params.fetchImpl });

  const checkResult = await checkFn(sentClaim);
  if (!checkResult || !checkResult.ok || !checkResult.receipt) {
    return reject(checkResult?.errorMessage || "Could not verify the citation with Open Alvary.");
  }

  const receipt = checkResult.receipt;
  let jwks = params.jwks;
  if (!jwks) {
    try {
      jwks = await loadReceiptKeys(params.keysUrl, params.fetchImpl);
    } catch (err) {
      return reject(`Could not load Open Alvary's key list: ${err.message}`, {
        receipt,
        claim: sentClaim,
      });
    }
  }

  // Verify signature, key validity/revocation, and claim.digest binding to sentClaim.
  const verified = await verifyReceipt(receipt, jwks, {
    claim: sentClaim,
    now: params.now,
  });

  if (!verified.ok) {
    return reject(verified.summary || `Receipt verification failed (${verified.reason}).`, {
      receipt,
      claim: sentClaim,
      verified,
    });
  }

  // Rule 3: Status must be "matches" or "words_found", no as_of date, and binding in "ng".
  if (receipt.status !== "matches" && receipt.status !== "words_found") {
    return reject(
      `Receipt status is "${receipt.status}"; published wording for the cited section was not confirmed.`,
      { receipt, claim: sentClaim, verified },
    );
  }

  const applicability = receipt.findings?.applicability;
  if (!applicability || applicability.status !== "bound" || applicability.jurisdiction !== "ng") {
    return reject("Open Alvary's record does not show this law as binding in Nigeria (ng).", {
      receipt,
      claim: sentClaim,
      verified,
    });
  }

  return {
    accepted: true,
    label: ACCEPTED_LABEL,
    reason: null,
    receipt,
    claim: sentClaim,
    verified,
  };
}
