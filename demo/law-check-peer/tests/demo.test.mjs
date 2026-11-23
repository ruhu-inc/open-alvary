import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  normalizeText,
  parseLawCheckReply,
  evaluateLegalBasisGate,
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

async function makeSignedReceipt({
  privateKey,
  kid,
  claim,
  status = "matches",
  applicability = { jurisdiction: "ng", status: "bound", basis: "national" },
}) {
  const digest = await claimDigest(claim);
  const unsigned = {
    schema_version: "oa-receipt-1",
    receipt_id: "rcpt_demo_001",
    issued_at: "2026-12-15T12:00:00Z",
    assessed_as_of: "2026-12-15",
    dataset_revision: "1:42",
    key: { kid, alg: "ES256" },
    mechanism: { kind: "deterministic_text_match", version: "1" },
    claim: { digest },
    status,
    part: {
      source_id: "ng-foi-act-2011",
      title: "Freedom of Information Act 2011",
      pinpoint: "s. 1(1)",
      anchor_id: "anc_foi_s1_1",
      language: "en",
      jurisdiction: "ng",
    },
    findings: {
      quote_match: status === "wording_differs" ? "absent" : "exact",
      matched_text: claim.quote || null,
      context_before: "",
      context_after: "",
      currentness: {
        status: status === "not_in_force" ? "not_in_force" : "in_force",
        basis: status === "matches" ? "reviewed_events" : "record",
      },
      applicability,
    },
    statement: "Open Alvary's published text of Freedom of Information Act 2011, s. 1(1) contains the quoted words.",
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
    now: "2026-12-16T00:00:00Z",
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
    now: "2026-12-16T00:00:00Z",
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

  // Tampered receipt statement after signing -> signature verification fails!
  const tampered = await evaluateLegalBasisGate({
    clause,
    jwks,
    now: "2026-12-16T00:00:00Z",
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
    now: "2026-12-16T00:00:00Z",
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
    now: "2026-12-16T00:00:00Z",
    callCheckClaim: async (sentClaim) => ({
      ok: true,
      receipt: await makeSignedReceipt({
        privateKey,
        kid,
        claim: sentClaim,
        status: "matches",
        applicability: { jurisdiction: "ng", status: "not_bound", basis: "record" },
      }),
      claim: sentClaim,
    }),
  });
  assert.equal(notBound.accepted, false);

  const declined = await evaluateLegalBasisGate({
    clause,
    jwks,
    consentApproved: false,
  });
  assert.equal(declined.accepted, false);
  assert.equal(declined.label, REJECTED_LABEL);
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
