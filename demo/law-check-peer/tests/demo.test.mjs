import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  normalizeText,
  parseLawCheckReply,
  evaluateLegalBasisGate,
  findWholeWords,
  sectionReferences,
  ACCEPTED_LABEL,
  REJECTED_LABEL,
} from "../gate.js";
import { UnifiedConsentController, CONSENT_MESSAGE } from "../consent.js";
import { canonicalJson, claimDigest } from "../receipt.js";
import {
  runInvalidNetworkCase,
  runCycleCase,
  runOverBudgetCase,
  runDeniedOriginCase,
} from "../conformance.js";

import { AgentNetwork } from "../../../web/public/agents/runtime/kernel.js";
import { runAgent } from "../../../web/public/agents/runtime/runner.js";
import { TraceBus } from "../../../web/public/agents/runtime/trace.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEMO_DIR = path.resolve(__dirname, "..");

// The golden receipts the Python signer produced (clients/receipt/vectors.json). Every
// fixture below starts from one of them, so the gate is always tested against the shape
// the server really signs. If that shape changes, these tests fail with it.
const VECTORS = JSON.parse(
  await readFile(path.resolve(DEMO_DIR, "../../clients/receipt/vectors.json"), "utf8"),
);
const GOLDEN_MATCHES = VECTORS.cases.find((c) => c.name === "valid_matches");
const GOLDEN_WORDS_FOUND = VECTORS.cases.find((c) => c.name === "retired_key_still_valid");
const NOW = "2026-12-16T00:00:00Z";
const JUST_BEFORE_NOW = "2026-12-15T23:59:00Z";

const encoder = new TextEncoder();

function bytesToBase64Url(bytes) {
  return Buffer.from(bytes)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

async function createTestKeyPair(kid = "oa-test-2026-12") {
  const keyPair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    true,
    ["sign", "verify"],
  );
  const pubJwk = await crypto.subtle.exportKey("jwk", keyPair.publicKey);
  const jwks = {
    keys: [
      {
        ...pubJwk,
        kid,
        alg: "ES256",
        use: "sig",
        valid_from: "2026-01-01T00:00:00Z",
        valid_to: "2028-01-01T00:00:00Z",
        revoked_at: null,
      },
    ],
  };
  return { privateKey: keyPair.privateKey, jwks, kid };
}

async function signReceipt(unsignedReceipt, privateKey) {
  const canonical = encoder.encode(canonicalJson(unsignedReceipt));
  const sig = new Uint8Array(
    await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, canonical),
  );
  assert.equal(sig.length, 64, "Web Crypto ECDSA P-256 signature must be 64-byte raw r||s");
  return { ...unsignedReceipt, signature: bytesToBase64Url(sig) };
}

/**
 * A receipt in the server's real shape, signed with a test key. It is the golden
 * "matches" receipt with its claim digest, time and the fields named here replaced.
 */
async function makeSignedReceipt({
  privateKey,
  kid,
  claim,
  status = "matches",
  applicability = { binds: "bound", relation: "national" },
  issuedAt = JUST_BEFORE_NOW,
  pinpoint = "s. 1(1)",
  dateSent = false,
  jurisdiction = "ng",
}) {
  const { signature: _goldenSignature, ...unsigned } = structuredClone(GOLDEN_MATCHES.receipt);
  const absent = status === "wording_differs";
  unsigned.receipt_id = "rcp_01JDDEMOTEST00000000000001";
  unsigned.issued_at = issuedAt;
  unsigned.key = { alg: "ES256", kid };
  unsigned.claim = {
    digest: await claimDigest(claim),
    assessed_at: issuedAt.slice(0, 10),
    date_sent: dateSent,
    jurisdiction,
  };
  unsigned.part.pinpoint = pinpoint;
  unsigned.status = status;
  unsigned.findings = {
    ...unsigned.findings,
    quote: absent ? "absent" : "exact",
    matched_text: absent ? null : claim.quote,
    opening_text: absent ? "Notwithstanding anything contained in any other Act" : null,
    context_before: absent ? null : "1. (1) ",
    context_after: absent ? null : ".",
    currentness: {
      state: status === "not_in_force" ? "not_in_force" : "in_force",
      basis: status === "matches" ? "provision" : "record",
      assessed_at: issuedAt.slice(0, 10),
    },
    applicability,
  };
  return signReceipt(unsigned, privateKey);
}

