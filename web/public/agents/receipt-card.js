/**
 * <oa-receipt> — Open Alvary verified receipt card Web Component.
 *
 * Open Shadow DOM, module-scoped <template>, single shared constructed CSSStyleSheet
 * via adoptedStyleSheets, @container queries, @layer, ElementInternals :state(),
 * zero innerHTML with untrusted data.
 */

const JURISDICTION_NAMES = Object.freeze({
  ng: "Nigeria",
  rw: "Rwanda",
  gh: "Ghana",
  ke: "Kenya",
  za: "South Africa",
});

export function formatJurisdiction(code) {
  if (!code) return "the requested jurisdiction";
  const key = String(code).toLowerCase();
  return JURISDICTION_NAMES[key] || String(code).toUpperCase();
}

export function formatLongDate(isoDate) {
  if (!isoDate) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(isoDate));
  if (!m) return String(isoDate);
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  if (Number.isNaN(d.getTime())) return String(isoDate);
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(d);
}

const GREY_STATUSES = new Set([
  "not_in_catalogue",
  "no_part_named",
  "ambiguous",
  "part_not_found",
  "text_not_available",
  "not_in_that_language",
]);

export function lookForStatus(status) {
  if (status === "matches") return "green";
  if (status === "words_found" || status === "words_found_with_omissions") return "amber";
  if (status === "wording_differs" || status === "not_in_force" || status === "does_not_bind") return "red";
  if (status === "located") return "neutral";
  return "grey";
}

export function greyHeadlineForStatus(status) {
  switch (status) {
    case "not_in_catalogue":
      return "Not in Open Alvary's catalogue";
    case "no_part_named":
      return "Name a section";
    case "text_not_available":
      return "No published text to check";
    case "not_in_that_language":
      return "No published text in that language";
    case "ambiguous":
      return "More than one section matches";
    case "part_not_found":
      return "Section not found in the published text";
    default:
      return "Could not check";
  }
}

/**
 * The result in a few words, shown in the band so a card says its result even when closed.
 * It never says more than the two lines below it.
 */
export function bandHeadlineForReceipt(receipt) {
  const status = receipt?.status;
  if (GREY_STATUSES.has(status)) return greyHeadlineForStatus(status);
  switch (status) {
    case "matches":
      return "Quoted words found";
    case "words_found":
      return "Quoted words found · status not confirmed";
    case "words_found_with_omissions":
      return "Quoted words found, with words left out";
    case "wording_differs":
      return "Quoted words not found";
    case "not_in_force":
      return "Recorded as not in force";
    case "does_not_bind":
      return `Recorded as not binding in ${formatJurisdiction(receipt?.claim?.jurisdiction)}`;
    case "located":
      return "Section found";
    default:
      return "Could not check";
  }
}

