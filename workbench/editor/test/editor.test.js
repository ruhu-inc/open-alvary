// The editor's commands and the units <-> document round trip, on editor states without a view.
// Run with `npm test` (node --test).

import { test } from "node:test";
import assert from "node:assert/strict";
import { Fragment, Slice } from "prosemirror-model";
import { EditorState, TextSelection, NodeSelection } from "prosemirror-state";
import { baseKeymap } from "prosemirror-commands";
import { CellSelection, handlePaste as tablePaste } from "prosemirror-tables";
import { schema, toDoc, fromDoc, commands, keyCommands, plugins, pasteUnits, pastedWords, unitNode, unitsFromText } from "../src/editor.js";
import { formatted } from "../src/schema.js";
import { findKey } from "../src/find.js";

const N = schema.nodes;

// --- helpers ---------------------------------------------------------------------------------------

function stateOf(units) {
  return EditorState.create({ doc: toDoc(units), plugins: plugins() });
}

function apply(state, command) {
  let next = state;
  const done = command(state, (tr) => { next = state.apply(tr); });
  return { state: next, done };
}

// A key as the editor handles it: the page's own keys, then the standard ones.
function press(state, key) {
  for (const command of [keyCommands[key], baseKeymap[key]]) {
    if (!command) continue;
    const result = apply(state, command);
    if (result.done) return result.state;
  }
  return state;
}

// Position of a unit's words (after its chip, if any) plus `offset`.
function inWords(doc, index, offset = 0) {
  let pos = 0;
  for (let i = 0; i < index; i++) pos += doc.child(i).nodeSize;
  const body = doc.child(index).firstChild;
  const chip = body.firstChild && body.firstChild.type === N.marker ? 1 : 0;
  return pos + 2 + chip + offset;
}

function select(state, from, to = from) {
  return state.apply(state.tr.setSelection(TextSelection.create(state.doc, from, to)));
}

// The slice ProseMirror's clipboard parser makes of paragraphs from outside the page (lines of
// plain text, HTML <p> blocks) pasted into a cell: each one in the nearest textblock of the
// context, which there is a cell. Each paragraph is a string or text nodes.
function parsedInCell(...paragraphs) {
  const cells = paragraphs.map((p) => N.table_cell.create(null, typeof p === "string" ? schema.text(p) : p));
  return new Slice(Fragment.from(cells), 0, 0);
}

// The slice every plugin's transformPasted makes of what the clipboard parser made.
function transformed(state, slice) {
  const view = { state };
  return state.plugins.reduce((s, plugin) => (plugin.props.transformPasted ? plugin.props.transformPasted.call(plugin, s, view, false) : s), slice);
}

// A paste as the view runs it once the clipboard is parsed: the slice transformed, then the first
// plugin whose handlePaste takes it (the table plugin's comes before the page's own).
function paste(state, slice, clipboard = {}) {
  const view = { state, dispatch(tr) { view.state = view.state.apply(tr); } };
  const event = { clipboardData: { getData: (type) => clipboard[type] || "" } };
  const pasted = transformed(state, slice);
  if (!state.plugins.some((plugin) => plugin.props.handlePaste && plugin.props.handlePaste.call(plugin, view, event, pasted))) {
    view.dispatch(view.state.tr.replaceSelection(pasted));
  }
  return view.state;
}

// Start of the text of the n-th textblock of a kind (a note, a cell, a caption), in document order.
function inBlock(doc, type, n = 0, offset = 0) {
  let found = null, seen = 0;
  doc.descendants((node, pos) => {
    if (found !== null) return false;
    if (node.type === type && seen++ === n) found = pos + 1 + offset;
    return found === null;
  });
  return found;
}

const TAX_TABLE = {
  kind: "table",
  caption: "",
  header_rows: 2,
  rows: [["Annual Taxable Income (RWF)", "", "Tax Rate"], ["From", "To", ""], ["0", "360,000", "0%"], ["360,001", "1,200,000", "20%"]],
  spans: [[0, 0, 1, 2], [0, 2, 2, 1]],
};

const PAGE = [
  { kind: "heading", text: "Article 3: Purpose of this Law", marks: [{ type: "italic", start: 0, end: 10 }] },
  { kind: "paragraph", text: "The Commission shall have powers to —", marker: "6.", marker_source: ["azure", "mineru"], notes: ["Powers of the Commission"] },
  { kind: "list_item", text: "oversee the implementation of this Act;", marker: "(a)", level: 2, align: "justify", marks: [{ type: "bold", start: 0, end: 7 }, { type: "superscript", start: 12, end: 26 }] },
  TAX_TABLE,
  { kind: "paragraph", text: "Small enterprises must pay a lumpsum tax." },
];

// --- units <-> document -----------------------------------------------------------------------------

test("stored units come back from the document unchanged", () => {
  assert.deepEqual(fromDoc(toDoc(PAGE)), PAGE);
});

