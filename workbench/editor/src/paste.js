// Text pasted from elsewhere (Word, a web page, a PDF) becomes units the corpus can hold: each
// paragraph, heading or list item a unit of its own, tables kept as tables of text, and of the
// formatting only bold, italic, underline, superscript and subscript. Fonts, sizes, colours,
// links, images and alignment are dropped. The result is in the stored form (as the server keeps
// units), so the editor builds it the same way as a saved page.

const BLOCKS = new Set([
  "address", "article", "aside", "blockquote", "center", "dd", "details", "dialog", "div", "dl", "dt",
  "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
  "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "ul",
]);
const SKIPPED = new Set(["head", "script", "style", "template", "title", "meta", "link", "noscript", "img", "svg", "object", "iframe", "video", "audio", "canvas", "button", "input", "select", "textarea"]);
const HEADINGS = new Set(["h1", "h2", "h3", "h4", "h5", "h6"]);

function styles(element) {
  const out = {};
  for (const part of (element.getAttribute("style") || "").split(";")) {
    const at = part.indexOf(":");
    if (at > 0) out[part.slice(0, at).trim().toLowerCase()] = part.slice(at + 1).trim().toLowerCase();
  }
  return out;
}

const hidden = (element, style) => style.display === "none" || style["mso-hide"] === "all" || element.hidden;

// The formatting an element adds to (or takes from) what it is inside.
function marksOf(element, inherited) {
  const marks = new Set(inherited);
  const tag = element.localName;
  const style = styles(element);
  if (tag === "b" || tag === "strong") marks.add("bold");
  if (tag === "i" || tag === "em") marks.add("italic");
  if (tag === "u" || tag === "ins") marks.add("underline");
  if (tag === "sup") { marks.delete("subscript"); marks.add("superscript"); }
  if (tag === "sub") { marks.delete("superscript"); marks.add("subscript"); }
  const weight = style["font-weight"];
  if (weight) {
    if (/^(bold|bolder|[6-9]00)$/.test(weight)) marks.add("bold");
    else if (/^(normal|lighter|[1-4]00)$/.test(weight)) marks.delete("bold"); // Google Docs wraps everything in <b style="font-weight:normal">
  }
  if (style["font-style"]) style["font-style"] === "normal" ? marks.delete("italic") : /italic|oblique/.test(style["font-style"]) && marks.add("italic");
  if (/underline/.test(style["text-decoration"] || style["text-decoration-line"] || "")) marks.add("underline");
  if (/^super/.test(style["vertical-align"] || "")) { marks.delete("subscript"); marks.add("superscript"); }
  if (/^sub/.test(style["vertical-align"] || "")) { marks.delete("superscript"); marks.add("subscript"); }
  return marks;
}

// Runs of text with their formatting -> one unit: white space collapsed as a browser shows it.
function unitFromRuns(kind, runs) {
  let text = "";
  const marks = [];
  for (const run of runs) {
    let piece = run.text.replace(/[\s ]+/g, " ");
    if (!text || text.endsWith(" ")) piece = piece.replace(/^ /, "");
    if (!piece) continue;
    const start = text.length;
    text += piece;
    for (const type of run.marks) {
      const last = marks.findLast((m) => m.type === type && m.end === start);
      if (last) last.end = text.length;
      else marks.push({ type, start, end: text.length });
    }
  }
  text = text.replace(/ +$/, "");
  const kept = marks.map((m) => ({ ...m, end: Math.min(m.end, text.length) })).filter((m) => m.start < m.end && text.slice(m.start, m.end).trim());
  return kept.length ? { kind, text, marks: kept } : { kind, text };
}

// A cell's (or a caption's) words on one line, with their formatting: what the table, its row and
// the cell add to the formatting `inherited` from around the table.
function cellWords(cell, table, inherited) {
  const around = [];
  for (let node = cell.parentElement; node && node !== table; node = node.parentElement) around.unshift(node);
  const runs = [];
  const walk = (node, marks) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) { runs.push({ text: child.data, marks }); continue; }
      if (child.nodeType !== 1 || SKIPPED.has(child.localName) || hidden(child, styles(child))) continue;
      const apart = child.localName === "br" || BLOCKS.has(child.localName);
      if (apart) runs.push({ text: " ", marks: new Set() });
      walk(child, marksOf(child, marks));
      if (apart) runs.push({ text: " ", marks: new Set() });
    }
  };
  walk(cell, [table, ...around, cell].reduce((marks, element) => marksOf(element, marks), inherited));
  return unitFromRuns("paragraph", runs);
}

const span = (value) => Math.min(Math.max(parseInt(value, 10) || 1, 1), 100);

