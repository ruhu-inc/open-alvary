/**
 * Browser controller for Example Vendor demo peer site (§12).
 */

import { evaluateLegalBasisGate, ACCEPTED_LABEL, REJECTED_LABEL } from "./gate.js";
import { UnifiedConsentController, createDialogConsentPrompt } from "./consent.js";

const SAMPLE_ALTERED_CLAUSE = {
  text:
    'Under Freedom of Information Act 2011, s. 1(1), "only Nigerian citizens residing in Abuja ' +
    'have the right to access or request information in the custody of any public official."',
  citation: "Freedom of Information Act 2011, s. 1(1)",
  quote:
    "only Nigerian citizens residing in Abuja have the right to access or request information in the custody of any public official.",
};

const SAMPLE_PUBLISHED_CLAUSE = {
  text:
    'Under Freedom of Information Act 2011, s. 1(1), "Notwithstanding anything contained in any ' +
    'other Act, law or regulation, the right of any person to access or request information, ' +
    'whether or not contained in any written form, which is in the custody or possession of any ' +
    'public official, agency or institution howsoever described, is established."',
  citation: "Freedom of Information Act 2011, s. 1(1)",
  quote:
    "Notwithstanding anything contained in any other Act, law or regulation, the right of any person to access or request information, whether or not contained in any written form, which is in the custody or possession of any public official, agency or institution howsoever described, is established.",
};

const dialogEl = document.getElementById("consent-dialog");
const clauseListEl = document.getElementById("clause-list");
const consent = new UnifiedConsentController(createDialogConsentPrompt(dialogEl));

/**
 * Load Open Alvary's <oa-receipt> custom element so the demo site cannot restyle
 * the receipt card's band, published text, or statement.
 */
async function ensureReceiptCard() {
  if (customElements.get("oa-receipt")) return;
  try {
    await import("https://open.alvary.ai/agents/receipt-card.js");
  } catch {
    // Offline/local fallback: attempt repo-relative copy if served from workspace root.
    try {
      await import("../../web/public/agents/receipt-card.js");
    } catch {
      // No-op; renderReceiptFallback handles display if <oa-receipt> is unavailable.
    }
  }
}

function renderClauseResult(clause, result) {
  const block = document.createElement("article");
  block.className = `clause-block ${result.accepted ? "accepted" : "rejected"}`;

  const badge = document.createElement("span");
  badge.className = `clause-badge ${result.accepted ? "accepted" : "rejected"}`;
  badge.textContent = result.accepted ? ACCEPTED_LABEL : REJECTED_LABEL;
  block.appendChild(badge);

  const textP = document.createElement("p");
  textP.className = "clause-text";
  textP.textContent = clause.text;
  block.appendChild(textP);

  if (!result.accepted && result.reason) {
    const reasonP = document.createElement("p");
    reasonP.className = "clause-reason";
    reasonP.textContent = result.reason;
    block.appendChild(reasonP);
  }

  if (result.receipt && result.verified?.ok) {
    const slot = document.createElement("div");
    slot.className = "receipt-slot";
    if (customElements.get("oa-receipt")) {
      const card = document.createElement("oa-receipt");
      if (typeof card.setReceipt === "function") {
        card.setReceipt({ receipt: result.receipt, claim: result.claim, verified: result.verified });
      } else {
        card.receipt = result.receipt;
        card.claim = result.claim;
      }
      slot.appendChild(card);
    } else {
      const fallback = document.createElement("pre");
      fallback.textContent = result.receipt.statement || JSON.stringify(result.receipt, null, 2);
      slot.appendChild(fallback);
    }
    block.appendChild(slot);
  }

  clauseListEl.prepend(block);
}

async function runGateForClause(clause) {
  await ensureReceiptCard();
  const decision = await consent.requestConsent({ target: "https://open.alvary.ai/agents/mcp" });
  const result = await evaluateLegalBasisGate({
    clause,
    consentApproved: decision === "allow",
  });
  renderClauseResult(clause, result);
}

document.getElementById("btn-try-altered")?.addEventListener("click", () => {
  runGateForClause(SAMPLE_ALTERED_CLAUSE);
});

document.getElementById("btn-try-published")?.addEventListener("click", () => {
  runGateForClause(SAMPLE_PUBLISHED_CLAUSE);
});

document.getElementById("btn-reset-consent")?.addEventListener("click", () => {
  consent.reset();
});

// Wire unified engine.onConsent for the embedded vendor_desk runtime when mounted.
export function attachUnifiedConsent(engine) {
  if (engine) {
    engine.onConsent = (info) => consent.requestConsent(info);
  }
}

// Honour ?dev=1 link for judges.
if (new URLSearchParams(location.search).get("dev") === "1") {
  try {
    localStorage.setItem("ns_dev_mode", "1");
  } catch {
    // ignore storage errors
  }
}