// --- heading ranks ----------------------------------------------------------------------------------

const RANKED = [
  { kind: "heading", rank: 1, text: "LAW N° 020/2023 OF 31/03/2023 ON TAX PROCEDURES" },
  { kind: "heading", rank: 2, text: "CHAPTER ONE: GENERAL PROVISIONS", align: "left" },
  { kind: "heading", rank: 6, text: "Definitions", marks: [{ type: "italic", start: 0, end: 11 }] },
  { kind: "heading", text: "Purpose of this Law" },
  { kind: "paragraph", text: "This Law determines tax procedures." },
];

// The unit's DOM as the editor draws it, read back as copy and paste reads it.
function throughDOM(node) {
  const [, attrs] = N.unit.spec.toDOM(node);
  const dataset = Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5), value]));
  return { attrs, parsed: N.unit.spec.parseDOM[0].getAttrs({ dataset }) };
}

test("a heading's rank comes back from the document and from its markup; unranked headings stay unranked", () => {
  assert.deepEqual(fromDoc(toDoc(RANKED)), RANKED);
  assert.equal("rank" in fromDoc(toDoc([PAGE[0]]))[0], false);
  const doc = toDoc(RANKED);
  const { attrs, parsed } = throughDOM(doc.child(1));
  assert.equal(attrs.class, "u heading h2 align-left");
  assert.equal(attrs["data-rank"], "2");
  assert.equal(parsed.rank, 2);
  assert.equal(throughDOM(doc.child(3)).attrs["data-rank"], undefined);
  assert.equal(throughDOM(doc.child(3)).parsed.rank, null);
});

test("a rank on anything but a heading, or out of range, is not kept", () => {
  const units = fromDoc(toDoc([{ kind: "paragraph", text: "Words", rank: 2 }, { kind: "heading", text: "Title", rank: 7 }, { kind: "heading", text: "Title", rank: true }]));
  assert.deepEqual(units, [{ kind: "paragraph", text: "Words" }, { kind: "heading", text: "Title" }, { kind: "heading", text: "Title" }]);
  assert.equal(N.unit.spec.parseDOM[0].getAttrs({ dataset: { kind: "paragraph", rank: "2" } }).rank, null);
});

test("Heading 2 makes the units selected headings of rank 2; a heading made a paragraph loses its rank", () => {
  let state = stateOf(RANKED.slice(3));
  state = select(state, inWords(state.doc, 0, 2), inWords(state.doc, 1, 2));
  state = apply(state, commands.rank(2)).state;
  assert.deepEqual(fromDoc(state.doc).map((u) => [u.kind, u.rank]), [["heading", 2], ["heading", 2]]);
  state = apply(state, commands.kind("paragraph")).state;
  assert.deepEqual(fromDoc(state.doc), [{ kind: "paragraph", text: "Purpose of this Law" }, { kind: "paragraph", text: "This Law determines tax procedures." }]);
  assert.equal(apply(state, commands.rank(0)).done, false);
  assert.equal(apply(state, commands.rank(7)).done, false);
});

test("Ctrl/Cmd+Alt+1 makes a heading; pressed again it steps the rank down, 4 back to 1", () => {
  let state = stateOf([{ kind: "paragraph", text: "Purpose of this Law" }]);
  state = select(state, inWords(state.doc, 0, 3));
  const ranks = [];
  for (let i = 0; i < 6; i++) {
    state = press(state, "Mod-Alt-1");
    const [unit] = fromDoc(state.doc);
    assert.equal(unit.kind, "heading");
    ranks.push(unit.rank ?? null);
  }
  assert.deepEqual(ranks, [null, 1, 2, 3, 4, 1]);
  state = press(state, "Mod-Alt-2");
  assert.deepEqual(fromDoc(state.doc), [{ kind: "paragraph", text: "Purpose of this Law" }]);
});

test("Ctrl/Cmd+Alt+1 over headings and a paragraph makes them all headings and leaves the ranks", () => {
  let state = stateOf([RANKED[1], RANKED[4]]);
  state = select(state, inWords(state.doc, 0, 2), inWords(state.doc, 1, 2));
  state = press(state, "Mod-Alt-1");
  assert.deepEqual(fromDoc(state.doc).map((u) => [u.kind, u.rank]), [["heading", 2], ["heading", undefined]]);
  state = press(state, "Mod-Alt-1"); // all headings now: every one takes the rank after the first one's
  assert.deepEqual(fromDoc(state.doc).map((u) => u.rank), [3, 3]);
});

test("Enter in a ranked heading: the words after it are a paragraph; at its start, a heading of the same rank opens above", () => {
  let state = stateOf([RANKED[1]]);
  state = select(state, inWords(state.doc, 0, 12));
  state = press(state, "Enter");
  assert.deepEqual(fromDoc(state.doc), [{ ...RANKED[1], text: "CHAPTER ONE:" }, { kind: "paragraph", text: "GENERAL PROVISIONS" }]);
  state = select(state, inWords(state.doc, 0));
  state = press(state, "Enter");
  assert.deepEqual(fromDoc(state.doc)[0], { kind: "heading", rank: 2, text: "" });
});

