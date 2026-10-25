// Tables as printed, edited with prosemirror-tables: cells selected by dragging or Shift+arrows,
// merged and split, rows and columns added and removed, header rows marked. What is stored
// (workbench.content): a full grid of text, one string per position, merges listed as
// [row, column, rows, columns] with the positions they cover left empty, how many leading rows are
// headers, the formatting of the caption and of each cell ({"row,column": ranges}), as a unit's
// marks are kept, and the cells marked illegible or unsupported ({"row,column": state}). In the
// editor a merged cell spans its rows and columns and the covered positions are not there, so no
// words, no formatting and no mark can hide in them.

import { EditorState, Plugin, Selection, TextSelection } from "prosemirror-state";
import {
  TableMap, CellSelection, isInTable, selectedRect, goToNextCell,
  addRowBefore as addRowBeforeCells, addRowAfter as addRowAfterCells, deleteRow as deleteRowCells,
  addColumnBefore, addColumnAfter, deleteColumn as deleteColumnCells,
  mergeCells as mergeCellsKeepingText, splitCell, selectionCell,
} from "prosemirror-tables";
import { schema, formatted, wordsAndMarks, cellState } from "./schema.js";

const N = schema.nodes;

// --- stored table <-> table node -------------------------------------------------------------------

// A merge that overlaps another, or would cover words, is not applied: its cells stay apart, so
// nothing typed in them is lost. `notes` hears about it.
export function tableNode(unit, notes = []) {
  const grid = unit.rows && unit.rows.length ? unit.rows : [[""]];
  const width = Math.max(1, ...grid.map((row) => row.length));
  const rows = grid.map((row) => Array.from({ length: width }, (_, c) => (typeof row[c] === "string" ? row[c] : "")));
  const header = Math.min(Math.max(unit.header_rows || 0, 0), rows.length);
  const extent = new Map(), covered = new Set();
  let undone = 0;
  for (const [r, c, rs, cs] of unit.spans || []) {
    const fits = r >= 0 && c >= 0 && rs >= 1 && cs >= 1 && r + rs <= rows.length && c + cs <= width;
    const places = [];
    if (fits) for (let dr = 0; dr < rs; dr++) for (let dc = 0; dc < cs; dc++) places.push(`${r + dr},${c + dc}`);
    const clash = !fits || places.some((p) => covered.has(p) || extent.has(p));
    if (clash || places.slice(1).some((p) => { const [rr, cc] = p.split(",").map(Number); return rows[rr][cc].trim(); })) { undone++; continue; }
    extent.set(places[0], [rs, cs]);
    places.slice(1).forEach((p) => covered.add(p));
  }
  if (undone) notes.push(`${undone} merged cell${undone === 1 ? "" : "s"} shown apart: ${undone === 1 ? "it covered" : "they covered"} words or another merge`);
  const tableRows = rows.map((row, r) => N.table_row.create(null, row.flatMap((value, c) => {
    if (covered.has(`${r},${c}`)) return [];
    const [rowspan, colspan] = extent.get(`${r},${c}`) || [1, 1];
    const marks = unit.cell_marks && unit.cell_marks[`${r},${c}`];
    const state = cellState(unit.cell_states && unit.cell_states[`${r},${c}`]);
    return [(r < header ? N.table_header : N.table_cell).create({ rowspan, colspan, state }, formatted(value, marks))];
  })));
  const caption = N.table_caption.create(null, formatted(unit.caption || "", unit.caption_marks));
  return N.table_unit.create({ header_rows: header }, [caption, N.table.create(null, tableRows)]);
}

export function tableUnit(block) {
  const caption = wordsAndMarks(block.child(0));
  const table = block.child(1);
  const map = TableMap.get(table);
  const rows = Array.from({ length: map.height }, () => Array(map.width).fill(""));
  const spans = [];
  const cellMarks = {}, cellStates = {};
  const seen = new Set();
  // Row by row, so a cell is met first at its top-left corner and the merges come in grid order.
  for (const pos of map.map) {
    if (seen.has(pos)) continue;
    seen.add(pos);
    const { top, left, bottom, right } = map.findCell(pos);
    const cell = table.nodeAt(pos);
    const { words, ranges } = wordsAndMarks(cell);
    rows[top][left] = words;
    if (ranges.length) cellMarks[`${top},${left}`] = ranges;
    if (cellState(cell.attrs.state)) cellStates[`${top},${left}`] = cell.attrs.state;
    if (bottom - top > 1 || right - left > 1) spans.push([top, left, bottom - top, right - left]);
  }
  const unit = { kind: "table", caption: caption.words, header_rows: Math.min(block.attrs.header_rows, rows.length), rows };
  if (caption.ranges.length) unit.caption_marks = caption.ranges;
  if (spans.length) unit.spans = spans;
  if (Object.keys(cellMarks).length) unit.cell_marks = cellMarks;
  if (Object.keys(cellStates).length) unit.cell_states = cellStates;
  return unit;
}

// --- header rows -------------------------------------------------------------------------------------

