// The receipt panel (design approved 2026-11-23): beside the thread on a wide window, the check the
// visitor chose, in full. The section with the matched words marked, the quote sent, the signature
// this browser checked, the receipt's own fields, the official source, and Save proof and Copy
// summary. A second tab shows the proof as JSON. Everything is built with textContent: no receipt
// data is ever parsed as markup.
import {
  bandHeadlineForReceipt,
  formatJurisdiction,
  formatLongDate,
  recordLinesForReceipt,
  safeLinkHref,
  sectionHref,
  wordsLineForReceipt,
} from "./receipt-card.js";

const LANGUAGES = {
  en: "English",
  fr: "French",
  rw: "Kinyarwanda",
  sw: "Swahili",
  ar: "Arabic",
  pt: "Portuguese",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function newTabLink(href, label) {
  const a = el("a", "rp-link", label);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.appendChild(el("span", "sr-only", " (opens in a new tab)"));
  return a;
}

async function copyWithFeedback(button, text, label) {
  let done = false;
  try {
    await navigator.clipboard.writeText(text);
    done = true;
  } catch {
    done = false;
  }
  button.textContent = done ? "Copied" : "Copy failed";
  setTimeout(() => {
    button.textContent = label;
  }, 1600);
}

// The section's words around the match, with the matched words marked.
function passage(receipt) {
  const f = receipt.findings || {};
  const box = el("div", "rp-passage");
  box.setAttribute("dir", "auto");
  if (receipt.part?.language) box.setAttribute("lang", receipt.part.language);
  if (f.matched_text) {
    if (f.context_before)
      box.append(
        document.createTextNode(
          (f.context_before.length > 200 ? "…" : "") + f.context_before,
        ),
      );
    const mark = el("mark");
    mark.append(
      el("span", "sr-only", "Start of matched quote: "),
      document.createTextNode(f.matched_text),
    );
    mark.append(el("span", "sr-only", " End of matched quote."));
    box.append(mark);
    if (f.context_after) box.append(document.createTextNode(f.context_after));
    return { label: "The section says, with your words marked", box };
  }
  if (f.opening_text) {
    box.textContent = f.opening_text;
    return { label: "The section opens", box };
  }
  return null;
}

function receiptTab(card) {
  const r = card.receipt;
  const c = card.claim;
  const v = card.verification;
  const tab = el("div", "rp-tab");

  tab.append(el("p", "rp-kicker", bandHeadlineForReceipt(r)));
  tab.append(
    el("h2", "rp-title", r.part?.title || c?.citation || "No section checked"),
  );
  const sub = [
    r.part?.pinpoint,
    LANGUAGES[r.part?.language] || r.part?.language,
    r.claim?.jurisdiction && formatJurisdiction(r.claim.jurisdiction),
  ]
    .filter(Boolean)
    .join(" · ");
  if (sub) tab.append(el("p", "rp-sub", sub));

  const lines = el("ul", "rp-lines");
  if (r.part) {
    lines.append(el("li", null, wordsLineForReceipt(r, c).text));
    for (const line of recordLinesForReceipt(r))
      lines.append(el("li", line.secondary ? "secondary" : null, line.text));
    tab.append(lines);
  }

  const shown = passage(r);
  if (shown) {
    tab.append(el("p", "rp-label", shown.label));
    tab.append(shown.box);
  }
  if (c?.quote && !r.findings?.matched_text) {
    tab.append(
      el(
        "p",
        "rp-label",
        card.source === "agent"
          ? "Quote sent (the AI's)"
          : "Quote sent (yours)",
      ),
    );
    tab.append(el("blockquote", "rp-quote", c.quote));
  }

  const verified = el("div", "rp-verified");
  const tick = el("span", "rp-tick");
  tick.setAttribute("aria-hidden", "true");
  const words = el("span");
  words.append(
    el("b", null, "Signature checked in this browser."),
    el("br"),
    el(
      "span",
      "rp-muted",
      "Signed by Open Alvary. Anyone can check it again with the saved proof.",
    ),
  );
  verified.append(tick, words);
  tab.append(verified);

  const meta = el("dl", "rp-meta");
  const age =
    v?.ageDays === 0
      ? "Today"
      : v?.ageDays === 1
        ? "1 day old"
        : v?.ageDays != null
          ? `${v.ageDays} days old`
          : null;
  const rows = [
    ["Receipt ID", r.receipt_id],
    ["Checked", formatLongDate(r.claim?.assessed_at || r.issued_at)],
    ["Age", age],
    ["Signing key", r.key?.kid ? `${r.key.kid} (${r.key.alg})` : null],
    ["Claim digest", r.claim?.digest],
    ["Dataset", r.dataset],
  ];
  for (const [label, value] of rows) {
    if (value == null || value === "") continue;
    const row = el("div");
    row.append(
      el("dt", null, label),
      el(
        "dd",
        /ID|key|digest|Dataset/.test(label) ? "rp-mono" : null,
        String(value),
      ),
    );
    meta.append(row);
  }
  tab.append(meta);

  const links = el("div", "rp-links");
  const recordHref = sectionHref(r.part?.record_url, r.part?.pinpoint);
  if (recordHref) links.append(newTabLink(recordHref, "Open the section ↗"));
  const officialHref = safeLinkHref(r.part?.official_url);
  if (officialHref) links.append(newTabLink(officialHref, "Official source ↗"));
  if (links.childElementCount) tab.append(links);

  const buttons = el("div", "rp-buttons");
  const save = el("button", "rp-btn rp-btn-dark", "Save proof");
  save.type = "button";
  save.addEventListener("click", () => card.saveProof());
  const copy = el("button", "rp-btn", "Copy summary");
  copy.type = "button";
  copy.addEventListener("click", () =>
    copyWithFeedback(copy, card.summaryText(), "Copy summary"),
  );
  buttons.append(save, copy);
  tab.append(buttons);
  return tab;
}

function jsonTab(card) {
  const tab = el("div", "rp-tab");
  const json = JSON.stringify(
    { receipt: card.receipt, claim: card.claim },
    null,
    2,
  );
  tab.append(
    el(
      "p",
      "rp-sub",
      "The proof as a file holds exactly this. Anyone can verify it on this page.",
    ),
  );
  const pre = el("pre", "rp-json", json);
  pre.tabIndex = 0;
  pre.setAttribute("aria-label", "Proof JSON");
  tab.append(pre);
  const copy = el("button", "rp-btn", "Copy JSON");
  copy.type = "button";
  copy.addEventListener("click", () =>
    copyWithFeedback(copy, json, "Copy JSON"),
  );
  const buttons = el("div", "rp-buttons");
  buttons.append(copy);
  tab.append(buttons);
  return tab;
}

// Draws the chosen card's receipt into the panel, keeping the tab the visitor last chose.
export function renderReceiptPanel(panel, card, { tab = "receipt" } = {}) {
  if (!panel || !card?.receipt) return;
  panel.replaceChildren();
  const tabs = el("div", "rp-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "The chosen check");
  const views = { receipt: receiptTab(card), json: jsonTab(card) };
  const names = { receipt: "Receipt", json: "Proof JSON" };
  const buttons = {};
  const show = (name, focus = false) => {
    for (const [key, view] of Object.entries(views)) {
      view.hidden = key !== name;
      buttons[key].setAttribute(
        "aria-selected",
        key === name ? "true" : "false",
      );
      buttons[key].tabIndex = key === name ? 0 : -1;
    }
    panel.dataset.tab = name;
    if (focus) buttons[name].focus();
  };
  for (const key of Object.keys(views)) {
    const b = el("button", "rp-tab-btn", names[key]);
    b.type = "button";
    b.id = `rp-tab-${key}`;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-controls", `rp-view-${key}`);
    b.addEventListener("click", () => show(key));
    b.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const keys = Object.keys(views);
      const next =
        keys[
          (keys.indexOf(key) + (e.key === "ArrowRight" ? 1 : keys.length - 1)) %
            keys.length
        ];
      show(next, true);
    });
    buttons[key] = b;
    tabs.append(b);
    views[key].id = `rp-view-${key}`;
    views[key].setAttribute("role", "tabpanel");
    views[key].setAttribute("aria-labelledby", b.id);
  }
  panel.append(tabs, views.receipt, views.json);
  show(views[tab] ? tab : "receipt");
}