test("a merged table keeps its grid, merges and header rows", () => {
  const doc = toDoc([TAX_TABLE]);
  const table = doc.child(0).child(1);
  assert.equal(table.child(0).childCount, 2); // the covered position is not a cell
  assert.equal(table.child(0).child(0).type, N.table_header);
  assert.deepEqual({ ...table.child(0).child(0).attrs }, { colspan: 2, rowspan: 1, colwidth: null });
  assert.equal(table.child(2).child(0).type, N.table_cell);
  assert.deepEqual(fromDoc(doc), [TAX_TABLE]);
});

test("a short row is filled out to the table's width", () => {
  const units = fromDoc(toDoc([{ kind: "table", caption: "", header_rows: 0, rows: [["a", "b"], ["c"]] }]));
  assert.deepEqual(units[0].rows, [["a", "b"], ["c", ""]]);
});

// Footnote markers, units and emphasis in a table, as legal tables print them.
const AREA_TABLE = {
  kind: "table",
  caption: "Table 1: Plot sizes",
  caption_marks: [{ type: "italic", start: 0, end: 7 }],
  header_rows: 1,
  rows: [["Zone", "Area (m2)", "Note"], ["R1", "450", "minimum1"], ["Total", "", ""]],
  spans: [[2, 0, 1, 3]],
  cell_marks: {
    "0,0": [{ type: "bold", start: 0, end: 4 }],
    "0,1": [{ type: "bold", start: 0, end: 4 }, { type: "superscript", start: 7, end: 8 }],
    "1,2": [{ type: "underline", start: 0, end: 7 }, { type: "superscript", start: 7, end: 8 }],
    "2,0": [{ type: "bold", start: 0, end: 5 }],
  },
};

test("a table's formatting, in its cells and caption, comes back from the document unchanged", () => {
  assert.deepEqual(fromDoc(toDoc([AREA_TABLE])), [AREA_TABLE]);
  const cell = toDoc([AREA_TABLE]).child(0).child(1).child(0).child(1); // a header cell
  assert.deepEqual(cell.content.content.map((n) => [n.text, n.marks.map((m) => m.type.name)]), [["Area", ["strong"]], [" (m", []], ["2", ["sup"]], [")", []]]);
});

test("a table without formatting comes back byte for byte; a covered position never holds marks", () => {
  assert.equal(JSON.stringify(fromDoc(toDoc([TAX_TABLE]))[0]), JSON.stringify(TAX_TABLE));
  const covered = { kind: "table", caption: "", header_rows: 0, rows: [["abc", ""]], spans: [[0, 0, 1, 2]], cell_marks: { "0,1": [{ type: "bold", start: 0, end: 3 }] } };
  const { cell_marks: _, ...unmarked } = covered;
  assert.deepEqual(fromDoc(toDoc([covered])), [unmarked]);
});

test("a merge that would hide words is shown apart, and said so", () => {
  const notes = [];
  const unit = { kind: "table", caption: "", header_rows: 0, rows: [["a", "hidden"], ["c", "d"]], spans: [[0, 0, 1, 2]] };
  const doc = toDoc([unit], notes);
  assert.equal(notes.length, 1);
  assert.deepEqual(fromDoc(doc)[0].rows, unit.rows);
  assert.equal(fromDoc(doc)[0].spans, undefined);
});

// --- formatting ---------------------------------------------------------------------------------------

test("bold over words of which some are bold makes them all bold", () => {
  let state = stateOf([{ kind: "paragraph", text: "Small enterprises must pay", marks: [{ type: "bold", start: 6, end: 17 }] }]);
  state = select(state, inWords(state.doc, 0), inWords(state.doc, 0, 26));
  state = apply(state, commands.mark("strong")).state;
  assert.deepEqual(fromDoc(state.doc)[0].marks, [{ type: "bold", start: 0, end: 26 }]);
  state = apply(state, commands.mark("strong")).state; // every word bold: now removed
  assert.equal(fromDoc(state.doc)[0].marks, undefined);
});

test("bold never goes on a number chip, and spaces at the ends stay plain", () => {
  let state = stateOf([PAGE[1]]);
  const start = inWords(state.doc, 0) - 1; // before the chip
  state = select(state, start, inWords(state.doc, 0, 4));
  state = apply(state, commands.mark("strong")).state;
  assert.equal(state.doc.child(0).firstChild.firstChild.marks.length, 0);
  assert.deepEqual(fromDoc(state.doc)[0].marks, [{ type: "bold", start: 0, end: 3 }]);
});