/** http(s) and same-site addresses only; anything else is not made into a link. */
export function safeLinkHref(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const base = typeof location !== "undefined" && location.href ? location.href : "https://open.alvary.ai/";
    const url = new URL(value, base);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

// Where a pinpoint is on the law's page: "s. 1(1)" at #section-1, "art. 5" at #article-5, as the
// site's reader names them. A pinpoint it cannot place gives no address, and the page opens at its top.
export function sectionHash(pinpoint) {
  const text = String(pinpoint || "").trim();
  const section = /^(?:ss?|secs?|sect|sections?)\.?\s*(\d+[A-Za-z]*)\b/i.exec(text);
  if (section) return `#section-${section[1]}`;
  const article = /^(?:arts?|articles?)\.?\s*(\d+[A-Za-z]*)\b/i.exec(text);
  if (article) return `#article-${article[1]}`;
  return "";
}

// The record's address with the part's own place on the page, when the address has none.
export function sectionHref(recordUrl, pinpoint) {
  const href = safeLinkHref(recordUrl);
  if (!href || href.includes("#")) return href;
  return href + sectionHash(pinpoint);
}

export function wordsLineForReceipt(receipt, claim = null) {
  const quoteFinding = receipt?.findings?.quote;
  const pinpoint = receipt?.part?.pinpoint || "this section";
  switch (quoteFinding) {
    case "exact":
    case "normalised":
      return { icon: "tick", text: "Quoted words found" };
    case "with_omissions":
      return { icon: "exclamation", text: "Quoted words found, with words left out" };
    case "absent":
      // Every word is there, in order: only capital letters or punctuation differ.
      if (
        typeof claim?.quote === "string" &&
        typeof receipt?.findings?.opening_text === "string" &&
        marksOnlyDifference(claim.quote, receipt.findings.opening_text)
      ) {
        return {
          icon: "cross",
          text: `Not found exactly as quoted in ${pinpoint}: the capital letters or punctuation differ`,
        };
      }
      return { icon: "cross", text: `These words are not in ${pinpoint} of the published text` };
    case "not_given":
    default:
      return { icon: "dash", text: "No words were checked" };
  }
}

export function recordLinesForReceipt(receipt) {
  const lines = [];
  const curr = receipt?.findings?.currentness;
  const app = receipt?.findings?.applicability;
  const dateText = formatLongDate(curr?.assessed_at || receipt?.claim?.assessed_at);
  const country = formatJurisdiction(receipt?.claim?.jurisdiction);

  if (curr) {
    if (curr.state === "not_in_force") {
      lines.push({ icon: "cross", text: `Recorded as not in force on ${dateText}` });
    } else if (curr.state === "in_force" && curr.basis === "provision") {
      lines.push({
        icon: "tick",
        text: `Recorded as in force on ${dateText}, from reviewed legal events`,
      });
    } else if (curr.state === "in_force" && curr.basis === "record" && receipt?.claim?.date_sent) {
      lines.push({
        icon: "question",
        text: `Listed as in force on ${dateText}, by the text's effective dates only (record status not checked for past dates).`,
      });
    } else if (curr.state === "in_force") {
      lines.push({
        icon: "question",
        text: "Open Alvary's record lists this law as in force. Its amendments and repeals have not been reviewed, so this is not confirmed.",
      });
    } else if (curr.state === "partly_in_force") {
      lines.push({ icon: "question", text: `Recorded as partly in force on ${dateText}` });
    } else if (curr.state === "contested") {
      lines.push({ icon: "question", text: `Recorded as contested on ${dateText}` });
    } else {
      lines.push({
        icon: "question",
        text: `Status on ${dateText} is unknown in Open Alvary's record`,
      });
    }
  }

  if (app && receipt?.claim?.jurisdiction) {
    if (app.binds === "not_bound") {
      lines.push({ icon: "cross", text: `Recorded as not binding in ${country}` });
    } else if (app.binds === "unknown") {
      lines.push({
        icon: "question",
        text: `Whether this law binds in ${country} is unknown in Open Alvary's record`,
      });
    } else if (app.binds === "bound") {
      const relationText =
        app.relation === "national"
          ? `Recorded as a national law of ${country}.`
          : `Recorded as binding in ${country}.`;
      lines.push({ icon: "none", text: relationText, secondary: true });
    }
  }

  return lines;
}

const SVG_NS = "http://www.w3.org/2000/svg";

// Where a quote that was not found departs from the section's opening words. Both are split into
// words; the quote's first words are looked for in the section, then the shared words at each end
// are set aside, and what is left is the difference. Returns null when the two cannot be lined up
// with confidence (the quote starts outside the opening the receipt carries, or differs too much).
export function wordDifference(quote, sectionText) {
  if (typeof quote !== "string" || typeof sectionText !== "string") return null;
  const split = (t) => t.trim().split(/\s+/).filter(Boolean);
  const norm = (w) => w.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  const q = split(quote);
  const s = split(sectionText);
  if (q.length < 4 || s.length < 4) return null;
  const qn = q.map(norm);
  const sn = s.map(norm);
  const anchor = 3;
  let start = -1;
  for (let i = 0; i + anchor <= sn.length; i++) {
    if (qn.slice(0, anchor).every((w, k) => w === sn[i + k])) {
      start = i;
      break;
    }
  }
  if (start < 0) return null;
  let p = 0;
  while (p < qn.length && start + p < sn.length && qn[p] === sn[start + p]) p++;
  if (p === qn.length) return null; // the whole quote is there: nothing to show
  // Line the ends up: the quote's last words, found in the section after the shared start.
  let best = null;
  for (let qEnd = qn.length; qEnd > p && !best; qEnd--) {
    // At the quote's very end one shared word is enough to line up, so a slip in its last words
    // (a typo in "writen form") is still shown.
    const longest = Math.min(anchor, qEnd - p);
    const shortest = qEnd === qn.length ? 1 : longest;
    for (let size = longest; size >= shortest && !best; size--) {
      const tail = qn.slice(qEnd - size, qEnd);
      for (let j = start + p; j + tail.length <= sn.length; j++) {
        if (tail.every((w, k) => w === sn[j + k])) {
          best = { qEnd, sEnd: j + tail.length };
          break;
        }
      }
    }
  }
  if (!best) {
    // The quote departs only in its last word or two: set them beside the section's words in their place.
    const left = qn.length - p;
    if (left > 2 || start + p + left > sn.length) return null;
    best = { qEnd: qn.length, sEnd: start + p + left };
  }
  // The quote's own last words must line up, unless the section's opening ends first.
  if (best.qEnd < qn.length && best.sEnd < sn.length) return null;
  let qa = best.qEnd;
  let sa = best.sEnd;
  // Walk back over the shared words at the end.
  while (qa > p && sa > start + p && qn[qa - 1] === sn[sa - 1]) {
    qa--;
    sa--;
  }
  const quoteWords = q.slice(p, qa);
  const sectionWords = s.slice(start + p, sa);
  if (quoteWords.length > 12 || sectionWords.length > 12) return null;
  if (!quoteWords.length && !sectionWords.length) return null;
  return {
    before: q.slice(Math.max(0, p - 6), p).join(" "),
    quoteWords: quoteWords.join(" "),
    sectionWords: sectionWords.join(" "),
    after: q.slice(qa, Math.min(q.length, qa + 6)).join(" "),
    leadingCut: p > 6,
    trailingCut: qa + 6 < q.length,
  };
}

// A quote that was not found although every one of its words is in the section, in order: only
// capital letters or punctuation differ ("The right…" for "the right…", a full stop for a comma).
// Returns the section's own words for that stretch, or null.
export function marksOnlyDifference(quote, sectionText) {
  if (typeof quote !== "string" || typeof sectionText !== "string") return null;
  const split = (t) => t.trim().split(/\s+/).filter(Boolean);
  const norm = (w) => w.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  const q = split(quote).map(norm).filter(Boolean);
  const words = split(sectionText);
  const s = words.map(norm);
  if (q.length < 4) return null;
  for (let i = 0; i + q.length <= s.length; i++) {
    if (q.every((w, k) => w === s[i + k])) {
      const found = words.slice(i, i + q.length);
      const shown = found.length > 40 ? [...found.slice(0, 40), "…"] : found;
      return shown.join(" ");
    }
  }
  return null;
}

function createSvgIcon(kind, size = 18) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("icon");

  const add = (tag, attrs) => {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.appendChild(el);
  };

  if (kind === "band-green") {
    // Tick in a circle
    add("circle", { cx: "10", cy: "10", r: "8.25", fill: "none", stroke: "currentColor", "stroke-width": "1.6" });
    add("path", { d: "M6.2 10.3 8.8 12.9 14 7.3", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" });
  } else if (kind === "band-amber") {
    // Exclamation mark in a triangle
    add("path", { d: "M10 2.3 18.2 16.8H1.8Z", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linejoin": "round" });
    add("path", { d: "M10 7.2v4.6", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round" });
    add("circle", { cx: "10", cy: "14.4", r: "1", fill: "currentColor" });
  } else if (kind === "band-red") {
    // Cross in an octagon
    add("polygon", { points: "6.6,2 13.4,2 18,6.6 18,13.4 13.4,18 6.6,18 2,13.4 2,6.6", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linejoin": "round" });
    add("path", { d: "M7 7l6 6M13 7l-6 6", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round" });
  } else if (kind === "band-grey") {
    // Dash in a square
    add("rect", { x: "2.4", y: "2.4", width: "15.2", height: "15.2", rx: "2.2", fill: "none", stroke: "currentColor", "stroke-width": "1.6" });
    add("path", { d: "M6.2 10h7.6", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round" });
  } else if (kind === "band-neutral") {
    // Pin
    add("path", { d: "M10 18s5.5-5.1 5.5-9.5a5.5 5.5 0 1 0-11 0C4.5 12.9 10 18 10 18Z", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linejoin": "round" });
    add("circle", { cx: "10", cy: "8.5", r: "2", fill: "currentColor" });
  } else if (kind === "copy") {
    add("rect", { x: "6.5", y: "6.5", width: "10", height: "10", rx: "2", fill: "none", stroke: "currentColor", "stroke-width": "1.5" });
    add("path", { d: "M13.5 6.5V5a1.5 1.5 0 0 0-1.5-1.5H5A1.5 1.5 0 0 0 3.5 5v7A1.5 1.5 0 0 0 5 13.5h1.5", fill: "none", stroke: "currentColor", "stroke-width": "1.5" });
  } else if (kind === "save") {
    add("path", { d: "M10 3.5v9M6.5 9 10 12.5 13.5 9", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round" });
    add("path", { d: "M3.5 13v2a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-2", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round" });
  } else if (kind === "tick") {
    add("path", { d: "M4.5 10.4 8.2 14 15.5 6.4", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" });
  } else if (kind === "exclamation") {
    add("path", { d: "M10 4.5v6.5", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round" });
    add("circle", { cx: "10", cy: "14.5", r: "1.2", fill: "currentColor" });
  } else if (kind === "cross") {
    add("path", { d: "M5.5 5.5l9 9M14.5 5.5l-9 9", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round" });
  } else if (kind === "dash") {
    add("path", { d: "M5 10h10", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round" });
  } else if (kind === "question") {
    add("path", { d: "M7.5 7.4a2.6 2.6 0 0 1 5 .8c0 1.7-2.5 2-2.5 3.6", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round" });
    add("circle", { cx: "10", cy: "15.2", r: "1.1", fill: "currentColor" });
  }
  return svg;
}

const CARD_CSS = `
@layer reset, base, components;

@layer reset {
  *, *::before, *::after {
    box-sizing: border-box;
  }
  button {
    font: inherit;
    color: inherit;
    cursor: pointer;
    touch-action: manipulation;
  }
  a {
    color: inherit;
    text-decoration: underline;
    text-underline-offset: 2px;
    overflow-wrap: anywhere;
  }
  button:focus-visible,
  a:focus-visible,
  summary:focus-visible {
    outline: 3px solid #737373;
    outline-offset: 3px;
  }
}

@layer base {
  :host {
    display: block;
    container-type: inline-size;
    font-family: "DM Sans", system-ui, -apple-system, sans-serif;
    color: #1a1a1a;
    font-size: 16px;
    line-height: 1.55;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
}

@layer components {
  .card {
    background: #ffffff;
    border: 1px solid #ededed;
    border-radius: 6px;
    overflow: hidden;
  }
  .diff {
    margin-block: 4px 2px;
  }
  .diff-text {
    margin: 4px 0 0;
    padding: 10px 12px;
    border-radius: 6px;
    background: #faf8f5;
    font: 15px/1.6 Georgia, "Times New Roman", serif;
    color: #3a3a3a;
  }
  .diff-text del {
    background: #eeeeee;
    color: #6b6b6b;
    padding: 0 2px;
  }
  .diff-text ins {
    background: #edf3ea;
    color: #1a1a1a;
    text-decoration: none;
    padding: 0 2px;
  }
  .diff-note {
    display: block;
    margin-bottom: 4px;
    font: 13px/1.5 "DM Sans", system-ui, -apple-system, sans-serif;
    color: #4a4a4a;
  }
  /* Beside the receipt panel, the card is the verdict; the passage, the quote and the signature
     details are in the panel. */
  :host([panel]) [data-ref="published-section"],
  :host([panel]) [data-ref="quote-section"],
  :host([panel]) [data-ref="details"] {
    display: none;
  }
  /* The chosen card is told apart by a slightly darker edge only: no shadow, no colour. */
  :host([selected]) .card {
    border-color: #d6d6d6;
  }
  .card.highlighted {
    border-color: #cfcfcf;
  }
  .band {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 500;
  }
  .band-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-inline-size: 0;
  }
  .band-text {
    overflow-wrap: break-word;
  }
  .band[data-look="green"] {
    background: #f1f7f3;
    color: #2f6b4f;
  }
  .band[data-look="amber"] {
    background: #faf5ec;
    color: #6e4a23;
  }
  /* Not found and the like: told by the words and the icon, in plain grey, never in red. */
  .band[data-look="red"] {
    background: #f6f6f5;
    color: #3a3a3a;
  }
  .band[data-look="grey"] {
    background: #f6f6f5;
    color: #3a3a3a;
  }
  .band[data-look="neutral"] {
    background: #ffffff;
    color: #1a1a1a;
    border-block-end: 1px solid #f0f0f0;
  }
  .collapse-toggle {
    flex: none;
    background: transparent;
    border: 1px solid #e6e6e6;
    border-radius: 6px;
    padding: 2px 10px;
    font-size: 12.5px;
    font-weight: 500;
    min-block-size: 28px;
    min-inline-size: 28px;
  }
  .body {
    padding: 12px 16px 14px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .header-block {
    padding: 14px 16px 0;
  }
  .card[data-collapsed="true"] .header-block {
    padding-bottom: 14px;
  }
  .card[data-collapsed="true"] .body {
    display: none;
  }
  .ai-suggested {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: #6b6b6b;
    margin: 0 0 4px;
  }
  .title {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    line-height: 1.3;
    color: #1a1a1a;
    text-wrap: balance;
    overflow-wrap: break-word;
  }
  .lines {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .finding-line {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    font-size: 14px;
    line-height: 1.5;
  }
  .finding-line .icon {
    flex: none;
    margin-top: 3px;
  }
  .finding-line.secondary {
    padding-inline-start: 26px;
    font-size: 13px;
    color: #6b6b6b;
  }
  /* Quiet sentence-case labels; the passage and the quote carry the weight. */
  .section-label {
    font-size: 12px;
    font-weight: 500;
    color: #6b6b6b;
    margin: 0 0 4px;
  }
  .published-block {
    border-inline-start: 2px solid #e3d6c4;
    padding-inline-start: 14px;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 15.5px;
    line-height: 1.65;
    color: #1a1a1a;
    overflow-wrap: break-word;
    max-inline-size: 72ch;
  }
  .published-block mark {
    background: #fbf3dc;
    color: #1a1a1a;
    text-decoration: underline;
    text-decoration-color: #c9ab83;
    text-decoration-thickness: 1px;
    text-underline-offset: 3px;
    padding: 0 2px;
    border-radius: 2px;
  }
  .published-block .omitted {
    color: #5e5e5e;
    text-decoration: underline dotted #5e5e5e;
    text-underline-offset: 3px;
  }
  .show-all-btn {
    margin-top: 8px;
    background: transparent;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 13px;
    min-block-size: 28px;
  }
  .quote-sent,
  .ai-reading {
    margin: 0;
    padding: 8px 12px;
    background: #fafafa;
    border: 1px solid #f0f0f0;
    border-radius: 6px;
    font-size: 13.5px;
    line-height: 1.55;
    overflow-wrap: break-word;
  }
  .ai-reading {
    font-size: 13.5px;
    color: #3a3a3a;
  }
  .grey-note {
    margin: 0;
    font-size: 14px;
    line-height: 1.55;
    text-wrap: pretty;
  }
  .nearest-box {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    padding: 10px 12px;
    background: #fafafa;
    border: 1px solid #f0f0f0;
    border-radius: 4px;
    font-size: 14px;
  }
  .alternatives {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .alt-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 12px;
    background: #fafafa;
    border: 1px solid #f0f0f0;
    border-radius: 4px;
  }
  .alt-meta {
    min-inline-size: 0;
    flex: 1 1 200px;
  }
  .alt-title {
    font-weight: 600;
    font-size: 14px;
    overflow-wrap: break-word;
  }
  .alt-reason {
    font-size: 13px;
    color: #6b6b6b;
  }
  .alt-btn {
    background: #1a1a1a;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 6px 12px;
    font-size: 13px;
    font-weight: 500;
    min-block-size: 32px;
  }
  .newer-text-note {
    padding: 10px 12px;
    background: #f3e9d9;
    color: #6e4a23;
    border-radius: 4px;
    font-size: 14px;
  }
  .signed-by {
    font-size: 13px;
    color: #6b6b6b;
    margin: 0;
  }
  details.statement-details {
    border-top: 1px solid #f2f2f2;
    padding-top: 10px;
    font-size: 13px;
  }
  details.statement-details > summary {
    cursor: pointer;
    font-weight: 500;
    color: #3a3a3a;
    min-block-size: 28px;
    display: list-item;
  }
  .details-body {
    margin-top: 10px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .statement-text {
    margin: 0;
    font-size: 14px;
    line-height: 1.55;
    text-wrap: pretty;
  }
  .meta-dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    margin: 0;
    font-size: 13px;
  }
  .meta-dl dt {
    color: #6b6b6b;
  }
  .meta-dl dd {
    margin: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    overflow-wrap: anywhere;
  }
  /* Links, then quiet actions in the manner of a chat message's: an icon and a word, no border, a light
     background only on hover. */
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    align-items: center;
    border-top: 1px solid #f2f2f2;
    padding-top: 10px;
    font-size: 13px;
  }
  .actions a {
    color: #3a3a3a;
    text-underline-offset: 3px;
    text-decoration-color: #c8c8c8;
  }
  .actions a:hover {
    color: #1a1a1a;
    text-decoration-color: currentColor;
  }
  .action-group {
    display: inline-flex;
    gap: 2px;
    margin-inline-start: -8px;
  }
  /* Beside the links the actions sit at the right; on a narrow card they take their own line, at the left. */
  .actions a ~ .action-group {
    margin-inline-start: auto;
  }
  @container (max-width: 480px) {
    .actions a ~ .action-group {
      flex-basis: 100%;
      margin-inline-start: -8px;
    }
  }
  .action-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 13px;
    font-weight: 500;
    color: #3a3a3a;
    min-block-size: 28px;
    min-inline-size: 28px;
  }
  .action-btn .icon {
    color: #6b6b6b;
  }
  .action-btn:hover {
    background: #f2f2f2;
    color: #1a1a1a;
  }
  .copy-fallback {
    width: 100%;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    min-block-size: 80px;
    padding: 8px;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
  }
  .reader-link-row {
    padding: 8px 12px;
    background: #fafafa;
    border: 1px solid #f0f0f0;
    border-radius: 4px;
    font-size: 14px;
  }

  @container (max-width: 480px) {
    .published-block {
      font-size: 16px;
    }
    .band {
      padding: 10px 12px;
    }
    .header-block {
      padding: 12px 12px 0;
    }
    .body {
      padding: 12px;
    }
    .meta-dl {
      grid-template-columns: 1fr;
    }
  }

  @media (forced-colors: active) {
    .card,
    .band {
      border-color: CanvasText;
    }
    .band {
      border-block-end: 1px solid CanvasText;
    }
    .published-block {
      border-inline-start-color: CanvasText;
    }
  }

  @media print {
    .card {
      break-inside: avoid;
    }
    .card[data-collapsed="true"] .body {
      display: flex;
    }
    .collapse-toggle,
    .show-all-btn,
    .action-btn,
    .alt-btn {
      display: none !important;
    }
  }

  @media (pointer: coarse) {
    .collapse-toggle,
    .action-btn,
    .alt-btn,
    .show-all-btn,
    .actions a,
    details.statement-details > summary {
      min-block-size: 44px;
      min-inline-size: 44px;
      display: inline-flex;
      align-items: center;
    }
    details.statement-details > summary {
      display: list-item;
      padding-block: 10px;
    }
  }
}
`;

let sharedSheet = null;
function getSharedSheet() {
  if (!sharedSheet && typeof CSSStyleSheet === "function") {
    sharedSheet = new CSSStyleSheet();
    sharedSheet.replaceSync(CARD_CSS);
  }
  return sharedSheet;
}

let cardTemplate = null;
function getCardTemplate() {
  if (!cardTemplate && typeof document !== "undefined") {
    cardTemplate = document.createElement("template");
    // Static structural shell only — zero untrusted data ever enters innerHTML.
    cardTemplate.innerHTML = `
      <article class="card" part="card">
        <div class="band" data-ref="band">
          <div class="band-left">
            <span data-ref="band-icon"></span>
            <span class="band-text" data-ref="band-text"></span>
          </div>
          <button type="button" class="collapse-toggle" data-ref="collapse-btn" hidden></button>
        </div>
        <div class="header-block">
          <p class="ai-suggested" data-ref="ai-suggested" hidden>Section suggested by AI</p>
          <h3 class="title" data-ref="title"></h3>
        </div>
        <div class="body" data-ref="body">
          <ul class="lines" data-ref="lines" hidden></ul>
          <div class="diff" data-ref="diff" hidden>
            <p class="section-label" data-ref="diff-label">Where your quote differs from the section</p>
            <p class="diff-text" data-ref="diff-text" dir="auto"></p>
          </div>
          <div data-ref="grey-area" hidden></div>
          <div data-ref="newer-text" class="newer-text-note" hidden></div>
          <div data-ref="published-section" hidden>
            <p class="section-label" data-ref="published-label">Published text</p>
            <div class="published-block" data-ref="published-block" dir="auto"></div>
          </div>
          <div data-ref="quote-section" hidden>
            <p class="section-label" data-ref="quote-label">Quote sent</p>
            <blockquote class="quote-sent" data-ref="quote-sent"></blockquote>
          </div>
          <div data-ref="reading-section" hidden>
            <p class="section-label">AI's reading (not checked by Open Alvary)</p>
            <p class="ai-reading" data-ref="ai-reading"></p>
          </div>
          <div data-ref="reader-links" hidden></div>
          <div class="actions" data-ref="actions"></div>
          <details class="statement-details" data-ref="details">
            <summary>Verification details · verified in this browser</summary>
            <div class="details-body">
              <p class="signed-by">Signed by Open Alvary · verified in this browser</p>
              <p class="statement-text" data-ref="statement"></p>
              <dl class="meta-dl" data-ref="meta-dl"></dl>
              <div>
                <button type="button" class="action-btn" data-ref="copy-receipt-btn">Copy JSON</button>
              </div>
              <textarea class="copy-fallback" data-ref="copy-fallback" readonly hidden aria-label="Proof JSON to copy"></textarea>
            </div>
          </details>
        </div>
      </article>
    `;
  }
  return cardTemplate;
}

const BaseElement = typeof HTMLElement !== "undefined" ? HTMLElement : class {};

export class OaReceiptElement extends BaseElement {
  constructor() {
    super();
    this._receipt = null;
    this._claim = null;
    this._verification = null;
    this._source = "form"; // "form" | "agent" | "yours" (the visitor's quote, on the AI's section) | "recheck"
    this._collapsed = false;
    this._wasDetailsOpenBeforePrint = false;
    this._readerLinks = [];

    if (typeof this.attachShadow === "function") {
      this._internals = typeof this.attachInternals === "function" ? this.attachInternals() : null;
      const root = this.attachShadow({ mode: "open" });
      const sheet = getSharedSheet();
      if (sheet && "adoptedStyleSheets" in root) {
        root.adoptedStyleSheets = [sheet];
      }
      const tpl = getCardTemplate();
      if (tpl) {
        root.appendChild(tpl.content.cloneNode(true));
      }
      this._els = {};
      for (const el of root.querySelectorAll("[data-ref]")) {
        this._els[el.getAttribute("data-ref")] = el;
      }
      this._els["collapse-btn"].addEventListener("click", () => {
        // The visitor's own click always works; the focus guard below is for automatic closing only.
        this._collapsed = !this._collapsed;
        this._updateCollapseUi();
      });
      this._els["details"].addEventListener("toggle", () => {
        this._syncStates();
      });
      this._els["copy-receipt-btn"].addEventListener("click", () => {
        const payload = JSON.stringify({ receipt: this._receipt, claim: this._claim }, null, 2);
        this._copyWithFeedback(this._els["copy-receipt-btn"], payload, "Copy JSON");
      });
    }

    this._onBeforePrint = () => {
      const details = this._els?.details;
      if (!details) return;
      this._wasDetailsOpenBeforePrint = details.open;
      details.open = true;
    };
    this._onAfterPrint = () => {
      const details = this._els?.details;
      if (!details) return;
      details.open = this._wasDetailsOpenBeforePrint;
    };
  }

  connectedCallback() {
    if (typeof window !== "undefined") {
      window.addEventListener("beforeprint", this._onBeforePrint);
      window.addEventListener("afterprint", this._onAfterPrint);
    }
    this._syncStates();
  }

  disconnectedCallback() {
    if (typeof window !== "undefined") {
      window.removeEventListener("beforeprint", this._onBeforePrint);
      window.removeEventListener("afterprint", this._onAfterPrint);
    }
  }

  get receipt() {
    return this._receipt;
  }

  get claim() {
    return this._claim;
  }

  get verification() {
    return this._verification;
  }

  get source() {
    return this._source;
  }

  // The plain-text summary the card's "Copy summary" button copies, for the receipt panel.
  summaryText() {
    return this._formatAsPlainText();
  }

  // Saves the receipt and its claim as a proof file, as the card's "Save proof" button does.
  saveProof() {
    const payload = JSON.stringify({ receipt: this._receipt, claim: this._claim }, null, 2);
    const blob = new Blob([payload], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${this._receipt?.receipt_id || "open-alvary-receipt"}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  get receiptId() {
    return this._receipt?.receipt_id || null;
  }

  get sourceId() {
    return this._receipt?.part?.source_id || null;
  }

  get collapsed() {
    return this._collapsed;
  }

  set collapsed(val) {
    const next = Boolean(val);
    // Automatic closing (a newer card arriving) never closes a card that holds focus
    if (next && typeof this.matches === "function" && this.matches(":focus-within")) {
      return;
    }
    this._collapsed = next;
    this._updateCollapseUi();
  }

  setData({ receipt, claim = null, verification = null, source = "form", collapsible = false, collapsed = false }) {
    if (!verification || verification.ok !== true) {
      throw new Error("<oa-receipt> only renders verified receipts.");
    }
    this._receipt = receipt;
    this._claim = claim;
    this._verification = verification;
    this._source = source;
    this._collapsible = Boolean(collapsible);
    this._collapsed = Boolean(collapsed && collapsible);
    this._render();
    this._syncStates();
  }

  addReaderLink(sourceId, pinpoint) {
    if (!this._els) return;
    const container = this._els["reader-links"];
    const href = `/sources/${encodeURIComponent(sourceId)}${sectionHash(pinpoint)}`;
    const label = pinpoint ? `Open ${pinpoint} in the reader ↗` : "Open in the reader ↗";
    if (this._readerLinks.some((l) => l.href === href && l.label === label)) return;
    this._readerLinks.push({ href, label });
    container.hidden = false;
    const row = document.createElement("div");
    row.className = "reader-link-row";
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = label;
    const sr = document.createElement("span");
    sr.className = "sr-only";
    sr.textContent = " (opens in a new tab)";
    a.appendChild(sr);
    row.appendChild(a);
    container.appendChild(row);
    const card = this.shadowRoot?.querySelector(".card");
    if (card) card.classList.add("highlighted");
  }

  expandAndReveal(shouldScroll = false) {
    this._collapsed = false;
    this._updateCollapseUi();
    const card = this.shadowRoot?.querySelector(".card");
    if (card) card.classList.add("highlighted");
    if (shouldScroll && typeof this.scrollIntoView === "function") {
      const reduce = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      this.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "nearest" });
    }
  }

  _syncStates() {
    const states = this._internals?.states;
    if (!states) return;
    if (this._verification?.ok) {
      states.add("verified");
    } else {
      states.delete("verified");
    }
    if (!this._collapsed) {
      states.add("expanded");
    } else {
      states.delete("expanded");
    }
  }

  _updateCollapseUi() {
    if (!this._els) return;
    const card = this.shadowRoot.querySelector(".card");
    const btn = this._els["collapse-btn"];
    card.dataset.collapsed = this._collapsed ? "true" : "false";
    if (this._collapsible) {
      btn.hidden = false;
      btn.setAttribute("aria-expanded", this._collapsed ? "false" : "true");
      btn.textContent = this._collapsed ? "Show result" : "Hide result";
    } else {
      btn.hidden = true;
    }
    this._syncStates();
  }

  _render() {
    const r = this._receipt;
    const c = this._claim;
    const v = this._verification;
    if (!r || !this._els) return;

    const look = lookForStatus(r.status);
    const isGrey = GREY_STATUSES.has(r.status);
    const assessedDate = formatLongDate(r.claim?.assessed_at || r.issued_at);

    // Band
    const band = this._els.band;
    band.dataset.look = look;
    const iconSlot = this._els["band-icon"];
    iconSlot.replaceChildren(createSvgIcon(`band-${look}`, 18));
    this._els["band-text"].textContent = `${bandHeadlineForReceipt(r)} · Checked ${assessedDate}`;

    // Title
    this._els["ai-suggested"].hidden = this._source !== "agent" && this._source !== "yours";
    if (r.part) {
      this._els.title.textContent = `${r.part.title}, ${r.part.pinpoint}`;
    } else if (c?.citation) {
      this._els.title.textContent = c.citation;
    } else {
      this._els.title.textContent = greyHeadlineForStatus(r.status);
    }

    // Words & Record lines (hidden on grey cards)
    const linesEl = this._els.lines;
    linesEl.replaceChildren();
    if (!isGrey) {
      linesEl.hidden = false;
      const wl = wordsLineForReceipt(r, c);
      linesEl.appendChild(this._makeFindingLi(wl.icon, wl.text, false));
      for (const rl of recordLinesForReceipt(r)) {
        linesEl.appendChild(this._makeFindingLi(rl.icon, rl.text, Boolean(rl.secondary)));
      }
    } else {
      linesEl.hidden = true;
    }

    // Where a quote that was not found departs from the section's opening words
    const diffBox = this._els.diff;
    const diffText = this._els["diff-text"];
    const diffLabel = this._els["diff-label"];
    diffText.replaceChildren();
    if (diffLabel) diffLabel.textContent = "Where your quote differs from the section";
    const canCompare = r.status === "wording_differs" && c?.quote && r.findings?.opening_text;
    const difference = canCompare ? wordDifference(c.quote, r.findings.opening_text) : null;
    const marksOnly = canCompare && !difference ? marksOnlyDifference(c.quote, r.findings.opening_text) : null;
    if (marksOnly) {
      // The words are all there: say so, and show how the section writes them.
      diffBox.hidden = false;
      if (diffLabel) diffLabel.textContent = "Your words are in the section, written differently";
      if (r.part?.language) diffText.setAttribute("lang", r.part.language);
      const note = document.createElement("span");
      note.className = "diff-note";
      note.textContent =
        "The check compares capital letters and punctuation exactly. The section writes these words: ";
      diffText.append(note, document.createTextNode(`\u201c${marksOnly}\u201d`));
    } else if (difference) {
      diffBox.hidden = false;
      if (r.part?.language) diffText.setAttribute("lang", r.part.language);
      const add = (node) => diffText.appendChild(node);
      const text = (t) => document.createTextNode(t);
      const marked = (tag, label, words) => {
        const el = document.createElement(tag);
        const sr = document.createElement("span");
        sr.className = "sr-only";
        sr.textContent = label;
        el.append(sr, text(words));
        return el;
      };
      add(text((difference.leadingCut ? "…" : "") + (difference.before ? difference.before + " " : "")));
      if (difference.quoteWords) add(marked("del", "your quote says: ", difference.quoteWords));
      if (difference.quoteWords && difference.sectionWords) add(text(" "));
      if (difference.sectionWords) add(marked("ins", "the section says: ", difference.sectionWords));
      add(text((difference.after ? " " + difference.after : "") + (difference.trailingCut ? "…" : "")));
    } else {
      diffBox.hidden = true;
    }

    // Grey-card guidance, nearest, alternatives
    const greyArea = this._els["grey-area"];
    greyArea.replaceChildren();
    if (isGrey) {
      greyArea.hidden = false;
      if (r.status === "not_in_catalogue") {
        const p = document.createElement("p");
        p.className = "grey-note";
        p.textContent = "Open Alvary cannot check this law. That does not mean the law does not exist. ";
        const cov = document.createElement("a");
        cov.href = "/coverage";
        cov.textContent = "See Coverage";
        p.appendChild(cov);
        greyArea.appendChild(p);
      } else if (r.status === "no_part_named") {
        const p = document.createElement("p");
        p.className = "grey-note";
        p.textContent = "Name a specific section (for example, s. 1(1)) to check quoted words against it.";
        greyArea.appendChild(p);
      } else if (r.status === "text_not_available") {
        const p = document.createElement("p");
        p.className = "grey-note";
        p.textContent = "Open Alvary does not currently publish the full text for this record.";
        greyArea.appendChild(p);
      } else if (r.status === "not_in_that_language") {
        const p = document.createElement("p");
        p.className = "grey-note";
        p.textContent = "No published text is available in the language requested.";
        greyArea.appendChild(p);
      } else if (r.status === "part_not_found") {
        const p = document.createElement("p");
        p.className = "grey-note";
        p.textContent = "That section number was not found in Open Alvary's published text.";
        greyArea.appendChild(p);
        if (r.nearest) {
          const box = document.createElement("div");
          box.className = "nearest-box";
          const words = document.createElement("span");
          words.textContent = `Nearest enclosing section: ${r.nearest.title}, ${r.nearest.pinpoint}`;
          box.appendChild(words);
          if (c?.quote && (r.nearest.anchor_id || r.nearest.pinpoint)) {
            // The same words, checked against the section that holds the one named.
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "alt-btn";
            btn.textContent = `Check ${r.nearest.pinpoint || "this section"} instead`;
            btn.addEventListener("click", () => {
              this.dispatchEvent(
                new CustomEvent("oa-check-alternative", {
                  bubbles: true,
                  composed: true,
                  detail: { alternative: r.nearest, receipt: r, claim: c },
                }),
              );
            });
            box.appendChild(btn);
          }
          greyArea.appendChild(box);
        }
      } else if (r.status === "ambiguous") {
        const p = document.createElement("p");
        p.className = "grey-note";
        p.textContent = "Choose one of the matching entries below to check it:";
        greyArea.appendChild(p);
        if (Array.isArray(r.alternatives) && r.alternatives.length > 0) {
          const ul = document.createElement("ul");
          ul.className = "alternatives";
          for (const alt of r.alternatives) {
            const li = document.createElement("li");
            li.className = "alt-row";
            const meta = document.createElement("div");
            meta.className = "alt-meta";
            const t = document.createElement("div");
            t.className = "alt-title";
            t.textContent = alt.pinpoint ? `${alt.title}, ${alt.pinpoint}` : alt.title;
            const reason = document.createElement("div");
            reason.className = "alt-reason";
            reason.textContent = alt.reason || "";
            meta.append(t, reason);

            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "alt-btn";
            btn.textContent = "Check this one";
            btn.addEventListener("click", () => {
              this.dispatchEvent(
                new CustomEvent("oa-check-alternative", {
                  bubbles: true,
                  composed: true,
                  detail: { alternative: alt, receipt: r, claim: c },
                }),
              );
            });
            li.append(meta, btn);
            ul.appendChild(li);
          }
          greyArea.appendChild(ul);
        }
      }
    } else {
      greyArea.hidden = true;
    }

    // Newer text note
    const newerEl = this._els["newer-text"];
    newerEl.replaceChildren();
    if (r.findings?.newer_text) {
      newerEl.hidden = false;
      newerEl.appendChild(document.createTextNode("A newer published text of this law exists: "));
      const a = document.createElement("a");
      const newerHref = safeLinkHref(r.findings.newer_text.record_url);
      if (newerHref) {
        a.href = newerHref;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = "Open newer text ↗";
        const sr = document.createElement("span");
        sr.className = "sr-only";
        sr.textContent = " (opens in a new tab)";
        a.appendChild(sr);
        newerEl.appendChild(a);
      } else {
        newerEl.appendChild(document.createTextNode("see Open Alvary's record."));
      }
    } else {
      newerEl.hidden = true;
    }

    // Published text (matched_text OR opening_text on wording_differs)
    const pubSec = this._els["published-section"];
    const pubBlock = this._els["published-block"];
    pubBlock.replaceChildren();
    if (r.part?.language) {
      pubBlock.setAttribute("lang", r.part.language);
    } else {
      pubBlock.removeAttribute("lang");
    }

    if (r.findings?.matched_text) {
      pubSec.hidden = false;
      this._els["published-label"].textContent = "Published text";
      this._renderPublishedPassage(
        pubBlock,
        r.findings.context_before || "",
        r.findings.matched_text,
        r.findings.context_after || "",
        Array.isArray(r.findings.omitted) ? r.findings.omitted : [],
      );
    } else if (r.status === "wording_differs" && r.findings?.opening_text) {
      pubSec.hidden = false;
      this._els["published-label"].textContent = "The section says";
      pubBlock.textContent = r.findings.opening_text;
    } else {
      pubSec.hidden = true;
    }

    // Quote sent
    const quoteSec = this._els["quote-section"];
    if (c?.quote) {
      quoteSec.hidden = false;
      this._els["quote-label"].textContent =
        this._source === "agent" ? "Quote sent (the AI's)" : "Quote sent (yours)";
      this._els["quote-sent"].textContent = c.quote;
    } else {
      quoteSec.hidden = true;
    }

    // AI's reading (only when agent-made and reading present)
    const readingSec = this._els["reading-section"];
    if (this._source === "agent" && c?.reading) {
      readingSec.hidden = false;
      this._els["ai-reading"].textContent = c.reading;
    } else {
      readingSec.hidden = true;
    }

    // Statement & details
    this._els.statement.textContent = r.statement || "";
    const dl = this._els["meta-dl"];
    dl.replaceChildren();
    const ageLabel = v.ageDays === 0 ? "Today" : v.ageDays === 1 ? "1 day old" : `${v.ageDays} days old`;
    const metaRows = [
      ["Receipt ID", r.receipt_id],
      ["Status", r.status],
      ["Issued", r.issued_at],
      ["Age", ageLabel],
      ["Dataset", r.dataset],
      ["Signing key", `${r.key?.kid} (${r.key?.alg})`],
      ["Claim digest", r.claim?.digest],
    ];
    for (const [label, val] of metaRows) {
      if (val == null) continue;
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = String(val);
      dl.append(dt, dd);
    }

    // Actions bar
    const actionsEl = this._els.actions;
    actionsEl.replaceChildren();
    const recordHref = sectionHref(r.part?.record_url, r.part?.pinpoint);
    if (recordHref) {
      actionsEl.appendChild(this._makeNewTabLink(recordHref, "Open the section ↗"));
    }
    const officialHref = safeLinkHref(r.part?.official_url);
    if (officialHref) {
      actionsEl.appendChild(this._makeNewTabLink(officialHref, "Official source ↗"));
    }

    const actionButton = (icon, text) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "action-btn";
      const label = document.createElement("span");
      label.className = "action-label";
      label.textContent = text;
      button.append(createSvgIcon(icon, 16), label);
      return button;
    };
    const group = document.createElement("span");
    group.className = "action-group";

    const copyTextBtn = actionButton("copy", "Copy summary");
    copyTextBtn.addEventListener("click", () => {
      const text = this._formatAsPlainText();
      this._copyWithFeedback(copyTextBtn, text, "Copy summary");
    });
    group.appendChild(copyTextBtn);

    const downloadBtn = actionButton("save", "Save proof");
    downloadBtn.addEventListener("click", () => this.saveProof());
    group.appendChild(downloadBtn);
    actionsEl.appendChild(group);

    this._updateCollapseUi();
  }

  _makeFindingLi(icon, text, secondary) {
    const li = document.createElement("li");
    li.className = secondary ? "finding-line secondary" : "finding-line";
    if (!secondary && icon && icon !== "none") {
      li.appendChild(createSvgIcon(icon, 16));
    }
    const span = document.createElement("span");
    span.textContent = text;
    li.appendChild(span);
    return li;
  }

  _makeNewTabLink(href, label) {
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = label;
    const sr = document.createElement("span");
    sr.className = "sr-only";
    sr.textContent = " (opens in a new tab)";
    a.appendChild(sr);
    return a;
  }

  _renderPublishedPassage(container, before, matched, after, omitted) {
    const fullLen = before.length + matched.length + after.length;
    const renderContent = (truncated) => {
      container.replaceChildren();
      const span = document.createElement("span");
      if (before) {
        span.appendChild(document.createTextNode(truncated && before.length > 80 ? "…" + before.slice(-80) : before));
      }

      const mark = document.createElement("mark");
      const startSr = document.createElement("span");
      startSr.className = "sr-only";
      startSr.textContent = "Start of matched quote: ";
      mark.appendChild(startSr);

      const shownMatched = truncated && matched.length > 420 ? matched.slice(0, 420) + "…" : matched;
      if (!truncated && omitted.length > 0) {
        let cursor = 0;
        const sorted = [...omitted].sort((a, b) => a.start - b.start);
        for (const seg of sorted) {
          if (seg.start > cursor) {
            mark.appendChild(document.createTextNode(matched.slice(cursor, seg.start)));
          }
          const om = document.createElement("span");
          om.className = "omitted";
          const omSr = document.createElement("span");
          omSr.className = "sr-only";
          omSr.textContent = " (left out of the quote: ";
          om.appendChild(omSr);
          om.appendChild(document.createTextNode(matched.slice(seg.start, seg.end)));
          const omEndSr = document.createElement("span");
          omEndSr.className = "sr-only";
          omEndSr.textContent = ") ";
          om.appendChild(omEndSr);
          mark.appendChild(om);
          cursor = Math.max(cursor, seg.end);
        }
        if (cursor < matched.length) {
          mark.appendChild(document.createTextNode(matched.slice(cursor)));
        }
      } else {
        mark.appendChild(document.createTextNode(shownMatched));
      }

      const endSr = document.createElement("span");
      endSr.className = "sr-only";
      endSr.textContent = " End of matched quote.";
      mark.appendChild(endSr);
      span.appendChild(mark);

      if (after && !truncated) {
        span.appendChild(document.createTextNode(after));
      } else if (after && truncated) {
        span.appendChild(document.createTextNode(after.slice(0, 80) + (after.length > 80 ? "…" : "")));
      }

      container.appendChild(span);

      if (fullLen > 600) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "show-all-btn";
        btn.textContent = truncated ? "Show all" : "Show less";
        btn.addEventListener("click", () => {
          renderContent(!truncated);
          container.querySelector(".show-all-btn")?.focus();
        });
        container.appendChild(document.createElement("br"));
        container.appendChild(btn);
      }
    };
    // A passage with words left out opens whole, so the left-out words are always marked.
    renderContent(fullLen > 600 && omitted.length === 0);
  }

  _formatAsPlainText() {
    const r = this._receipt;
    const c = this._claim;
    const title = r.part ? `${r.part.title}, ${r.part.pinpoint}` : (c?.citation || r.status);
    const lines = [
      title,
      `Checked: ${formatLongDate(r.claim?.assessed_at || r.issued_at)}`,
      `Result: ${[bandHeadlineForReceipt(r), ...recordLinesForReceipt(r).map((l) => l.text)]
        .map((t) => String(t).trim().replace(/[.\s]+$/, ""))
        .filter(Boolean)
        .join(". ")}.`,
    ];
    if (c?.quote) lines.push(`Quote: "${c.quote}"`);
    if (r.statement) lines.push(r.statement);
    lines.push(`Receipt ID: ${r.receipt_id}`);
    lines.push("Verify at https://open.alvary.ai/agents/#recheck");
    return lines.join("\n");
  }

  async _copyWithFeedback(button, text, originalLabel) {
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(text);
        // A button with an icon keeps it: only its words change.
        const label = button.querySelector(".action-label") || button;
        label.textContent = "Copied";
        this.dispatchEvent(
          new CustomEvent("oa-receipt-announce", { bubbles: true, composed: true, detail: { text: "Copied." } }),
        );
        setTimeout(() => {
          label.textContent = originalLabel;
        }, 2000);
        return;
      }
    } catch {
      // Fall through to selectable fallback
    }
    const fallback = this._els["copy-fallback"];
    const details = this._els.details;
    if (details) details.open = true;
    fallback.hidden = false;
    fallback.value = text;
    fallback.focus();
    fallback.select();
  }
}

if (typeof customElements !== "undefined" && !customElements.get("oa-receipt")) {
  customElements.define("oa-receipt", OaReceiptElement);
}