test("vendor_desk network forwards only country in downstream sly_data (canary stripped)", async () => {
  const netJson = JSON.parse(await readFile(path.join(DEMO_DIR, "vendor_desk.network.json"), "utf8"));
  const net = new AgentNetwork(netJson, "vendor_desk");
  const spec = net.getAgentSpec("vendor_desk");
  assert.deepEqual(spec.allow?.to_downstream?.sly_data, ["country"]);

  let receivedSlyData = null;
  let step = 0;
  const scriptedLlm = {
    async chat() {
      step += 1;
      if (step === 1) {
        return {
          content: "",
          toolCalls: [
            {
              id: "call-1",
              type: "function",
              function: {
                name: "agents_check_law_check",
                arguments: JSON.stringify({ request: "1. Anyone can request public records under FOI Act 2011." }),
              },
            },
          ],
        };
      }
      return { content: "Draft ready.", toolCalls: [] };
    },
  };

  const ctx = {
    network: net,
    frontMan: "vendor_desk",
    displayOrigin: "https://demo.example",
    baseOrigin: "https://demo.example",
    llm: scriptedLlm,
    trace: new TraceBus(),
    slyData: {
      country: "ng",
      buyer_ceiling_canary: "CANARY-LIMIT-DO-NOT-LEAK",
      internal_notes: "private",
    },
    composition: {
      deadline: Date.now() + 90_000,
      maxRemoteCalls: 4,
      maxCallsPerPeer: 1,
      maxAgentCalls: 24,
      remoteCalls: 0,
      peerCalls: new Map(),
      agentCalls: 0,
    },
    requestConsent: async () => "allow",
    resolveRemote: async () => ({
      invoke: async (request) => {
        receivedSlyData = request.sly_data;
        return JSON.stringify([{ claim: 1, none: true }]);
      },
    }),
  };

  await runAgent(ctx, "vendor_desk", "Draft an FOI clause.", null, []);

  assert.deepEqual(receivedSlyData, { country: "ng" });
  assert.equal("buyer_ceiling_canary" in receivedSlyData, false);
  assert.equal("internal_notes" in receivedSlyData, false);
});

test("normalizeText folds quotes, dashes, ligatures, soft hyphens and whitespace", () => {
  const a = normalizeText("“Right\u00AD of\u00A0any  person” — \uFB01ling");
  const b = normalizeText('"Right of any person" - filing');
  assert.equal(a, b);
});

test("parseLawCheckReply validates fixed JSON shape and rejects prose", () => {
  const good = parseLawCheckReply(
    JSON.stringify([
      { claim: 1, citation: "FOI Act 2011, s. 1(1)", quote: "the right of any person to access or request information" },
      { claim: 2, none: true },
    ]),
  );
  assert.equal(good.ok, true);
  assert.equal(good.candidates.length, 2);

  const bad = parseLawCheckReply("Here is the section you asked for: s. 1(1).");
  assert.equal(bad.ok, false);
});

test("legal-basis gate accepts published wording with live verified receipt binding in ng", async () => {
  const { privateKey, jwks, kid } = await createTestKeyPair();
  const quote =
    "the right of any person to access or request information, whether or not contained in any written form";
  // Clause uses curly quotes and extra spacing — normal form must still match.
  const clause = {
    text: `Under Freedom of Information Act 2011, s. 1(1), “the right of any person to access or request information,\u00A0whether or not contained in any written form” applies.`,
    citation: "Freedom of Information Act 2011, s. 1(1)",
    quote,
  };

  const result = await evaluateLegalBasisGate({
    clause,
    jwks,
    now: NOW,
    callCheckClaim: async (sentClaim) => {
      assert.equal(sentClaim.as_of, undefined, "gate must not send as_of");
      assert.equal(sentClaim.jurisdiction, "ng");
      const receipt = await makeSignedReceipt({
        privateKey,
        kid,
        claim: sentClaim,
        status: "words_found",
      });
      return { ok: true, receipt, claim: sentClaim };
    },
  });

  assert.equal(result.accepted, true);
  assert.equal(result.label, ACCEPTED_LABEL);
  assert.doesNotMatch(result.label, /required by law/i);
  assert.equal(result.verified.ok, true);
});