test("formatting does not apply in a margin note", () => {
  let state = stateOf(PAGE);
  state = select(state, inBlock(state.doc, N.note, 0, 1), inBlock(state.doc, N.note, 0, 5));
  assert.equal(apply(state, commands.mark("strong")).done, false);
});

test("bold in a cell, a header cell and a caption; superscript typed at the cursor in a cell", () => {
  let state = stateOf([{ kind: "table", caption: "Rates of tax", header_rows: 1, rows: [["Band", "Rate"], ["Area in m", "20%"]] }]);
  state = select(state, inBlock(state.doc, N.table_cell, 0, 8), inBlock(state.doc, N.table_cell, 0, 9)); // "m"
  state = apply(state, commands.mark("strong")).state;
  state = select(state, inBlock(state.doc, N.table_header, 1), inBlock(state.doc, N.table_header, 1, 4)); // "Rate"
  state = apply(state, commands.mark("em")).state;
  state = select(state, inBlock(state.doc, N.table_caption, 0, 0), inBlock(state.doc, N.table_caption, 0, 5));
  state = apply(state, commands.mark("strong")).state;
  state = select(state, inBlock(state.doc, N.table_cell, 0, 9)); // after "m"
  state = press(state, "Mod-b"); // bold off for what is typed next
  state = press(state, "Mod-.");
  state = state.apply(state.tr.insertText("2"));
  const unit = fromDoc(state.doc)[0];
  assert.deepEqual(unit.rows, [["Band", "Rate"], ["Area in m2", "20%"]]);
  assert.deepEqual(unit.caption_marks, [{ type: "bold", start: 0, end: 5 }]);
  assert.deepEqual(unit.cell_marks, {
    "0,1": [{ type: "italic", start: 0, end: 4 }],
    "1,0": [{ type: "bold", start: 8, end: 9 }, { type: "superscript", start: 9, end: 10 }],
  });
});

test("bold over selected cells formats every cell; again, it comes off", () => {
  let state = stateOf([{ kind: "table", caption: "", header_rows: 0, rows: [["From", "To"], ["0", "360,000"]] }]);
  const a = inBlock(state.doc, N.table_cell, 0) - 1, d = inBlock(state.doc, N.table_cell, 3) - 1;
  state = state.apply(state.tr.setSelection(CellSelection.create(state.doc, a, d)));
  assert.equal(apply(state, commands.mark("strong")).done, true);
  state = apply(state, commands.mark("strong")).state;
  assert.deepEqual(fromDoc(state.doc)[0].cell_marks, {
    "0,0": [{ type: "bold", start: 0, end: 4 }], "0,1": [{ type: "bold", start: 0, end: 2 }],
    "1,0": [{ type: "bold", start: 0, end: 1 }], "1,1": [{ type: "bold", start: 0, end: 7 }],
  });
  state = apply(state, commands.mark("strong")).state;
  assert.equal(fromDoc(state.doc)[0].cell_marks, undefined);
});

test("bold at the cursor applies to the words typed next", () => {
  let state = stateOf([{ kind: "paragraph", text: "Words" }]);
  state = select(state, inWords(state.doc, 0, 5));
  state = apply(state, commands.mark("strong")).state;
  state = state.apply(state.tr.insertText(" more"));
  assert.deepEqual(fromDoc(state.doc)[0], { kind: "paragraph", text: "Words more", marks: [{ type: "bold", start: 5, end: 10 }] });
});

test("bold across units and a table formats every word, in the cells too", () => {
  let state = stateOf(PAGE);
  state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, inWords(state.doc, 2), inWords(state.doc, 4, 5))));
  state = apply(state, commands.mark("strong")).state;
  const units = fromDoc(state.doc);
  assert.deepEqual(units[2].marks.find((m) => m.type === "bold"), { type: "bold", start: 0, end: 39 });
  const bold = {};
  TAX_TABLE.rows.forEach((row, r) => row.forEach((cell, c) => { if (cell) bold[`${r},${c}`] = [{ type: "bold", start: 0, end: cell.length }]; }));
  assert.deepEqual(units[3], { ...TAX_TABLE, cell_marks: bold });
  assert.deepEqual(units[4].marks, [{ type: "bold", start: 0, end: 5 }]);
});

// --- Enter, Backspace, Delete -------------------------------------------------------------------------

test("Backspace at the start of a margin note never runs it into the words", () => {
  let state = stateOf([PAGE[1]]);
  state = select(state, inBlock(state.doc, N.note));
  state = press(state, "Backspace");
  assert.deepEqual(fromDoc(state.doc), [PAGE[1]]);
});

test("Backspace removes an empty note and joins a note to the note above", () => {
  let state = stateOf([{ kind: "paragraph", text: "Words", notes: ["First", "Second"] }]);
  state = select(state, inBlock(state.doc, N.note, 1));
  state = press(state, "Backspace");
  assert.deepEqual(fromDoc(state.doc)[0].notes, ["First Second"]);
  state = apply(state, commands.addNote).state;
  state = press(state, "Backspace");
  assert.deepEqual(fromDoc(state.doc)[0].notes, ["First Second"]);
});

