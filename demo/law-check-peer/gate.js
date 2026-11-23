/**
 * Code-built legal-basis gate for the Example Vendor demo peer site (§12).
 *
 * A clause that cites the law is accepted ONLY when deterministic code verifies:
 * 1. The quoted words appear in the clause's own text, as whole words (compared in
 *    normal form), and the clause's text names the section it cites and no other.
 * 2. A live check_claim call with that exact citation and quote returns a receipt
 *    whose signature and claim.digest verify via Open Alvary's receipt.js, and whose
 *    `part` is the section the clause cites.
 * 3. The receipt's status is "matches" or "words_found", it was issued just now with
 *    no date sent, for "ng", and the record is recorded as binding there.
 *
 * What it does not stop: true words used to mean something else in the clause's own
 * sentence. The accepted label says only "Published wording shown".
 *
 * The receipt fields read here are the ones the server signs (schema oa-receipt-1,
 * retrieval/verify.py): status, claim.date_sent, claim.jurisdiction, part.pinpoint,
 * findings.quote, findings.applicability.binds. The tests run the gate against the
 * golden receipts in clients/receipt/vectors.json so the two cannot drift apart.
 */

import { verifyReceipt, loadReceiptKeys } from "./receipt.js";

export const OPEN_ALVARY_MCP_URL = "https://open.alvary.ai/agents/mcp";
export const ACCEPTED_LABEL = "Published wording shown";
export const REJECTED_LABEL = "Cannot be accepted: legal basis not shown";
export const GATE_JURISDICTION = "ng";
/** A receipt must have been issued within this long of the gate's clock, either way. */
export const MAX_RECEIPT_AGE_MS = 60 * 60 * 1000;

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

const WORD_CHAR = /[\p{L}\p{N}_]/u;

function isWordChar(ch) {
  return typeof ch === "string" && ch.length > 0 && WORD_CHAR.test(ch);
}

/**
 * Find `needle` in `haystack` as whole words: the match may not start or end inside a
 * longer word, nor inside a hyphenated one ("non-public" does not hold "public").
 * Both strings are expected in normal form.
 *
 * @param {string} haystack
 * @param {string} needle
 * @returns {number} index of the first whole-word match, or -1
 */
export function findWholeWords(haystack, needle) {
  if (typeof haystack !== "string" || typeof needle !== "string" || !needle) return -1;
  const hay = Array.from(haystack);
  const ned = Array.from(needle);
  const startsWord = isWordChar(ned[0]);
  const endsWord = isWordChar(ned[ned.length - 1]);
  outer: for (let i = 0; i + ned.length <= hay.length; i++) {
    for (let j = 0; j < ned.length; j++) {
      if (hay[i + j] !== ned[j]) continue outer;
    }
    const end = i + ned.length;
    const before = hay[i - 1];
    const after = hay[end];
    const leftOk =
      !startsWord ||
      i === 0 ||
      (!isWordChar(before) && !(before === "-" && isWordChar(hay[i - 2])));
    const rightOk =
      !endsWord ||
      end === hay.length ||
      (!isWordChar(after) && !(after === "-" && isWordChar(hay[end + 1])));
    if (leftOk && rightOk) return i;
  }
  return -1;
}

const SECTION_REF =
  /(?:§+|(?<![\p{L}\p{N}_])(?:ss?|secs?|sect|sections?|arts?|articles?)\.?)\s*(\d+[A-Za-z]?(?:\s*\(\s*[0-9A-Za-z]+\s*\))*)/giu;

/**
 * List the section numbers a text names ("s. 1(1)", "section 30", "Art. 5"), each as a
 * compact lower-case key such as "1(1)". Order is kept; repeats are kept.
 *
 * @param {string} text
 * @returns {string[]}
 */
export function sectionReferences(text) {
  if (typeof text !== "string") return [];
  const refs = [];
  for (const m of normalizeText(text).matchAll(SECTION_REF)) {
    refs.push(m[1].replace(/\s+/g, "").toLowerCase());
  }
  return refs;
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
 *   maxReceiptAgeMs?: number,
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

  // Rule 1: The quoted words must appear in the clause's own text, as whole words (in normal
  // form), and the clause's text must name the section it cites and no other section.
  const normClause = normalizeText(clause.text);
  const normQuote = normalizeText(clause.quote);
  if (!normQuote || findWholeWords(normClause, normQuote) === -1) {
    return reject("The quoted words do not appear in the clause's own text.");
  }

  const citedRefs = [...new Set(sectionReferences(clause.citation))];
  if (citedRefs.length !== 1) {
    return reject("The clause must cite exactly one section, for example \"s. 1(1)\".");
  }
  const citedSection = citedRefs[0];
  const printedRefs = [...new Set(sectionReferences(clause.text))];
  if (!printedRefs.includes(citedSection)) {
    return reject(`The clause's text does not name the section it cites (s. ${citedSection}).`);
  }
  const otherSection = printedRefs.find((ref) => ref !== citedSection);
  if (otherSection) {
    return reject(
      `The clause's text names s. ${otherSection}, but the section it cites is s. ${citedSection}.`,
    );
  }

  // Rule 2: Send that exact citation and quote to check_claim (no as_of date, jurisdiction "ng").
  const sentClaim = {
    citation: clause.citation.trim(),
    quote: clause.quote.trim(),
    jurisdiction: GATE_JURISDICTION,
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

  const rejectReceipt = (reason) => reject(reason, { receipt, claim: sentClaim, verified });

  // Rule 2, second half: the receipt is about the section the clause cites and no other.
  const partRefs = sectionReferences(receipt.part?.pinpoint);
  if (!receipt.part || partRefs.length !== 1 || partRefs[0] !== citedSection) {
    return rejectReceipt(
      `The receipt is not about the section the clause cites (s. ${citedSection}).`,
    );
  }

  // Rule 3: status "matches" or "words_found"; issued just now, with no date sent; and the
  // record is recorded as binding in "ng". Every field is read as the server signs it.
  if (receipt.status !== "matches" && receipt.status !== "words_found") {
    return rejectReceipt(
      `Receipt status is "${receipt.status}"; published wording for the cited section was not confirmed.`,
    );
  }

  const quoteFinding = receipt.findings?.quote;
  if (quoteFinding !== "exact" && quoteFinding !== "normalised") {
    return rejectReceipt("The receipt does not record the quoted words as found in the section.");
  }

  if (receipt.claim?.date_sent !== false) {
    return rejectReceipt("The receipt was not assessed as of today.");
  }

  const nowMs =
    params.now instanceof Date
      ? params.now.getTime()
      : typeof params.now === "number"
        ? params.now
        : typeof params.now === "string"
          ? Date.parse(params.now)
          : Date.now();
  const issuedMs = Date.parse(receipt.issued_at);
  const maxAgeMs =
    typeof params.maxReceiptAgeMs === "number" && params.maxReceiptAgeMs > 0
      ? params.maxReceiptAgeMs
      : MAX_RECEIPT_AGE_MS;
  if (
    !Number.isFinite(nowMs) ||
    !Number.isFinite(issuedMs) ||
    Math.abs(nowMs - issuedMs) > maxAgeMs
  ) {
    return rejectReceipt(
      "The receipt was not issued just now. Check this device's clock, then try again.",
    );
  }

  const applicability = receipt.findings?.applicability;
  if (
    receipt.claim?.jurisdiction !== GATE_JURISDICTION ||
    !applicability ||
    applicability.binds !== "bound"
  ) {
    return rejectReceipt(
      "Open Alvary's record does not show this law as binding in Nigeria (ng).",
    );
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