test("legal-basis gate rejects altered wording not present in clause text", async () => {
  const { jwks } = await createTestKeyPair();
  const result = await evaluateLegalBasisGate({
    clause: {
      text: "Only residents of Abuja may request public records within 30 days.",
      citation: "Freedom of Information Act 2011, s. 1(1)",
      quote: "the right of any person to access or request information, whether or not contained in any written form",
    },
    jwks,
    callCheckClaim: async () => {
      throw new Error("check_claim should not be called when quote is absent from clause text");
    },
  });

  assert.equal(result.accepted, false);
  assert.equal(result.label, REJECTED_LABEL);
  assert.match(result.reason, /do not appear in the clause/i);
});

test("legal-basis gate rejects cross-section citation/digest mismatch and tampered signature", async () => {
  const { privateKey, jwks, kid } = await createTestKeyPair();
  const quote =
    "the right of any person to access or request information, whether or not contained in any written form";
  const clause = {
    text: `Under Freedom of Information Act 2011, s. 99(9), "${quote}".`,
    citation: "Freedom of Information Act 2011, s. 99(9)",
    quote,
  };

  // Server receipt was actually signed for s. 1(1), not s. 99(9) -> claim.digest mismatch!
  const crossSection = await evaluateLegalBasisGate({
    clause,
    jwks,
    now: NOW,
    callCheckClaim: async (sentClaim) => {
      const receipt = await makeSignedReceipt({
        privateKey,
        kid,
        claim: { ...sentClaim, citation: "Freedom of Information Act 2011, s. 1(1)" },
        status: "matches",
      });
      return { ok: true, receipt, claim: sentClaim };
    },
  });
  assert.equal(crossSection.accepted, false);
  assert.equal(crossSection.label, REJECTED_LABEL);
  assert.equal(crossSection.verified?.reason, "claim_digest_mismatch");

  // Tampered receipt statement after signing -> signature verification fails!
  const tampered = await evaluateLegalBasisGate({
    clause,
    jwks,
    now: NOW,
    callCheckClaim: async (sentClaim) => {
      const receipt = await makeSignedReceipt({
        privateKey,
        kid,
        claim: sentClaim,
        status: "matches",
      });
      receipt.statement = "Tampered statement";
      return { ok: true, receipt, claim: sentClaim };
    },
  });
  assert.equal(tampered.accepted, false);
  assert.equal(tampered.label, REJECTED_LABEL);
  assert.equal(tampered.verified?.reason, "invalid_signature");
});

test("legal-basis gate rejects wording_differs, non-ng binding, and declined consent", async () => {
  const { privateKey, jwks, kid } = await createTestKeyPair();
  const quote =
    "the right of any person to access or request information, whether or not contained in any written form";
  const clause = {
    text: `Clause quoting "${quote}" under FOI Act 2011, s. 1(1).`,
    citation: "Freedom of Information Act 2011, s. 1(1)",
    quote,
  };

  const differs = await evaluateLegalBasisGate({
    clause,
    jwks,
    now: NOW,
    callCheckClaim: async (sentClaim) => ({
      ok: true,
      receipt: await makeSignedReceipt({ privateKey, kid, claim: sentClaim, status: "wording_differs" }),
      claim: sentClaim,
    }),
  });
  assert.equal(differs.accepted, false);

  const notBound = await evaluateLegalBasisGate({
    clause,
    jwks,
    now: NOW,
    callCheckClaim: async (sentClaim) => ({
      ok: true,
      receipt: await makeSignedReceipt({
        privateKey,
        kid,
        claim: sentClaim,
        status: "words_found",
        applicability: { binds: "unknown", relation: null },
      }),
      claim: sentClaim,
    }),
  });
  assert.equal(notBound.accepted, false);
  assert.match(notBound.reason, /binding in Nigeria/i);

  const doesNotBind = await evaluateLegalBasisGate({
    clause,
    jwks,
    now: NOW,
    callCheckClaim: async (sentClaim) => ({
      ok: true,
      receipt: await makeSignedReceipt({
        privateKey,
        kid,
        claim: sentClaim,
        status: "does_not_bind",
        applicability: { binds: "not_bound", relation: null },
      }),
      claim: sentClaim,
    }),
  });
  assert.equal(doesNotBind.accepted, false);

  const declined = await evaluateLegalBasisGate({
    clause,
    jwks,
    consentApproved: false,
  });
  assert.equal(declined.accepted, false);
  assert.equal(declined.label, REJECTED_LABEL);
});

