/**
 * Open Alvary Law check receipt verifier (oa-receipt-1).
 *
 * Zero-dependency ES module for browsers and Node 20+ (Web Crypto).
 * License: MIT — Copyright (c) 2026 Ruhu, Inc.
 */

export const RECEIPT_SCHEMA_VERSION = "oa-receipt-1";
export const RECEIPT_KEYS_URL =
  "https://open.alvary.ai/.well-known/open-alvary-receipt-keys.json";
export const CHECKED_CLAIM_FIELDS = Object.freeze([
  "citation",
  "anchor_id",
  "quote",
  "as_of",
  "jurisdiction",
  "language",
]);

const STATUS_SUMMARIES = Object.freeze({
  matches:
    "Quoted words found; recorded as in force from reviewed legal events.",
  words_found:
    "Quoted words found; in-force status is not confirmed from reviewed legal events.",
  words_found_with_omissions:
    "Quoted words found with words left out.",
  located:
    "Section located; no words were checked.",
  wording_differs:
    "Quoted words were not found in the named section of the published text.",
  not_in_force:
    "Recorded as not in force on the assessed date.",
  does_not_bind:
    "Recorded as not binding in the requested jurisdiction.",
  not_in_catalogue:
    "Law is not in Open Alvary's catalogue.",
  no_part_named:
    "No specific section was named.",
  text_not_available:
    "No published text is available to check.",
  not_in_that_language:
    "No published text in the requested language.",
  ambiguous:
    "More than one section or law matches the citation.",
  part_not_found:
    "Named section was not found in the published text.",
});

const encoder = new TextEncoder();

/**
 * RFC 8785 (JCS) canonical JSON serialization.
 * Receipts hold no floating-point numbers; non-integer numbers are rejected.
 *
 * @param {unknown} value
 * @returns {string}
 */
export function canonicalJson(value) {
  if (value === null) return "null";
  const type = typeof value;
  if (type === "boolean") return value ? "true" : "false";
  if (type === "number") {
    if (!Number.isFinite(value) || !Number.isInteger(value)) {
      throw new TypeError("Receipts must not contain floating-point or non-finite numbers.");
    }
    return Object.is(value, -0) ? "0" : String(value);
  }
  if (type === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  }
  if (type === "object") {
    // RFC 8785 §3.2.3: sort property names by UTF-16 code units.
    const keys = Object.keys(value)
      .filter((k) => value[k] !== undefined)
      .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    const parts = keys.map((k) => JSON.stringify(k) + ":" + canonicalJson(value[k]));
    return "{" + parts.join(",") + "}";
  }
  throw new TypeError(`Unsupported value type in canonical JSON: ${type}`);
}

/**
 * Convert a Uint8Array to lowercase hex.
 *
 * @param {Uint8Array} bytes
 * @returns {string}
 */