test("Delete at the end of a unit's words joins the next unit with a space, its number kept as words", () => {
  let state = stateOf([{ kind: "paragraph", text: "The end.", notes: ["Beside"] }, PAGE[2]]);
  state = select(state, inWords(state.doc, 0, 8));
  state = press(state, "Delete");
  const [unit] = fromDoc(state.doc);
  assert.equal(unit.text, "The end. (a) oversee the implementation of this Act;");
  assert.deepEqual(unit.notes, ["Beside"]);
  assert.deepEqual(unit.marks, [{ type: "bold", start: 13, end: 20 }, { type: "superscript", start: 25, end: 39 }]);
  assert.equal(state.selection.from, inWords(state.doc, 0, 8));
});

test("Backspace at the start of a unit after an empty one removes the empty one", () => {
  let state = stateOf([{ kind: "paragraph", text: "" }, PAGE[0]]);
  state = select(state, inWords(state.doc, 1));
  state = press(state, "Backspace");
  assert.deepEqual(fromDoc(state.doc), [PAGE[0]]);
});

test("Enter at the start of the words opens a unit above; the notes stay", () => {
  let state = stateOf([{ kind: "paragraph", text: "Words", notes: ["Note"] }]);
  state = select(state, inWords(state.doc, 0));
  state = press(state, "Enter");
  assert.deepEqual(fromDoc(state.doc), [{ kind: "paragraph", text: "" }, { kind: "paragraph", text: "Words", notes: ["Note"] }]);
});

// --- tables -------------------------------------------------------------------------------------------

test("Backspace and Delete at a cell's edges leave the table whole", () => {
  let state = stateOf([TAX_TABLE]);
  state = select(state, inBlock(state.doc, N.table_cell, 1));
  state = press(state, "Backspace");
  state = select(state, inBlock(state.doc, N.table_cell, 0, 4));
  state = press(state, "Delete");
  assert.deepEqual(fromDoc(state.doc), [TAX_TABLE]);
});

test("rows added below the header rows are body rows; inside them, header rows", () => {
  let state = stateOf([TAX_TABLE]);
  state = select(state, inBlock(state.doc, N.table_header, 2, 1)); // "From", the last header row
  state = apply(state, commands.addRowAfter).state;
  let table = fromDoc(state.doc)[0];
  assert.equal(table.header_rows, 2);
  assert.deepEqual(table.rows[2], ["", "", ""]);
  state = apply(state, commands.addRowBefore).state;
  table = fromDoc(state.doc)[0];
  assert.equal(table.header_rows, 3);
  assert.deepEqual(table.spans, [[0, 0, 1, 2], [0, 2, 3, 1]]);
  assert.equal(state.doc.child(0).child(1).child(1).child(0).type, N.table_header);
});

test("deleting a header row lowers the header count; deleting every row deletes the table", () => {
  let state = stateOf([TAX_TABLE, PAGE[4]]);
  state = select(state, inBlock(state.doc, N.table_header, 2, 1));
  state = apply(state, commands.deleteRow).state;
  assert.equal(fromDoc(state.doc)[0].header_rows, 1);
  const cells = state.doc.child(0).child(1);
  const start = 1 + state.doc.child(0).child(0).nodeSize + 1;
  const first = start + 1, last = start + cells.content.size - cells.lastChild.nodeSize + 1 + cells.lastChild.content.size - cells.lastChild.lastChild.nodeSize;
  state = state.apply(state.tr.setSelection(CellSelection.create(state.doc, first, last)));
  state = apply(state, commands.deleteRow).state;
  assert.deepEqual(fromDoc(state.doc), [PAGE[4]]);
});

test("merging cells keeps every cell's words; splitting gives the positions back", () => {
  let state = stateOf([{ kind: "table", caption: "Rates", header_rows: 0, rows: [["From", "To"], ["0", "360,000"]] }]);
  const a = inBlock(state.doc, N.table_cell, 0) - 1, b = inBlock(state.doc, N.table_cell, 1) - 1;
  state = state.apply(state.tr.setSelection(CellSelection.create(state.doc, a, b)));
  state = apply(state, commands.mergeCells).state;
  assert.deepEqual(fromDoc(state.doc)[0], { kind: "table", caption: "Rates", header_rows: 0, rows: [["From To", ""], ["0", "360,000"]], spans: [[0, 0, 1, 2]] });
  state = apply(state, commands.splitCell).state;
  assert.deepEqual(fromDoc(state.doc)[0].rows, [["From To", ""], ["0", "360,000"]]);
  assert.equal(fromDoc(state.doc)[0].spans, undefined);
});