test("the golden receipts carry every field the gate reads", () => {
  for (const golden of [GOLDEN_MATCHES, GOLDEN_WORDS_FOUND]) {
    const r = golden.receipt;
    assert.equal(typeof r.status, "string");
    assert.equal(typeof r.issued_at, "string");
    assert.equal(r.claim.date_sent, false);
    assert.equal(r.claim.jurisdiction, "ng");
    assert.deepEqual(sectionReferences(r.part.pinpoint), ["1(1)"]);
    assert.ok(["exact", "normalised"].includes(r.findings.quote));
    assert.equal(r.findings.applicability.binds, "bound");
    // Names the server never sends; the gate must not depend on them.
    assert.equal("status" in r.findings.applicability, false);
    assert.equal("jurisdiction" in r.findings.applicability, false);
  }
});

test("legal-basis gate accepts the server's own golden receipts, unchanged", async () => {
  for (const golden of [GOLDEN_MATCHES, GOLDEN_WORDS_FOUND]) {
    const clause = {
      text: `Under ${golden.claim.citation}, "${golden.claim.quote}" applies to this request.`,
      citation: golden.claim.citation,
      quote: golden.claim.quote,
    };
    const result = await evaluateLegalBasisGate({
      clause,
      jwks: VECTORS.jwks,
      now: Date.parse(golden.receipt.issued_at) + 30_000,
      callCheckClaim: async (sentClaim) => {
        assert.deepEqual(sentClaim, golden.claim, "the gate sends the clause's own citation and quote");
        return { ok: true, receipt: structuredClone(golden.receipt), claim: sentClaim };
      },
    });
    assert.equal(result.accepted, true, `${golden.name}: ${result.reason}`);
    assert.equal(result.label, ACCEPTED_LABEL);
    assert.equal(result.receipt.status, golden.receipt.status);
  }
});

test("legal-basis gate refuses a clause that prints one section and cites another", async () => {
  const quote = "the right of any person to access or request information";
  const neverCalled = async () => {
    throw new Error("check_claim must not be called for a clause that fails rule 1");
  };

  const printsAnother = await evaluateLegalBasisGate({
    clause: {
      text: `Under FOI Act 2011, s. 30, no person has "${quote}" unless resident in Abuja.`,
      citation: "Freedom of Information Act 2011, s. 1(1)",
      quote,
    },
    callCheckClaim: neverCalled,
  });
  assert.equal(printsAnother.accepted, false);
  assert.match(printsAnother.reason, /does not name the section it cites/i);

  const printsBoth = await evaluateLegalBasisGate({
    clause: {
      text: `Under FOI Act 2011, s. 1(1) and section 30, "${quote}" is limited.`,
      citation: "Freedom of Information Act 2011, s. 1(1)",
      quote,
    },
    callCheckClaim: neverCalled,
  });
  assert.equal(printsBoth.accepted, false);
  assert.match(printsBoth.reason, /names s\. 30/i);

  const citesNone = await evaluateLegalBasisGate({
    clause: {
      text: `Under the Freedom of Information Act 2011, "${quote}" applies.`,
      citation: "Freedom of Information Act 2011",
      quote,
    },
    callCheckClaim: neverCalled,
  });
  assert.equal(citesNone.accepted, false);
  assert.match(citesNone.reason, /exactly one section/i);
});