// An HTML table -> a stored table: a full grid with merges listed, the leading rows of header
// cells (or the table head) counted as header rows, and the formatting of each cell.
function tableFrom(element, inherited) {
  const rows = [...element.rows];
  const taken = new Set(), placed = new Map(), spans = [], cellMarks = {};
  rows.forEach((row, r) => {
    let c = 0;
    for (const cell of row.cells) {
      while (taken.has(`${r},${c}`)) c++;
      const rs = Math.min(span(cell.getAttribute("rowspan")), rows.length - r), cs = span(cell.getAttribute("colspan"));
      const words = cellWords(cell, element, inherited);
      placed.set(`${r},${c}`, words.text);
      if (words.marks) cellMarks[`${r},${c}`] = words.marks;
      for (let dr = 0; dr < rs; dr++) for (let dc = 0; dc < cs; dc++) taken.add(`${r + dr},${c + dc}`);
      if (rs > 1 || cs > 1) spans.push([r, c, rs, cs]);
      c += cs;
    }
  });
  const width = Math.max(1, ...[...taken].map((key) => Number(key.split(",")[1]) + 1));
  const grid = rows.map((_, r) => Array.from({ length: width }, (_, c) => placed.get(`${r},${c}`) || ""));
  let header = 0;
  while (header < rows.length && rows[header].cells.length && [...rows[header].cells].every((cell) => cell.localName === "th" || cell.parentElement.localName === "thead")) header++;
  const caption = element.caption ? cellWords(element.caption, element, inherited) : { text: "" };
  const unit = { kind: "table", caption: caption.text, header_rows: header, rows: grid.length ? grid : [[""]] };
  if (caption.marks) unit.caption_marks = caption.marks;
  if (spans.length) unit.spans = spans;
  if (Object.keys(cellMarks).length) unit.cell_marks = cellMarks;
  return unit;
}

export function unitsFromHTML(html) {
  const doc = new DOMParser().parseFromString(html, "text/html"); // inert: nothing runs or loads
  const units = [];
  let current = null;
  const flush = () => {
    if (current) {
      const unit = unitFromRuns(current.kind, current.runs);
      if (unit.text) units.push(unit);
    }
    current = null;
  };
  const walk = (node, marks, kind) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        (current ||= { kind, runs: [] }).runs.push({ text: child.data, marks });
        continue;
      }
      if (child.nodeType !== 1) continue;
      const tag = child.localName;
      const style = styles(child);
      if (SKIPPED.has(tag) || hidden(child, style)) continue;
      if (tag === "br") { flush(); continue; }
      if (tag === "table") {
        flush();
        // A one-cell table is a layout box, not a table: its words are read as paragraphs.
        if (child.rows.length === 1 && child.rows[0].cells.length === 1) walk(child.rows[0].cells[0], marks, kind);
        else if (child.rows.length) units.push(tableFrom(child, marks));
        flush();
        continue;
      }
      if (BLOCKS.has(tag)) {
        flush();
        walk(child, marksOf(child, marks), HEADINGS.has(tag) ? "heading" : tag === "li" ? "list_item" : kind);
        flush();
        continue;
      }
      walk(child, marksOf(child, marks), kind);
    }
  };
  walk(doc.body, new Set(), "paragraph");
  flush();
  return units;
}

// A line starts a new unit when it starts with a provision number, or the line before it ends a
// sentence or a clause and this one starts with a capital or a number; other line breaks are where
// the source wrapped a line (a PDF). A blank line always separates units.
const NUMBERED = /^(\d{1,3}[A-Z]?\.|\(\d{1,3}[A-Z]?\)|\([a-z]{1,2}\)|\([ivxl]{1,6}\))(\s|$)/;

function startsNew(previous, line) {
  if (NUMBERED.test(line)) return true;
  return /[.;:!?—]["'”’)]?$/.test(previous) && /^["'“‘(]?[\p{Lu}\d]/u.test(line);
}

// Plain text (as a PDF viewer copies it) -> units of `kind`, the kind of the unit pasted into; the
// units after the first follow the rule of Enter (after a heading comes a paragraph).
export function unitsFromText(value, kind = "paragraph") {
  const paragraphs = [];
  let current = null;
  for (const raw of String(value).replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.replace(/[  ]+/g, " ").trim();
    if (!line) { current = null; continue; }
    if (current && !startsNew(current.text, line)) current.text += current.text.endsWith("-") ? line : ` ${line}`;
    else paragraphs.push((current = { text: line }));
  }
  const next = kind === "heading" ? "paragraph" : kind;
  return paragraphs.map((p, i) => ({ kind: i ? next : kind, text: p.text }));
}