test("merging cells keeps each cell's formatting with its words", () => {
  const unit = {
    kind: "table", caption: "", header_rows: 0, rows: [["From", " 10 m2 "], ["0", "360,000"]],
    cell_marks: { "0,0": [{ type: "bold", start: 0, end: 4 }], "0,1": [{ type: "italic", start: 1, end: 3 }, { type: "superscript", start: 5, end: 6 }] },
  };
  let state = stateOf([unit]);
  const a = inBlock(state.doc, N.table_cell, 0) - 1, b = inBlock(state.doc, N.table_cell, 1) - 1;
  state = state.apply(state.tr.setSelection(CellSelection.create(state.doc, a, b)));
  state = apply(state, commands.mergeCells).state;
  const merged = fromDoc(state.doc)[0];
  assert.deepEqual(merged.rows, [["From 10 m2", ""], ["0", "360,000"]]);
  assert.deepEqual(merged.spans, [[0, 0, 1, 2]]);
  assert.deepEqual(merged.cell_marks, { "0,0": [{ type: "bold", start: 0, end: 4 }, { type: "italic", start: 5, end: 7 }, { type: "superscript", start: 9, end: 10 }] });
  state = apply(state, commands.splitCell).state;
  assert.deepEqual(fromDoc(state.doc)[0].cell_marks, merged.cell_marks); // the words stay in the first cell
});

test("a row that joins or leaves the header rows keeps its cells' formatting", () => {
  let state = stateOf([AREA_TABLE]);
  state = select(state, inBlock(state.doc, N.table_cell, 0, 1)); // first body row
  state = apply(state, commands.toggleHeaderRow).state;
  assert.deepEqual(fromDoc(state.doc), [{ ...AREA_TABLE, header_rows: 2 }]);
});

test("the header-row toggle moves the header boundary to the row under the cursor", () => {
  let state = stateOf([TAX_TABLE]);
  state = select(state, inBlock(state.doc, N.table_cell, 0, 1)); // first body row
  state = apply(state, commands.toggleHeaderRow).state;
  assert.equal(fromDoc(state.doc)[0].header_rows, 3);
  state = select(state, inBlock(state.doc, N.table_header, 0, 1)); // first row
  state = apply(state, commands.toggleHeaderRow).state;
  assert.equal(fromDoc(state.doc)[0].header_rows, 0);
  assert.equal(state.doc.child(0).child(1).child(0).child(0).type, N.table_cell);
});

test("Tab moves between cells and adds a row after the last one", () => {
  let state = stateOf([{ kind: "table", caption: "", header_rows: 0, rows: [["a", "b"]] }]);
  state = select(state, inBlock(state.doc, N.table_cell, 0, 1));
  state = press(state, "Tab");
  assert.equal(state.selection.$from.parent.textContent, "b");
  state = press(state, "Tab");
  assert.deepEqual(fromDoc(state.doc)[0].rows, [["a", "b"], ["", ""]]);
  assert.equal(state.selection.$from.parent.type, N.table_cell);
  state = press(state, "Shift-Tab");
  assert.equal(state.selection.$from.parent.textContent, "b");
});

test("a column added and removed leaves the merges consistent", () => {
  let state = stateOf([TAX_TABLE]);
  state = select(state, inBlock(state.doc, N.table_cell, 0, 1)); // "0"
  state = apply(state, commands.addColumnAfter).state;
  let table = fromDoc(state.doc)[0];
  assert.deepEqual(table.rows[0], ["Annual Taxable Income (RWF)", "", "", "Tax Rate"]);
  assert.deepEqual(table.spans, [[0, 0, 1, 3], [0, 3, 2, 1]]);
  state = select(state, inBlock(state.doc, N.table_cell, 1)); // the new, empty cell beside "0"
  state = apply(state, commands.deleteColumn).state;
  assert.deepEqual(fromDoc(state.doc)[0], TAX_TABLE);
});

test("a table inserted after the cursor has a header row and the cursor in its first cell", () => {
  let state = stateOf([PAGE[4]]);
  state = select(state, inWords(state.doc, 0, 3));
  state = apply(state, commands.insertTable).state;
  assert.deepEqual(fromDoc(state.doc)[1], { kind: "table", caption: "", header_rows: 1, rows: [["", ""], ["", ""]] });
  assert.equal(state.selection.$from.parent.type, N.table_header);
});

// --- pasting ------------------------------------------------------------------------------------------

test("plain text from a PDF: wrapped lines join; numbered lines and sentence ends start units", () => {
  const text = "1. The Minister may make\nregulations for the pur-\npose of this Act.\n(a) prescribing fees;\n(b) generally.\n\nA new paragraph";
  assert.deepEqual(unitsFromText(text, "list_item"), [
    { kind: "list_item", text: "1. The Minister may make regulations for the pur-pose of this Act." },
    { kind: "list_item", text: "(a) prescribing fees;" },
    { kind: "list_item", text: "(b) generally." },
    { kind: "list_item", text: "A new paragraph" },
  ]);
  assert.deepEqual(unitsFromText("CHAPTER ONE\nGENERAL PROVISIONS", "heading"), [{ kind: "heading", text: "CHAPTER ONE GENERAL PROVISIONS" }]);
});

