// The shapes a reviewed page can take: units (heading, paragraph, list item) with their words and
// margin notes, section-number chips, and tables of text cells. Each node reads back from its own
// markup, so text copied and pasted within the page keeps its units, numbers and formatting.

import { Schema } from "prosemirror-model";

const DEPTH = [
  [/^\d{1,3}[A-Z]?\./, 0],
  [/^\(\d{1,3}[A-Z]?\)/, 1],
  [/^\([ivxl]{2,6}\)/, 3],
  [/^\([a-z]{1,2}\)/, 2],
];

export function derivedDepth(lead) {
  const text = (lead || "").trimStart();
  for (const [pattern, depth] of DEPTH) if (pattern.test(text)) return depth;
  return null;
}

export function unitLead(node) {
  const body = node.firstChild;
  let marker = "";
  body.forEach((child) => { if (child.type.name === "marker" && !marker) marker = child.attrs.value; });
  return marker || body.textContent;
}

export const KINDS = ["heading", "paragraph", "list_item"];
export const ALIGNMENTS = ["left", "center", "right", "justify"];
// The stored table grid allows at most 100 rows of 100 cells (workbench.content.validate_units).
const MAX_SPAN = 100;

// A heading's rank as printed, 1 (the title) to 6; null for a heading no one ranked, shown as
// rank 3. Only headings have one. It is not the indent level, and not in the published text.
export const isRank = (value) => Number.isInteger(value) && value >= 1 && value <= 6;
export const rankOf = (kind, value) => (kind === "heading" && isRank(value) ? value : null);

function unitAttrs(dom) {
  const level = Number(dom.dataset.level);
  const kind = KINDS.includes(dom.dataset.kind) ? dom.dataset.kind : "paragraph";
  return {
    kind,
    level: dom.dataset.level && Number.isInteger(level) && level >= 0 && level <= 3 ? level : null,
    align: ALIGNMENTS.includes(dom.dataset.align) ? dom.dataset.align : null,
    rank: dom.dataset.rank ? rankOf(kind, Number(dom.dataset.rank)) : null,
  };
}

function span(value) {
  return Math.min(Math.max(parseInt(value, 10) || 1, 1), MAX_SPAN);
}

// A merged cell, as printed, keeps its extent in rows and columns; the cells it covers are not
// there, as in an HTML table. Cells hold text only.
function cellSpec(tag, role) {
  return {
    tableRole: role,
    content: "text*",
    marks: "",
    isolating: true,
    attrs: { colspan: { default: 1 }, rowspan: { default: 1 }, colwidth: { default: null } },
    parseDOM: [{ tag, getAttrs: (dom) => ({ colspan: span(dom.getAttribute("colspan")), rowspan: span(dom.getAttribute("rowspan")) }) }],
    toDOM(node) {
      const { colspan, rowspan } = node.attrs;
      const attrs = {};
      if (colspan > 1) attrs.colspan = colspan;
      if (rowspan > 1) attrs.rowspan = rowspan;
      if (colspan > 1 || rowspan > 1) {
        attrs.title = `Merged cell as printed: ${[rowspan > 1 ? `${rowspan} rows` : "", colspan > 1 ? `${colspan} columns` : ""].filter(Boolean).join(", ")}`;
      }
      return [tag, attrs, 0];
    },
  };
}