export function bytesToHex(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

/**
 * Decode an unpadded base64url string to Uint8Array.
 * Throws if padded or containing invalid characters.
 *
 * @param {string} str
 * @returns {Uint8Array}
 */
export function base64UrlToBytes(str) {
  if (typeof str !== "string" || str.length === 0 || !/^[A-Za-z0-9_-]+$/.test(str)) {
    throw new Error("Invalid unpadded base64url string.");
  }
  const padLen = (4 - (str.length % 4)) % 4;
  if (padLen === 3) {
    throw new Error("Invalid base64url length.");
  }
  const b64 = str.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat(padLen);
  if (typeof atob === "function") {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  // Node fallback if atob were absent
  return new Uint8Array(Buffer.from(b64, "base64"));
}

/**
 * Compute the 64-character lowercase hex SHA-256 digest of a claim's checked fields.
 * Includes only checked fields actually sent (omitted fields are absent; `reading` is excluded).
 *
 * @param {Record<string, unknown>} claim
 * @returns {Promise<string>}
 */
export async function claimDigest(claim) {
  if (!claim || typeof claim !== "object" || Array.isArray(claim)) {
    throw new TypeError("Claim must be a plain object.");
  }
  const checked = {};
  for (const field of CHECKED_CLAIM_FIELDS) {
    if (Object.prototype.hasOwnProperty.call(claim, field) && claim[field] !== undefined && claim[field] !== null) {
      checked[field] = claim[field];
    }
  }
  const payload = encoder.encode(canonicalJson(checked));
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error("Web Crypto subtle API is unavailable.");
  }
  const hashBuf = await subtle.digest("SHA-256", payload);
  return bytesToHex(new Uint8Array(hashBuf));
}

/**
 * Fetch the Open Alvary receipt JWKS document.
 *
 * @param {string} [url]
 * @param {typeof fetch} [fetchImpl]
 * @returns {Promise<{keys: Array<Record<string, unknown>>}>}
 */
export async function loadReceiptKeys(url = RECEIPT_KEYS_URL, fetchImpl = globalThis.fetch) {
  if (typeof fetchImpl !== "function") {
    throw new Error("fetch is unavailable.");
  }
  const resp = await fetchImpl(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new Error(`Failed to load receipt keys (HTTP ${resp.status}).`);
  }
  const data = await resp.json();
  if (!data || !Array.isArray(data.keys)) {
    throw new Error("Invalid receipt key list format.");
  }
  return data;
}

function parseTimestampMs(value) {
  if (typeof value !== "string" || !value.trim()) return NaN;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : NaN;
}

function fail(reason, summary, status = null, ageDays = null) {
  return { ok: false, status, reason, ageDays, summary };
}

/**
 * Verify an Open Alvary receipt against a JWKS key set and optional claim object.
 * Always returns a structured result object `{ ok, status, reason, ageDays, summary }`,
 * never a bare boolean.
 *
 * @param {Record<string, any>} receipt
 * @param {{keys: Array<Record<string, any>>}} jwks
 * @param {{ claim?: Record<string, unknown> | null, now?: Date | string | number }} [options]
 * @returns {Promise<{ ok: boolean, status: string | null, reason: string | null, ageDays: number | null, summary: string }>}
 */
export async function verifyReceipt(receipt, jwks, options = {}) {
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
    return fail("malformed_receipt", "Receipt is missing or not an object.");
  }

  if (typeof receipt.schema_version !== "string") {
    return fail("malformed_receipt", "Receipt is missing schema_version.");
  }
  if (receipt.schema_version !== RECEIPT_SCHEMA_VERSION) {
    return fail(
      "unsupported_version",
      `Unsupported receipt schema version: ${receipt.schema_version}.`,
    );
  }

  if (typeof receipt.signature !== "string" || !receipt.signature) {
    return fail("missing_signature", "Receipt is missing its signature.");
  }

  const keyInfo = receipt.key;
  if (!keyInfo || typeof keyInfo !== "object" || keyInfo.alg !== "ES256" || typeof keyInfo.kid !== "string" || !keyInfo.kid) {
    return fail("invalid_key_metadata", "Receipt has invalid signing key metadata.");
  }

  const issuedMs = parseTimestampMs(receipt.issued_at);
  if (!Number.isFinite(issuedMs)) {
    return fail("invalid_issued_at", "Receipt has an invalid issued_at timestamp.");
  }

  const nowMs = options.now instanceof Date
    ? options.now.getTime()
    : typeof options.now === "number"
      ? options.now
      : typeof options.now === "string"
        ? Date.parse(options.now)
        : Date.now();
  const ageDays = Number.isFinite(nowMs)
    ? Math.max(0, Math.floor((nowMs - issuedMs) / 86400000))
    : null;

  if (!jwks || !Array.isArray(jwks.keys)) {
    return fail("invalid_jwks", "Receipt key list is missing or malformed.", null, ageDays);
  }

  const jwk = jwks.keys.find((k) => k && k.kid === keyInfo.kid);
  if (!jwk) {
    return fail("unknown_key", `Signing key ${keyInfo.kid} is not in Open Alvary's key list.`, null, ageDays);
  }

  if (jwk.revoked_at !== null && jwk.revoked_at !== undefined && jwk.revoked_at !== "") {
    return fail(
      "key_revoked",
      "This receipt was signed with a key that has been withdrawn. Re-check to get a new one.",
      null,
      ageDays,
    );
  }

  if (jwk.valid_from != null) {
    const fromMs = parseTimestampMs(jwk.valid_from);
    if (!Number.isFinite(fromMs) || issuedMs < fromMs) {
      return fail(
        "key_not_yet_valid",
        "Receipt issued_at precedes the signing key's validity window.",
        null,
        ageDays,
      );
    }
  }

  if (jwk.valid_to != null && jwk.valid_to !== "") {
    const toMs = parseTimestampMs(jwk.valid_to);
    if (!Number.isFinite(toMs) || issuedMs > toMs) {
      return fail(
        "key_expired",
        "Receipt issued_at falls after the signing key's validity window.",
        null,
        ageDays,
      );
    }
  }

  let sigBytes;
  try {
    sigBytes = base64UrlToBytes(receipt.signature);
  } catch {
    return fail("invalid_signature_encoding", "Receipt signature is not valid unpadded base64url.", null, ageDays);
  }
  if (sigBytes.length !== 64) {
    return fail("invalid_signature_length", "ES256 raw signature must be exactly 64 bytes (r||s).", null, ageDays);
  }

  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    return fail("crypto_unavailable", "This browser cannot verify receipts.", null, ageDays);
  }

  const { signature: _ignored, ...unsigned } = receipt;
  let canonicalBytes;
  try {
    canonicalBytes = encoder.encode(canonicalJson(unsigned));
  } catch (err) {
    return fail("canonicalization_failed", `Receipt could not be canonicalized: ${err.message}`, null, ageDays);
  }

  let cryptoKey;
  try {
    const publicJwk = {
      kty: jwk.kty,
      crv: jwk.crv,
      x: jwk.x,
      y: jwk.y,
    };
    cryptoKey = await subtle.importKey(
      "jwk",
      publicJwk,
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["verify"],
    );
  } catch {
    return fail("invalid_jwk", "Signing key in JWKS is not a valid P-256 public key.", null, ageDays);
  }

  let verified = false;
  try {
    verified = await subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      cryptoKey,
      sigBytes,
      canonicalBytes,
    );
  } catch {
    verified = false;
  }

  if (!verified) {
    return fail(
      "invalid_signature",
      "The signature does not match. Do not rely on this receipt.",
      null,
      ageDays,
    );
  }

  if (options.claim !== undefined && options.claim !== null) {
    let expectedDigest;
    try {
      expectedDigest = await claimDigest(options.claim);
    } catch (err) {
      return fail("invalid_claim", `Claim could not be digested: ${err.message}`, null, ageDays);
    }
    if (receipt.claim?.digest !== expectedDigest) {
      return fail(
        "claim_digest_mismatch",
        "The receipt does not match the claim shown beside it.",
        null,
        ageDays,
      );
    }
  }

  const status = typeof receipt.status === "string" ? receipt.status : null;
  const summary = (status && STATUS_SUMMARIES[status]) || receipt.statement || "Verified receipt.";
  return {
    ok: true,
    status,
    reason: null,
    ageDays,
    summary,
  };
}