function tableUnitAt($pos) {
  for (let depth = $pos.depth; depth > 0; depth--) {
    if ($pos.node(depth).type === N.table_unit) return { node: $pos.node(depth), pos: $pos.before(depth) };
  }
  return null;
}

// The cells whose top-left corner is in a header row are header cells; after any change the cell
// types follow the table's header_rows.
export const headerRows = new Plugin({
  appendTransaction(transactions, _, state) {
    if (!transactions.some((tr) => tr.docChanged)) return null;
    const tr = state.tr;
    state.doc.forEach((block, pos) => {
      if (block.type !== N.table_unit) return;
      const table = block.child(1);
      const start = pos + 1 + block.child(0).nodeSize + 1;
      const map = TableMap.get(table);
      const header = Math.min(block.attrs.header_rows, map.height);
      if (header !== block.attrs.header_rows) tr.setNodeMarkup(pos, null, { header_rows: header });
      for (const cellPos of new Set(map.map)) {
        const cell = table.nodeAt(cellPos);
        const type = map.findCell(cellPos).top < header ? N.table_header : N.table_cell;
        if (cell.type !== type) tr.setNodeMarkup(start + cellPos, type, cell.attrs);
      }
    });
    return tr.docChanged ? tr : null;
  },
});

// A row command that keeps the header rows where they were: `change` gives the new count.
function keepingHeader(command, change) {
  return (state, dispatch) => {
    if (!isInTable(state) || !command(state)) return false;
    if (!dispatch) return true;
    const rect = selectedRect(state);
    const unit = tableUnitAt(state.selection.$from);
    let tr = null;
    command(state, (result) => { tr = result; });
    if (!tr) return false;
    const header = Math.max(0, change(unit.node.attrs.header_rows, rect));
    if (header !== unit.node.attrs.header_rows) tr.setNodeMarkup(tr.mapping.map(unit.pos), null, { header_rows: header });
    dispatch(tr.scrollIntoView());
    return true;
  };
}

// A row added inside the header rows (or above the first) is a header row; one added below them is not.
export const addRowBefore = keepingHeader(addRowBeforeCells, (header, rect) => (rect.top < header ? header + 1 : header));
export const addRowAfter = keepingHeader(addRowAfterCells, (header, rect) => (rect.bottom < header ? header + 1 : header));
const removeRows = keepingHeader(deleteRowCells, (header, rect) => header - Math.max(0, Math.min(rect.bottom, header) - rect.top));
export { addColumnBefore, addColumnAfter, splitCell };

// The row under the cursor (and those below it) leave the header rows, or it (and those above it) join them.
export function toggleHeaderRow(state, dispatch) {
  if (!isInTable(state)) return false;
  const rect = selectedRect(state);
  const unit = tableUnitAt(state.selection.$from);
  const header = rect.top < unit.node.attrs.header_rows ? rect.top : rect.bottom;
  if (dispatch) dispatch(state.tr.setNodeMarkup(unit.pos, null, { header_rows: header }));
  return true;
}

export function inHeaderRow(state) {
  if (!isInTable(state)) return false;
  return selectedRect(state).top < tableUnitAt(state.selection.$from).node.attrs.header_rows;
}

// --- tables, rows and columns ----------------------------------------------------------------------

export function deleteTable(state, dispatch) {
  const unit = tableUnitAt(state.selection.$from);
  if (!unit) return false;
  if (dispatch) {
    const end = unit.pos + unit.node.nodeSize;
    const tr = state.doc.childCount > 1 ? state.tr.delete(unit.pos, end) : state.tr.replaceWith(unit.pos, end, N.unit.create(null, N.body.create()));
    tr.setSelection(Selection.near(tr.doc.resolve(Math.min(unit.pos, tr.doc.content.size)), -1));
    dispatch(tr.scrollIntoView());
  }
  return true;
}

// Removing every row (or every column) removes the table.
export function deleteRow(state, dispatch) {
  if (!isInTable(state)) return false;
  const rect = selectedRect(state);
  if (rect.top === 0 && rect.bottom === rect.map.height) return deleteTable(state, dispatch);
  return removeRows(state, dispatch);
}

export function deleteColumn(state, dispatch) {
  if (!isInTable(state)) return false;
  const rect = selectedRect(state);
  if (rect.left === 0 && rect.right === rect.map.width) return deleteTable(state, dispatch);
  return deleteColumnCells(state, dispatch);
}

// A cell's words without the spaces at either end, each word keeping its formatting.
function trimmedWords(cell) {
  const nodes = [];
  cell.forEach((node) => nodes.push(node));
  while (nodes.length) {
    const value = nodes[0].text.replace(/^\s+/, "");
    if (value) { nodes[0] = schema.text(value, nodes[0].marks); break; }
    nodes.shift();
  }
  while (nodes.length) {
    const last = nodes.length - 1, value = nodes[last].text.replace(/\s+$/, "");
    if (value) { nodes[last] = schema.text(value, nodes[last].marks); break; }
    nodes.pop();
  }
  return nodes;
}

