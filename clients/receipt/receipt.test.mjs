import { describe, it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  canonicalJson,
  claimDigest,
  verifyReceipt,
  base64UrlToBytes,
  RECEIPT_SCHEMA_VERSION,
} from "./receipt.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  fs.readFileSync(path.join(__dirname, "vectors.json"), "utf8"),
);
const schema = JSON.parse(
  fs.readFileSync(path.join(__dirname, "oa-receipt-1.schema.json"), "utf8"),
);

describe("canonicalJson (RFC 8785 JCS)", () => {
  it("sorts keys lexicographically by UTF-16 code unit and omits whitespace", () => {
    const out = canonicalJson({ z: 1, a: { c: true, b: null }, m: [3, 2, 1] });
    assert.equal(out, '{"a":{"b":null,"c":true},"m":[3,2,1],"z":1}');
  });

  it("omits undefined object properties", () => {
    assert.equal(canonicalJson({ b: 2, a: undefined }), '{"b":2}');
  });

  it("rejects floating-point and non-finite numbers", () => {
    assert.throws(() => canonicalJson({ score: 0.5 }), TypeError);
    assert.throws(() => canonicalJson(NaN), TypeError);
    assert.throws(() => canonicalJson(Infinity), TypeError);
  });

  it("normalizes -0 to 0", () => {
    assert.equal(canonicalJson(-0), "0");
  });
});

describe("claimDigest", () => {
  it("produces 64-character lowercase hex SHA-256 over checked fields only", async () => {
    const d1 = await claimDigest({
      citation: "Freedom of Information Act 2011, s. 1(1)",
      quote: "the right of any person to access or request information",
      jurisdiction: "ng",
    });
    assert.match(d1, /^[0-9a-f]{64}$/);

    // reading is excluded; omitted optional fields are absent
    const d2 = await claimDigest({
      jurisdiction: "ng",
      quote: "the right of any person to access or request information",
      citation: "Freedom of Information Act 2011, s. 1(1)",
      reading: "AI paraphrase that must not affect digest",
      as_of: undefined,
      language: null,
    });
    assert.equal(d1, d2);
  });

  it("changes when any checked field changes", async () => {
    const d1 = await claimDigest({ citation: "Act A, s. 1", jurisdiction: "ng" });
    const d2 = await claimDigest({ citation: "Act A, s. 2", jurisdiction: "ng" });
    assert.notEqual(d1, d2);
  });
});

describe("verifyReceipt golden vectors", () => {
  for (const tc of vectors.cases) {
    it(tc.name + ": " + tc.description, async () => {
      const res = await verifyReceipt(tc.receipt, vectors.jwks, {
        claim: tc.claim,
        now: vectors.now,
      });
      assert.equal(typeof res, "object");
      assert.equal(res.ok, tc.expected.ok);
      assert.equal(res.status, tc.expected.status);
      assert.equal(res.reason, tc.expected.reason);
      if (tc.expected.ageDays !== undefined) {
        assert.equal(res.ageDays, tc.expected.ageDays);
      }
      assert.equal(typeof res.summary, "string");
      assert.ok(res.summary.length > 0);
    });
  }
});

describe("verifyReceipt edge cases", () => {
  const validCase = vectors.cases.find((c) => c.name === "valid_matches");

  it("rejects unsupported schema_version", async () => {
    const res = await verifyReceipt(
      { ...validCase.receipt, schema_version: "oa-receipt-99" },
      vectors.jwks,
    );
    assert.equal(res.ok, false);
    assert.equal(res.reason, "unsupported_version");
  });

  it("rejects unknown kid", async () => {
    const res = await verifyReceipt(
      { ...validCase.receipt, key: { alg: "ES256", kid: "unknown-key-id" } },
      vectors.jwks,
    );
    assert.equal(res.ok, false);
    assert.equal(res.reason, "unknown_key");
  });

  it("rejects receipt issued before key valid_from", async () => {
    const res = await verifyReceipt(
      { ...validCase.receipt, issued_at: "2020-01-01T00:00:00Z" },
      vectors.jwks,
    );
    assert.equal(res.ok, false);
    assert.equal(res.reason, "key_not_yet_valid");
  });

  it("rejects padded base64url signatures", () => {
    assert.throws(() => base64UrlToBytes("AAAA="));
  });

  it("schema defines Draft 2020-12 and oa-receipt-1 envelope fields", () => {
    assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
    assert.equal(schema.$defs.Receipt.properties.schema_version.const, RECEIPT_SCHEMA_VERSION);
    assert.ok(schema.$defs.Receipt.properties.nearest);
    assert.ok(schema.$defs.Receipt.properties.alternatives);
    assert.ok(schema.$defs.ReceiptFindings.properties.opening_text);
  });

  it("web/public/agents/receipt.js is an exact copy of clients/receipt/receipt.js", () => {
    const src = fs.readFileSync(path.join(__dirname, "receipt.js"));
    const copy = fs.readFileSync(
      path.join(__dirname, "../../web/public/agents/receipt.js"),
    );
    assert.ok(src.equals(copy), "web/public/agents/receipt.js must match clients/receipt/receipt.js");
  });
});