test("several units pasted mid-unit: words before and after the cursor frame them, notes stay", () => {
  let state = stateOf([{ kind: "paragraph", text: "Before after", notes: ["Note"] }]);
  state = select(state, inWords(state.doc, 0, 7));
  const blocks = [{ kind: "paragraph", text: "one", marks: [{ type: "bold", start: 0, end: 3 }] }, { kind: "heading", text: "two" }, { kind: "paragraph", text: "three " }].map((u) => unitNode(u));
  state = state.apply(pasteUnits(state, blocks));
  assert.deepEqual(fromDoc(state.doc), [
    { kind: "paragraph", text: "Before one", marks: [{ type: "bold", start: 7, end: 10 }], notes: ["Note"] },
    { kind: "heading", text: "two" },
    { kind: "paragraph", text: "three after" },
  ]);
  assert.equal(state.selection.from, inWords(state.doc, 2, 6));
});

test("a table pasted at the start of an empty unit takes its place", () => {
  let state = stateOf([{ kind: "paragraph", text: "" }, PAGE[4]]);
  state = select(state, inWords(state.doc, 0));
  state = state.apply(pasteUnits(state, [unitNode(TAX_TABLE)]));
  assert.deepEqual(fromDoc(state.doc), [TAX_TABLE, PAGE[4]]);
});

// A header cell with a word cut short and bold on its first word; a body cell with a misread number.
const INCOME = {
  kind: "table",
  caption: "Rates",
  header_rows: 1,
  rows: [["Annual Taxable Inc (RWF)", "Rate"], ["0 to 36O,OOO RWF", "0%"]],
  cell_marks: { "0,0": [{ type: "bold", start: 0, end: 6 }] },
};

test("words pasted from outside at a cursor in a cell go in there; the rest of the cell and its formatting stay", () => {
  let state = stateOf([INCOME]);
  state = select(state, inBlock(state.doc, N.table_header, 0, 18)); // after "Inc"
  state = paste(state, parsedInCell("ome"), { "text/plain": "ome" });
  assert.deepEqual(fromDoc(state.doc), [{ ...INCOME, rows: [["Annual Taxable Income (RWF)", "Rate"], INCOME.rows[1]] }]);
  assert.equal(state.selection.from, inBlock(state.doc, N.table_header, 0, 21));
});

test("lines pasted over words in a cell go in on one line, in that cell, not across the row", () => {
  let state = stateOf([INCOME]);
  state = select(state, inBlock(state.doc, N.table_cell, 0), inBlock(state.doc, N.table_cell, 0, 12)); // "0 to 36O,OOO"
  state = paste(state, parsedInCell("0 to", "360,000"), { "text/plain": "0 to\n360,000" });
  assert.deepEqual(fromDoc(state.doc), [{ ...INCOME, rows: [INCOME.rows[0], ["0 to 360,000 RWF", "0%"]] }]);
});

test("HTML paragraphs pasted into a cell are words with their formatting, which the table plugin leaves to the page's paste", () => {
  let state = stateOf([INCOME]);
  state = select(state, inBlock(state.doc, N.table_cell, 0, 12)); // before " RWF"
  // <p><b>Annual</b> income</p><p>in m<sup>2</sup></p>, as the clipboard parser reads it into a cell.
  // The page's paste then reads the words from the HTML itself (unitsFromHTML, which needs a browser).
  const slice = transformed(state, parsedInCell(formatted("Annual income", [{ type: "bold", start: 0, end: 6 }]), formatted("in m2", [{ type: "superscript", start: 4, end: 5 }])));
  assert.deepEqual(slice.content.content.map((n) => [n.text, n.marks.map((m) => m.type.name)]), [["Annual", ["strong"]], [" income in m", []], ["2", ["sup"]]]);
  assert.equal(tablePaste({ state, dispatch: () => assert.fail("the table plugin pasted cells") }, null, slice), false);
});

test("cells copied from the page, and words pasted over selected cells, still go in as cells", () => {
  const state = stateOf([{ kind: "table", caption: "", header_rows: 0, rows: [["a", "b"], ["c", "d"]] }]);
  const a = inBlock(state.doc, N.table_cell, 0) - 1, b = inBlock(state.doc, N.table_cell, 1) - 1;
  const copied = CellSelection.create(state.doc, a, b).content(); // a row of two cells, as the clipboard gives it back
  let pasted = paste(select(state, inBlock(state.doc, N.table_cell, 2, 1)), copied); // cursor in "c"
  assert.deepEqual(fromDoc(pasted.doc)[0].rows, [["a", "b"], ["a", "b"]]);
  pasted = paste(state.apply(state.tr.setSelection(CellSelection.create(state.doc, a, b))), parsedInCell("x"), { "text/plain": "x" });
  assert.deepEqual(fromDoc(pasted.doc)[0].rows, [["x", "x"], ["c", "d"]]);
});