test("legal-basis gate refuses a quote that only matches inside a longer word", async () => {
  assert.equal(findWholeWords("A non-public institution shall not", "public institution shall not"), -1);
  assert.equal(findWholeWords("the republic institution", "public institution"), -1);
  assert.equal(findWholeWords('says "public institution" here', "public institution"), 6);

  const quote = "public institution shall not be required to disclose the record";
  const result = await evaluateLegalBasisGate({
    clause: {
      text: `Under FOI Act 2011, s. 1(1), a non-${quote}.`,
      citation: "Freedom of Information Act 2011, s. 1(1)",
      quote,
    },
    callCheckClaim: async () => {
      throw new Error("check_claim must not be called when the quote is not whole words in the clause");
    },
  });
  assert.equal(result.accepted, false);
  assert.match(result.reason, /do not appear in the clause/i);
});

test("legal-basis gate refuses a receipt for another section, a dated one and a stale one", async () => {
  const { privateKey, jwks, kid } = await createTestKeyPair();
  const quote = "the right of any person to access or request information";
  const clause = {
    text: `Under FOI Act 2011, s. 1(1), "${quote}" applies.`,
    citation: "Freedom of Information Act 2011, s. 1(1)",
    quote,
  };
  const run = (overrides) =>
    evaluateLegalBasisGate({
      clause,
      jwks,
      now: NOW,
      callCheckClaim: async (sentClaim) => ({
        ok: true,
        receipt: await makeSignedReceipt({ privateKey, kid, claim: sentClaim, ...overrides }),
        claim: sentClaim,
      }),
    });

  const baseline = await run({});
  assert.equal(baseline.accepted, true, baseline.reason);

  const otherSection = await run({ pinpoint: "s. 2" });
  assert.equal(otherSection.accepted, false);
  assert.match(otherSection.reason, /not about the section the clause cites/i);

  const dated = await run({ dateSent: true });
  assert.equal(dated.accepted, false);
  assert.match(dated.reason, /as of today/i);

  const stale = await run({ issuedAt: "2026-12-15T21:00:00Z" });
  assert.equal(stale.accepted, false);
  assert.match(stale.reason, /not issued just now/i);

  const otherCountry = await run({ jurisdiction: "rw" });
  assert.equal(otherCountry.accepted, false);
  assert.match(otherCountry.reason, /binding in Nigeria/i);
});

test("unified consent controller shows one card for both peer call and check_claim", async () => {
  let seenMessage = "";
  const allowCtrl = new UnifiedConsentController(async ({ message }) => {
    seenMessage = message;
    return "allow";
  });

  const d1 = await allowCtrl.requestConsent({ peer: "open.alvary.ai" });
  const d2 = await allowCtrl.requestConsent({ target: "https://open.alvary.ai/agents/mcp" });
  assert.equal(d1, "allow");
  assert.equal(d2, "allow");
  assert.equal(allowCtrl.promptCount, 1, "one consent card covers both calls");
  assert.equal(seenMessage, CONSENT_MESSAGE);

  const declineCtrl = new UnifiedConsentController(async () => "decline");
  const d3 = await declineCtrl.requestConsent({ peer: "open.alvary.ai" });
  assert.equal(d3, "decline");
});

test("Stage One Conformance: all 4 refusal cases stop with visible runtime reports", async () => {
  const invalidNet = await runInvalidNetworkCase();
  assert.equal(invalidNet.stopped, true);
  assert.match(invalidNet.report, /nonexistent_specialist/);

  const cycle = await runCycleCase();
  assert.equal(cycle.stopped, true);
  assert.match(cycle.report, /Skipped.*already in this request's call chain/i);

  const overBudget = await runOverBudgetCase();
  assert.equal(overBudget.stopped, true);
  assert.match(overBudget.report, /Skipped/i);
  assert.ok(overBudget.trace.some((e) => e.kind === "composition_limit"));

  const denied = await runDeniedOriginCase();
  assert.equal(denied.stopped, true);
  assert.match(denied.report, /Policy:.*allowed_peers/i);
});