// Merging keeps every cell's words, in reading order, separated by a space, and each word its
// formatting.
export function mergeCells(state, dispatch) {
  if (!mergeCellsKeepingText(state)) return false;
  if (!dispatch) return true;
  const rect = selectedRect(state);
  const cells = [...new Set(rect.map.cellsInRect(rect))].sort((a, b) => a - b);
  const words = cells.map((pos) => trimmedWords(rect.table.nodeAt(pos))).filter((nodes) => nodes.length)
    .flatMap((nodes, i) => (i ? [schema.text(" "), ...nodes] : nodes));
  const tr = state.tr;
  cells.forEach((pos, i) => {
    const cell = rect.table.nodeAt(pos);
    const from = tr.mapping.map(rect.tableStart + pos + 1), to = tr.mapping.map(rect.tableStart + pos + cell.nodeSize - 1);
    tr.replaceWith(from, to, i === 0 ? words : []);
  });
  const emptied = EditorState.create({ doc: tr.doc, selection: state.selection.map(tr.doc, tr.mapping) });
  let merged = null;
  mergeCellsKeepingText(emptied, (result) => { merged = result; });
  merged.steps.forEach((step) => tr.step(step));
  tr.setSelection(Selection.fromJSON(tr.doc, merged.selection.toJSON()));
  // The merged cell keeps a mark only when every cell merged had that same one (TXP-02).
  const states = new Set(cells.map((pos) => rect.table.nodeAt(pos).attrs.state));
  const kept = states.size === 1 ? [...states][0] : null;
  const at = tr.selection.$anchorCell.pos, cell = tr.doc.nodeAt(at);
  if (cell.attrs.state !== kept) tr.setNodeMarkup(at, null, { ...cell.attrs, state: kept });
  dispatch(tr);
  return true;
}

// --- cells that cannot be transcribed ----------------------------------------------------------------

function selectedCells(state) {
  if (state.selection instanceof CellSelection) {
    const cells = [];
    state.selection.forEachCell((node, pos) => cells.push({ node, pos }));
    return cells;
  }
  if (!isInTable(state)) return [];
  const $cell = selectionCell(state);
  return [{ node: $cell.nodeAfter, pos: $cell.pos }];
}

// Mark the selected cells (or the one holding the cursor) illegible or unsupported, or clear them
// (null). Words left in a marked cell stay, and stop approval until one or the other goes.
export function markCells(value) {
  return (state, dispatch) => {
    const changed = selectedCells(state).filter(({ node }) => node.attrs.state !== value);
    if (!changed.length) return false;
    if (dispatch) {
      const tr = state.tr;
      changed.forEach(({ node, pos }) => tr.setNodeMarkup(pos, null, { ...node.attrs, state: value }));
      dispatch(tr);
    }
    return true;
  };
}

// The mark the selected cells share, or null when they have none or different ones.
export function cellStateAt(state) {
  const states = new Set(selectedCells(state).map(({ node }) => node.attrs.state));
  return states.size === 1 ? [...states][0] : null;
}

// Insert a table after the block holding the cursor: two columns, a header row and a row below it.
export function insertTable(state, dispatch) {
  const { $from } = state.selection;
  const at = $from.depth ? $from.after(1) : state.doc.content.size;
  if (dispatch) {
    const row = (type) => N.table_row.create(null, [type.create(), type.create()]);
    const node = N.table_unit.create({ header_rows: 1 }, [N.table_caption.create(), N.table.create(null, [row(N.table_header), row(N.table_cell)])]);
    const tr = state.tr.insert(at, node);
    tr.setSelection(TextSelection.create(tr.doc, at + 1 + node.child(0).nodeSize + 3));
    dispatch(tr.scrollIntoView());
  }
  return true;
}

// Tab and Shift+Tab move between cells (selecting the cell's words); Tab in the last cell adds a
// row, as in a word processor. Tab in the caption goes to the first cell.
export function nextCell(direction) {
  return (state, dispatch) => {
    const { $from } = state.selection;
    if ($from.parent.type === N.table_caption) {
      if (direction < 0) return true;
      if (dispatch) dispatch(state.tr.setSelection(TextSelection.near(state.doc.resolve($from.after() + 1))).scrollIntoView());
      return true;
    }
    if (!isInTable(state)) return false;
    if (goToNextCell(direction)(state, dispatch)) return true;
    if (direction < 0 || !dispatch) return true;
    let tr = null;
    addRowAfter(state, (result) => { tr = result; });
    if (!tr) return true;
    const unit = tableUnitAt(state.selection.$from);
    const block = tr.doc.nodeAt(unit.pos);
    const table = block.child(1);
    const rowStart = unit.pos + 1 + block.child(0).nodeSize + 1 + table.content.size - table.lastChild.nodeSize;
    tr.setSelection(TextSelection.near(tr.doc.resolve(rowStart + 1)));
    dispatch(tr.scrollIntoView());
    return true;
  };
}

// Enter in the caption goes to the first cell; in a cell it does nothing (cells hold one line).
export function enterInTable(state, dispatch) {
  const { $from } = state.selection;
  if ($from.parent.type === N.table_caption) return nextCell(1)(state, dispatch);
  return isInTable(state) || state.selection instanceof CellSelection;
}