// --- find and replace ---------------------------------------------------------------------------------

test("find counts matches in words, notes and cells; replace keeps the formatting", () => {
  let state = stateOf([
    { kind: "paragraph", text: "The Tax is due; tax rates vary.", marks: [{ type: "bold", start: 4, end: 7 }], notes: ["Tax"] },
    { kind: "table", caption: "", header_rows: 0, rows: [["Taxes", "tax"]] },
  ]);
  state = state.apply(state.tr.setMeta(findKey, { query: "tax", options: { wholeWord: true } }));
  assert.equal(findKey.getState(state).matches.length, 4);
  state = apply(state, commands.replaceAll("Levy")).state;
  const [unit, table] = fromDoc(state.doc);
  assert.equal(unit.text, "The Levy is due; Levy rates vary.");
  assert.deepEqual(unit.marks, [{ type: "bold", start: 4, end: 8 }]);
  assert.deepEqual(unit.notes, ["Levy"]);
  assert.deepEqual(table.rows, [["Taxes", "Levy"]]);
});

test("replace in a cell keeps the formatting of the words it replaces", () => {
  let state = stateOf([{ kind: "table", caption: "", header_rows: 0, rows: [["Area in m2"]], cell_marks: { "0,0": [{ type: "bold", start: 0, end: 4 }] } }]);
  state = state.apply(state.tr.setMeta(findKey, { query: "Area", options: {} }));
  state = apply(state, commands.replaceAll("Floor area")).state;
  assert.deepEqual(fromDoc(state.doc)[0].cell_marks, { "0,0": [{ type: "bold", start: 0, end: 10 }] });
});

test("words pasted into a cell keep their formatting; into a margin note they are plain", () => {
  const blocks = [unitNode({ kind: "paragraph", marker: "6.", text: "Area  in m2", marks: [{ type: "bold", start: 0, end: 4 }, { type: "superscript", start: 10, end: 11 }] })];
  const words = pastedWords(blocks, N.table_cell);
  assert.deepEqual(words.content.map((n) => [n.text, n.marks.map((m) => m.type.name)]), [["6. ", []], ["Area", ["strong"]], [" in m", []], ["2", ["sup"]]]);
  const plain = pastedWords([...blocks, unitNode({ kind: "paragraph", text: "next", notes: ["note"] })], N.note);
  assert.deepEqual(plain.content.map((n) => [n.text, n.marks.length]), [["6. Area in m2 next note", 0]]);
});

test("a match never runs across a number chip", () => {
  let state = stateOf([{ kind: "paragraph", text: "the", marker: "1." }]);
  state = state.apply(state.tr.setMeta(findKey, { query: "1.the", options: {} }));
  assert.equal(findKey.getState(state).matches.length, 0);
});

test("a node selection of a number chip has no formatting to apply", () => {
  let state = stateOf([PAGE[1]]);
  state = state.apply(state.tr.setSelection(NodeSelection.create(state.doc, inWords(state.doc, 0) - 1)));
  assert.equal(apply(state, commands.mark("strong")).done, false);
});

test("words pasted straight after a full stop get a space", () => {
  let state = stateOf([{ kind: "paragraph", text: "The end." }]);
  state = select(state, inWords(state.doc, 0, 8));
  state = state.apply(pasteUnits(state, [unitNode({ kind: "paragraph", text: "More words" })]));
  assert.equal(fromDoc(state.doc)[0].text, "The end. More words");
  state = select(state, inWords(state.doc, 0, 2));
  state = state.apply(pasteUnits(state, [unitNode({ kind: "paragraph", text: "x" })]));
  assert.equal(fromDoc(state.doc)[0].text, "Thxe end. More words");
});

test("a number chip pasted into words becomes words; at the start of an empty unit it stays a chip", () => {
  let state = stateOf([{ kind: "paragraph", text: "fees;" }, { kind: "paragraph", text: "" }]);
  state = select(state, inWords(state.doc, 0, 5));
  state = state.apply(pasteUnits(state, [unitNode({ kind: "paragraph", text: "The Commission", marker: "6." })]));
  assert.equal(fromDoc(state.doc)[0].text, "fees; 6. The Commission");
  assert.equal(fromDoc(state.doc)[0].marker, undefined);
  state = select(state, inWords(state.doc, 1));
  state = state.apply(pasteUnits(state, [unitNode({ kind: "paragraph", text: "The Commission", marker: "6." })]));
  assert.deepEqual(fromDoc(state.doc)[1], { kind: "paragraph", text: "The Commission", marker: "6." });
});