export const schema = new Schema({
  nodes: {
    doc: { content: "block+" },
    unit: {
      group: "block",
      content: "body note*",
      attrs: { kind: { default: "paragraph" }, level: { default: null }, align: { default: null }, rank: { default: null } },
      parseDOM: [{ tag: "div.u", getAttrs: unitAttrs }],
      toDOM(node) {
        const { kind, level, align } = node.attrs;
        const rank = rankOf(kind, node.attrs.rank);
        const shown = level ?? derivedDepth(unitLead(node));
        const cls = ["u", kind, rank ? `h${rank}` : "", shown === null ? "" : `lvl${shown}`, align ? `align-${align}` : ""];
        const attrs = { class: cls.join(" ").replace(/\s+/g, " ").trim(), "data-kind": kind };
        if (rank) attrs["data-rank"] = String(rank);
        if (level !== null) attrs["data-level"] = String(level);
        if (align) attrs["data-align"] = align;
        return ["div", attrs, 0];
      },
    },
    body: { content: "inline*", parseDOM: [{ tag: "div.body" }], toDOM: () => ["div", { class: "body" }, 0] },
    note: { content: "text*", marks: "", isolating: true, parseDOM: [{ tag: "aside.note" }], toDOM: () => ["aside", { class: "note" }, 0] },
    marker: {
      inline: true,
      group: "inline",
      atom: true,
      selectable: true,
      attrs: { value: {}, source: { default: null } },
      parseDOM: [{
        tag: "span.marker",
        getAttrs(dom) {
          const value = dom.textContent.trim();
          if (!value) return false;
          let source = null;
          try { source = JSON.parse(dom.dataset.source || "null"); } catch { source = null; }
          return { value: value.slice(0, 40), source };
        },
      }],
      toDOM(node) {
        const source = node.attrs.source ? `Number restored from ${[].concat(node.attrs.source).join(", ")}` : "Section or paragraph number";
        const attrs = { class: "marker", title: `${source}. Double-click to change.` };
        if (node.attrs.source) attrs["data-source"] = JSON.stringify(node.attrs.source);
        return ["span", attrs, node.attrs.value];
      },
    },
    table_unit: {
      group: "block",
      content: "table_caption table",
      attrs: { header_rows: { default: 0 } },
      parseDOM: [{ tag: "div.tbl", getAttrs: (dom) => ({ header_rows: Math.max(0, parseInt(dom.dataset.headerRows, 10) || 0) }) }],
      toDOM: (node) => ["div", { class: "tbl", "data-header-rows": String(node.attrs.header_rows) }, 0],
    },
    table_caption: { content: "text*", marks: "", isolating: true, parseDOM: [{ tag: "div.caption" }], toDOM: () => ["div", { class: "caption" }, 0] },
    table: { tableRole: "table", content: "table_row+", isolating: true, parseDOM: [{ tag: "table" }], toDOM: () => ["table", ["tbody", 0]] },
    table_row: { tableRole: "row", content: "(table_cell | table_header)*", parseDOM: [{ tag: "tr" }], toDOM: () => ["tr", 0] },
    table_cell: cellSpec("td", "cell"),
    table_header: cellSpec("th", "header_cell"),
    text: { group: "inline" },
  },
  // Character formatting that can carry meaning in legal text; no fonts, sizes or colours.
  marks: {
    strong: {
      parseDOM: [
        { tag: "strong" },
        { tag: "b", getAttrs: (dom) => !/font-weight\s*:\s*(normal|[1-4]00)/i.test(dom.getAttribute("style") || "") && null },
        { style: "font-weight", getAttrs: (value) => /^(bold|bolder|[6-9]00)$/i.test(value.trim()) && null },
      ],
      toDOM: () => ["strong", 0],
    },
    em: { parseDOM: [{ tag: "em" }, { tag: "i" }, { style: "font-style=italic" }], toDOM: () => ["em", 0] },
    underline: { parseDOM: [{ tag: "u" }, { style: "text-decoration", getAttrs: (value) => /underline/i.test(value) && null }], toDOM: () => ["u", 0] },
    sup: { excludes: "sup sub", parseDOM: [{ tag: "sup" }, { style: "vertical-align=super" }], toDOM: () => ["sup", 0] },
    sub: { excludes: "sup sub", parseDOM: [{ tag: "sub" }, { style: "vertical-align=sub" }], toDOM: () => ["sub", 0] },
  },
});

// Stored names of the formatting marks, as the server and the exports know them.
export const MARK_NAMES = { strong: "bold", em: "italic", underline: "underline", sup: "superscript", sub: "subscript" };
export const MARK_TYPES = Object.fromEntries(Object.entries(MARK_NAMES).map(([type, name]) => [name, type]));
