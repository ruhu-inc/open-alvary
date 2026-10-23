// Structured document surface for the review workbench.
//
// The page is edited in place, laid out as printed: each unit (heading, paragraph, list item) is
// a block with its words and, beside it, its margin notes; section and paragraph numbers the
// readers supplied are small chips; tables are rows of editable cells. The schema allows only
// these shapes, so editing can change words and structure but never produce something the
// corpus cannot hold. Flags are highlights that stay attached to their words as the text changes.

import { Fragment } from "prosemirror-model";
import { EditorState, Plugin, PluginKey, Selection, TextSelection } from "prosemirror-state";
import { EditorView, Decoration, DecorationSet } from "prosemirror-view";
import { keymap } from "prosemirror-keymap";
import { baseKeymap, chainCommands } from "prosemirror-commands";
import { history, undo, redo } from "prosemirror-history";
import { tableEditing, isInTable, CellSelection } from "prosemirror-tables";
import { schema, derivedDepth, unitLead, rankOf, isRank, ALIGNMENTS, KINDS, MARK_NAMES, MARK_TYPES } from "./schema.js";
import * as tables from "./tables.js";
import { unitsFromHTML, unitsFromText } from "./paste.js";
import { findPlugin, findKey, findNext, findStatus, replaceOne, replaceAll } from "./find.js";

export { schema };
export { unitsFromHTML, unitsFromText } from "./paste.js";

// Unit text with formatting ranges -> text nodes carrying marks.
function formatted(value, ranges) {
  const textValue = value || "";
  if (!textValue) return [];
  const valid = (ranges || []).filter((r) => MARK_TYPES[r.type] && r.start < r.end && r.end <= textValue.length);
  const cuts = [...new Set([0, textValue.length, ...valid.flatMap((r) => [r.start, r.end])])].sort((a, b) => a - b);
  const nodes = [];
  for (let i = 0; i + 1 < cuts.length; i++) {
    const [from, to] = [cuts[i], cuts[i + 1]];
    const marks = valid.filter((r) => r.start <= from && r.end >= to).map((r) => schema.marks[MARK_TYPES[r.type]].create());
    nodes.push(schema.text(textValue.slice(from, to), marks));
  }
  return nodes;
}

const N = schema.nodes;

// A section number hangs in the margin, so its words (and their next lines) form one column
// starting at "(1)": the class says how wide the number is. The number may be a chip or words.
export const SECTION_LEAD = /^\d{1,3}[A-Z]?\.\s+/;

export function hangClass(kind, marker) {
  if (kind === "heading" || !marker || derivedDepth(marker) !== 0) return "";
  return `hang hang-${Math.min(Math.max(marker.trim().length, 2), 5)}`;
}

function hangDecorations(doc) {
  const decorations = [];
  doc.forEach((node, pos) => {
    if (node.type !== N.unit) return;
    const body = node.firstChild;
    const first = body.firstChild;
    const start = pos + 2;
    let lead = null;
    let end = start;
    if (first && first.type === N.marker) {
      [lead, end] = [first.attrs.value, start + 1];
    } else if (first && first.isText) {
      const match = SECTION_LEAD.exec(first.text);
      if (match) [lead, end] = [match[0].trim(), start + match[0].length];
    }
    const cls = hangClass(node.attrs.kind, lead);
    if (!cls) return;
    decorations.push(Decoration.node(pos + 1, pos + 1 + body.nodeSize, { class: cls }));
    decorations.push(first.type === N.marker ? Decoration.node(start, end, { class: "lead" }) : Decoration.inline(start, end, { class: "lead" }));
  });
  return DecorationSet.create(doc, decorations);
}

const hangPlugin = new Plugin({ props: { decorations: (state) => hangDecorations(state.doc) } });
const text = (value) => (value ? [schema.text(value)] : []);

// --- units <-> document ---------------------------------------------------------------------------

// One stored unit -> its node. `notes` hears about anything that could not be shown as stored.
export function unitNode(unit, notes = []) {
  if (unit.kind === "table") return tables.tableNode(unit, notes);
  const inline = [];
  if (unit.marker) inline.push(N.marker.create({ value: unit.marker, source: unit.marker_source || null }));
  inline.push(...formatted(unit.text || "", unit.marks));
  const noteNodes = (unit.notes || []).map((note) => N.note.create(null, text(note)));
  const align = ALIGNMENTS.includes(unit.align) ? unit.align : null;
  const kind = KINDS.includes(unit.kind) ? unit.kind : "paragraph";
  const attrs = { kind, level: Number.isInteger(unit.level) ? unit.level : null, align, rank: rankOf(kind, unit.rank) };
  return N.unit.create(attrs, [N.body.create(null, inline), ...noteNodes]);
}

export function toDoc(units, notes = []) {
  const blocks = units.map((unit) => unitNode(unit, notes));
  return N.doc.create(null, blocks.length ? blocks : [N.unit.create(null, [N.body.create()])]);
}

export function fromDoc(doc) {
  const units = [];
  doc.forEach((block) => {
    if (block.type === N.table_unit) {
      units.push(tables.tableUnit(block));
      return;
    }
    const body = block.firstChild;
    let marker = null;
    let words = "";
    const ranges = [];
    body.forEach((child) => {
      if (child.type === N.marker) { if (!marker) marker = child.attrs; return; }
      const start = words.length;
      words += child.text || "";
      for (const mark of child.marks) {
        const name = MARK_NAMES[mark.type.name];
        const last = ranges.findLast((r) => r.type === name && r.end === start);
        if (last) last.end = words.length;
        else ranges.push({ type: name, start, end: words.length });
      }
    });
    const lead = words.length - words.trimStart().length;
    const trimmed = words.trim();
    const unit = { kind: block.attrs.kind, text: trimmed };
    const rank = rankOf(block.attrs.kind, block.attrs.rank);
    if (rank) unit.rank = rank;
    const marks = ranges
      .map((r) => ({ type: r.type, start: Math.max(0, r.start - lead), end: Math.min(trimmed.length, r.end - lead) }))
      .filter((r) => r.start < r.end);
    if (marks.length) unit.marks = marks;
    if (marker && marker.value) { unit.marker = marker.value; if (marker.source) unit.marker_source = marker.source; }
    const notes = [];
    block.forEach((child, _, index) => { if (index > 0 && child.textContent.trim()) notes.push(child.textContent.trim()); });
    if (notes.length) unit.notes = notes;
    if (Number.isInteger(block.attrs.level)) unit.level = block.attrs.level;
    if (block.attrs.align) unit.align = block.attrs.align;
    units.push(unit);
  });
  return units;
}

// --- flags as decorations ----------------------------------------------------------------------------

const tokens = (value) => [...String(value || "").matchAll(/\S+/g)].map((m) => ({ start: m.index, end: m.index + m[0].length }));

function sameUnit(a, b) {
  if (!a || !b || a.kind !== b.kind) return false;
  if (a.kind === "table") return JSON.stringify(a.rows) === JSON.stringify(b.rows) && (a.caption || "") === (b.caption || "");
  return a.text === b.text && (a.marker || "") === (b.marker || "") && JSON.stringify(a.notes || []) === JSON.stringify(b.notes || []);
}

// Machine page tokens, in the server's order: number and text, then notes; tables: caption, cells.
function tokenMap(units) {
  const map = [];
  units.forEach((unit, u) => {
    if (unit.kind === "table") {
      tokens(unit.caption).forEach((t) => map.push({ u, field: "caption", ...t }));
      unit.rows.forEach((row, r) => row.forEach((cell, c) => tokens(cell).forEach((t) => map.push({ u, field: `cell:${r}:${c}`, ...t }))));
      return;
    }
    tokens(unit.marker).forEach((t) => map.push({ u, field: "marker", ...t }));
    tokens(unit.text).forEach((t) => map.push({ u, field: "text", ...t }));
    (unit.notes || []).forEach((note, n) => tokens(note).forEach((t) => map.push({ u, field: `note:${n}`, ...t })));
  });
  return map;
}

// Document position where a unit's text, or one of its margin notes, starts.
function fieldStart(doc, u, field) {
  if (u >= doc.childCount) return null;
  let pos = 0;
  for (let i = 0; i < u; i++) pos += doc.child(i).nodeSize;
  const block = doc.child(u);
  if (block.type !== N.unit) return null;
  const body = block.firstChild;
  if (field === "text") {
    const chip = body.firstChild && body.firstChild.type === N.marker ? 1 : 0;
    return pos + 2 + chip; // unit open, body open, then the number chip if any
  }
  const note = Number(field.split(":")[1]); // note n is child n + 1
  if (!(note + 1 < block.childCount)) return null;
  let inner = pos + 1 + body.nodeSize;
  for (let i = 1; i <= note; i++) inner += block.child(i).nodeSize;
  return inner + 1;
}

function unitStart(doc, u) {
  let pos = 0;
  for (let i = 0; i < u; i++) pos += doc.child(i).nodeSize;
  return pos;
}

function caret(flag, title, symbol) {
  return () => {
    const mark = document.createElement("span");
    mark.className = `gap ${flag.severity}`;
    mark.title = title;
    mark.dataset.flag = flag.id;
    mark.textContent = symbol;
    return mark;
  };
}

function initialDecorations(doc, flags, machineUnits, units) {
  const map = tokenMap(machineUnits);
  const decorations = [];
  for (const flag of flags) {
    const spec = { flag: flag.id, severity: flag.severity, kind: flag.kind };
    if (flag.kind === "marker_conflict") {
      // The check readings disagree on a number the main reading dropped: mark where it belongs.
      const u = units.findIndex((unit) => unit.kind !== "table" && unit.text && unit.text.startsWith(flag.primary));
      if (u >= 0 && u < doc.childCount && doc.child(u).type === N.unit) {
        decorations.push(Decoration.widget(unitStart(doc, u) + 2, caret(flag, "The check readings give different numbers here", "#?"), { ...spec, side: -1 }));
      }
      continue;
    }
    const [from, to] = flag.tokens;
    const anchor = from < map.length ? map[from] : map[map.length - 1];
    if (!anchor || anchor.field.startsWith("cell") || anchor.field === "caption") continue;
    if (!sameUnit(machineUnits[anchor.u], units[anchor.u])) continue; // edited since: listed, not highlighted
    if (anchor.field === "marker") {
      // A flag on a restored number: highlight the number chip itself.
      const at = unitStart(doc, anchor.u) + 2;
      const chip = doc.nodeAt(at);
      if (chip && chip.type === N.marker) decorations.push(Decoration.node(at, at + 1, { class: `flag ${flag.severity}`, "data-flag": flag.id }, spec));
      continue;
    }
    const start = fieldStart(doc, anchor.u, anchor.field);
    if (start === null) continue;
    if (from === to || flag.kind === "missing_from_primary") {
      const at = from < map.length ? start + anchor.start : start + (machineUnits[anchor.u].text || "").length;
      decorations.push(Decoration.widget(at, caret(flag, "Words only the check readings have here", "⁁"), { ...spec, side: -1 }));
      continue;
    }
    // A flag that runs into the next unit is highlighted where it starts; the pop-up shows it all.
    let last = from;
    while (last + 1 < to && map[last + 1] && map[last + 1].u === anchor.u && map[last + 1].field === anchor.field) last++;
    decorations.push(Decoration.inline(start + map[from].start, start + map[last].end, { class: `flag ${flag.severity}`, "data-flag": flag.id }, { ...spec, partial: last + 1 < to }));
  }
  return DecorationSet.create(doc, decorations);
}

const flagKey = new PluginKey("flags");

function flagPlugin(flags, machineUnits, units, onFlagClick) {
  return new Plugin({
    key: flagKey,
    state: {
      init: (_, state) => ({ set: initialDecorations(state.doc, flags, machineUnits, units), resolved: new Set() }),
      apply(tr, value) {
        const meta = tr.getMeta(flagKey);
        let set = value.set.map(tr.mapping, tr.doc);
        let resolved = value.resolved;
        if (meta && meta.resolved) {
          resolved = new Set([...resolved, meta.resolved]);
          set = set.remove(set.find(undefined, undefined, (spec) => spec.flag === meta.resolved));
        }
        if (meta && meta.reopened) {
          resolved = new Set([...resolved].filter((id) => id !== meta.reopened));
        }
        return { set, resolved };
      },
    },
    props: {
      decorations: (state) => flagKey.getState(state).set,
      handleClickOn(view, pos, node, nodePos, event) {
        const target = event.target.closest("[data-flag]");
        if (target) { onFlagClick(target.dataset.flag, target.getBoundingClientRect()); return false; }
        return false;
      },
    },
  });
}

// --- structure commands ---------------------------------------------------------------------------

function unitAt($pos) {
  for (let depth = $pos.depth; depth > 0; depth--) {
    const node = $pos.node(depth);
    if (node.type === N.unit || node.type === N.table_unit) return { node, depth, pos: $pos.before(depth) };
  }
  return null;
}

// Enter in a unit's words: the rest of the words become a new unit of the same kind and level. At
// the very start of the words, an empty unit opens above instead (a heading of the same rank), so
// the margin notes stay put.
function splitUnit(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.body) return false;
  const unit = unitAt($from);
  if (dispatch) {
    const { kind, level, rank } = unit.node.attrs;
    if ($from.parentOffset === 0 && $from.parent.content.size) {
      const tr = state.tr.insert(unit.pos, N.unit.create({ kind, level, rank }, [N.body.create()]));
      dispatch(tr.scrollIntoView());
      return true;
    }
    const rest = $from.parent.content.cut($from.parentOffset);
    const after = unit.pos + unit.node.nodeSize;
    const tr = state.tr.delete($from.pos, $from.end());
    const fresh = N.unit.create({ kind: kind === "heading" ? "paragraph" : kind, level }, [N.body.create(null, trimStart(rest))]);
    const at = tr.mapping.map(after);
    tr.insert(at, fresh);
    tr.setSelection(TextSelection.create(tr.doc, at + 2));
    dispatch(tr.scrollIntoView());
  }
  return true;
}

function trimStart(fragment) {
  const first = fragment.firstChild;
  if (first && first.isText && /^\s/.test(first.text)) {
    const trimmed = first.text.replace(/^\s+/, "");
    return trimmed ? fragment.replaceChild(0, schema.text(trimmed, first.marks)) : fragment.cut(first.nodeSize);
  }
  return fragment;
}

// Enter in a margin note: a new note for the same unit.
function splitNote(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.note) return false;
  if (dispatch) {
    const tr = state.tr.split($from.pos);
    dispatch(tr.scrollIntoView());
  }
  return true;
}

const notesOf = (unit) => {
  const notes = [];
  unit.forEach((child, _, i) => { if (i > 0) notes.push(child); });
  return notes;
};
const isEmptyUnit = (node) => node.type === N.unit && node.childCount === 1 && !node.firstChild.content.size;

// A number chip can only stand at the start of a unit's words (the stored unit has one number, before
// its text): moved anywhere else, it becomes words, so nothing is lost when the page is saved.
function chipsAsWords(fragment) {
  const pieces = [];
  fragment.forEach((child) => pieces.push(child.type === N.marker ? schema.text(`${child.attrs.value} `) : child));
  return Fragment.fromArray(pieces);
}

const hasChip = (fragment) => { let found = false; fragment.forEach((child) => { found ||= child.type === N.marker; }); return found; };

// The unit at `index` joins the one before it: its words after a space, its notes after that unit's
// notes. A number chip cannot stand inside words (it would be lost when saved), so it becomes words.
// An empty unit before it simply goes.
function joinUnits(state, index, dispatch) {
  const previous = state.doc.child(index - 1), unit = state.doc.child(index);
  const unitPos = unitStart(state.doc, index), previousPos = unitPos - previous.nodeSize;
  if (!dispatch) return true;
  if (isEmptyUnit(previous)) {
    const tr = state.tr.delete(previousPos, unitPos);
    dispatch(tr.setSelection(TextSelection.create(tr.doc, previousPos + 2)).scrollIntoView());
    return true;
  }
  const previousBody = previous.firstChild;
  const previousBodyEnd = previousPos + previousBody.nodeSize;
  const words = trimStart(chipsAsWords(unit.firstChild.content));
  const last = previousBody.lastChild;
  const needsSpace = previousBody.content.size > 0 && words.size > 0 && !(last && last.isText && /\s$/.test(last.text));
  const tr = state.tr.delete(unitPos, unitPos + unit.nodeSize);
  tr.insert(previousBodyEnd, needsSpace ? Fragment.from(schema.text(" ")).append(words) : words);
  const notes = notesOf(unit);
  if (notes.length) tr.insert(tr.mapping.map(unitPos) - 1, notes);
  tr.setSelection(TextSelection.create(tr.doc, previousBodyEnd + (needsSpace ? 1 : 0)));
  dispatch(tr.scrollIntoView());
  return true;
}

// Backspace at the start of a unit's words joins it to the unit before. After a table, an empty
// unit goes and the cursor moves into the table; a unit with words stays.
function joinBackward(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.body || $from.parentOffset !== 0) return false;
  const index = $from.index(0);
  if (index === 0) return true;
  if (state.doc.child(index - 1).type === N.unit) return joinUnits(state, index, dispatch);
  if (isEmptyUnit(state.doc.child(index)) && dispatch) {
    const at = unitStart(state.doc, index);
    const tr = state.tr.delete(at, at + state.doc.child(index).nodeSize);
    dispatch(tr.setSelection(Selection.near(tr.doc.resolve(at), -1)).scrollIntoView());
  }
  return true;
}

// Delete at the end of a unit's words: the next unit's words join them, as Backspace does from the
// other side. The margin notes beside a unit never run into its words.
function joinForward(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.body || $from.parentOffset !== $from.parent.content.size) return false;
  const index = $from.index(0);
  const next = state.doc.childCount > index + 1 ? state.doc.child(index + 1) : null;
  if (next && next.type === N.unit) {
    // The cursor stays where it was, before the words that joined (an empty unit simply goes).
    if (isEmptyUnit(state.doc.child(index))) return joinUnits(state, index + 1, dispatch);
    return joinUnits(state, index + 1, dispatch && ((tr) => dispatch(tr.setSelection(TextSelection.create(tr.doc, $from.pos)))));
  }
  if (next && isEmptyUnit(state.doc.child(index)) && dispatch) {
    const at = unitStart(state.doc, index);
    const tr = state.tr.delete(at, at + state.doc.child(index).nodeSize);
    dispatch(tr.setSelection(Selection.near(tr.doc.resolve(at), 1)).scrollIntoView());
  }
  return true;
}

// Backspace at the start of a margin note: an empty note goes; a note joins the note above it; the
// first note stays where it is (its words never run into the unit's words).
function noteBackward(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.note || $from.parentOffset !== 0) return false;
  const note = $from.parent, start = $from.before();
  if (!dispatch) return true;
  if (!note.content.size) {
    const tr = state.tr.delete(start, start + note.nodeSize);
    dispatch(tr.setSelection(Selection.near(tr.doc.resolve(start), -1)).scrollIntoView());
  } else if ($from.index($from.depth - 1) > 1) {
    const previous = $from.node($from.depth - 1).child($from.index($from.depth - 1) - 1);
    const tr = state.tr.join(start);
    if (previous.content.size) tr.insertText(" ", start - 1);
    dispatch(tr.setSelection(TextSelection.create(tr.doc, start - 1 + (previous.content.size ? 1 : 0))).scrollIntoView());
  }
  return true;
}

// Delete at the end of a margin note: the next note joins it; the last note stays as it is.
function noteForward(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.note || $from.parentOffset !== $from.parent.content.size) return false;
  const unit = $from.node($from.depth - 1), index = $from.index($from.depth - 1);
  if (index + 1 < unit.childCount && dispatch) {
    const tr = state.tr.join($from.after());
    if ($from.parent.content.size && unit.child(index + 1).content.size) tr.insertText(" ", $from.pos);
    dispatch(tr.setSelection(TextSelection.create(tr.doc, $from.pos)).scrollIntoView());
  }
  return true;
}

// Applies to every paragraph the selection touches (Ctrl/Cmd+A then a button changes the page).
function setUnitAttrs(change) {
  return (state, dispatch) => {
    const { from, to } = state.selection;
    const tr = state.tr;
    state.doc.nodesBetween(from, to, (node, pos) => {
      if (node.type === N.unit) {
        const attrs = change(node.attrs, node);
        if (attrs) tr.setNodeMarkup(pos, null, attrs);
      }
      return false;
    });
    if (!tr.docChanged) return false;
    if (dispatch) dispatch(tr);
    return true;
  };
}

// --- formatting --------------------------------------------------------------------------------------

// The runs of text in the selection a mark can go on: a unit's words, never a number chip, a margin
// note, a caption or a table cell.
function formattable(state, type) {
  const runs = [];
  for (const { $from, $to } of state.selection.ranges) {
    state.doc.nodesBetween($from.pos, $to.pos, (node, pos, parent) => {
      if (!node.isText || !parent.type.allowsMarkType(type)) return;
      const from = Math.max(pos, $from.pos), to = Math.min(pos + node.nodeSize, $to.pos);
      if (from < to) runs.push({ from, to, text: node.text.slice(from - pos, to - pos), marks: node.marks });
    });
  }
  return runs;
}

// Bold, italic and the rest, as in a word processor: over a selection the mark is added unless
// every word already has it (then it is removed); at the cursor it applies to the words typed next.
// Spaces at either end of the selection are left as they are.
export function toggleFormat(type) {
  return (state, dispatch) => {
    const { empty, $cursor } = state.selection;
    if (empty) {
      if (!$cursor || !$cursor.parent.type.allowsMarkType(type)) return false;
      if (dispatch) dispatch(type.isInSet(state.storedMarks || $cursor.marks()) ? state.tr.removeStoredMark(type) : state.tr.addStoredMark(type.create()));
      return true;
    }
    const runs = formattable(state, type);
    const words = runs.filter((run) => run.text.trim());
    if (!words.length) return false;
    if (dispatch) {
      const tr = state.tr;
      if (words.every((run) => type.isInSet(run.marks))) {
        runs.forEach((run) => tr.removeMark(run.from, run.to, type));
      } else {
        const first = words[0], last = words[words.length - 1];
        for (const run of runs.slice(runs.indexOf(first), runs.indexOf(last) + 1)) {
          const from = run === first ? run.from + run.text.length - run.text.trimStart().length : run.from;
          const to = run === last ? run.to - (run.text.length - run.text.trimEnd().length) : run.to;
          if (from < to) tr.addMark(from, to, type.create());
        }
      }
      dispatch(tr.scrollIntoView());
    }
    return true;
  };
}

// For the toolbar: whether each mark can be used here and whether it is on (at the cursor, or on
// every word of the selection).
function formatState(state) {
  const out = {};
  const { empty, $cursor } = state.selection;
  for (const [name, type] of Object.entries(schema.marks)) {
    if (empty) {
      const can = Boolean($cursor && $cursor.parent.type.allowsMarkType(type));
      out[name] = { can, on: can && Boolean(type.isInSet(state.storedMarks || $cursor.marks())) };
    } else {
      const words = formattable(state, type).filter((run) => run.text.trim());
      out[name] = { can: words.length > 0, on: words.length > 0 && words.every((run) => type.isInSet(run.marks)) };
    }
  }
  return out;
}

// --- pasting -----------------------------------------------------------------------------------------

// Units pasted into a unit's words: the first one's words join the words before the cursor, the
// others follow as units of their own, and the words after the cursor end the last one. The margin
// notes stay beside the unit they were beside, as with Enter. Returns null where units cannot go.
export function pasteUnits(state, blocks, open = { start: true, end: true }) {
  const tr = state.tr;
  if (!tr.selection.empty) tr.deleteSelection();
  const $at = tr.selection.$from;
  if ($at.parent.type !== N.body || $at.depth !== 2 || !blocks.length) return null;
  const since = tr.steps.length;
  const map = (pos, assoc = 1) => tr.mapping.slice(since).map(pos, assoc);
  const unit = $at.node(1), unitPos = $at.before(1), cursor = $at.pos;
  const list = blocks.slice();
  const first = open.start && list[0].type === N.unit ? list.shift() : null;
  const last = open.end && list.length && list[list.length - 1].type === N.unit ? list.pop() : null;
  const tail = $at.parent.content.cut($at.parentOffset);
  const after = [...list];
  const lastWords = last && last.firstChild.content;
  if (last) after.push(N.unit.create(last.attrs, [N.body.create(null, lastWords.append(lastWords.size ? chipsAsWords(tail) : tail)), ...notesOf(last)]));
  else if (after.length && tail.size) {
    const kind = unit.attrs.kind === "heading" ? "paragraph" : unit.attrs.kind;
    after.push(N.unit.create({ kind, level: unit.attrs.level }, [N.body.create(null, trimStart(tail))]));
  }
  if (after.length) tr.delete(cursor, $at.end());
  let words = first ? first.firstChild.content : Fragment.empty;
  if ($at.parentOffset > 0 || hasChip($at.parent.content)) words = chipsAsWords(words);
  // Words pasted straight after the end of a sentence or clause get a space, as in a word processor.
  const before = $at.nodeBefore, start = words.firstChild;
  if (before && before.isText && /[.;:!?,)\]]$/.test(before.text) && start && start.isText && /^[\p{L}\p{N}("“]/u.test(start.text)) {
    words = Fragment.from(schema.text(" ", start.marks)).append(words);
  }
  tr.insert(cursor, words);
  let caret = cursor + words.size;
  if (first && notesOf(first).length) tr.insert(map(unitPos + unit.nodeSize) - 1, notesOf(first));
  if (after.length) {
    const end = map(unitPos + unit.nodeSize);
    tr.insert(end, after);
    // The cursor goes where the pasted words end: in the last pasted unit, before the moved words.
    const pasted = last || !tail.size ? after.length - 1 : after.length - 2;
    let start = end;
    for (const node of after.slice(0, pasted)) start += node.nodeSize;
    const node = after[pasted];
    caret = last ? start + 2 + last.firstChild.content.size : node.type === N.unit ? start + node.firstChild.nodeSize : start + node.nodeSize - 1;
    const left = tr.doc.nodeAt(map(unitPos));
    if (isEmptyUnit(left)) {
      const steps = tr.steps.length;
      tr.delete(map(unitPos), map(unitPos) + left.nodeSize);
      caret = tr.mapping.slice(steps).map(caret);
    }
  }
  return tr.setSelection(Selection.near(tr.doc.resolve(caret), -1));
}

const chipText = (node) => (node.type === N.marker ? node.attrs.value : "");
const wordsOf = (node) => (node.isText ? node.text : node.isLeaf ? chipText(node) : node.textBetween(0, node.content.size, " ", chipText));
const plain = (blocks) => blocks.map(wordsOf).join(" ").replace(/\s+/g, " ").trim();

// Pasting: within one unit's words, copied words go in as they are (formatting and chips kept);
// several units, or anything from outside the page, are cleaned into units (paste.js). A margin
// note, a caption or a cell takes the words as plain text. Cells pasted into a table are placed by
// the table plugin before this runs.
const pastePlugin = new Plugin({
  props: {
    handlePaste(view, event, slice) {
      const data = event.clipboardData;
      const html = data ? data.getData("text/html") : "";
      const { state } = view;
      const $from = state.selection.$from;
      let blocks, open = { start: true, end: true };
      if (/data-pm-slice/.test(html)) {
        blocks = [];
        slice.content.forEach((node) => blocks.push(node));
        if (!blocks.every((node) => node.type === N.unit || node.type === N.table_unit)) return pasteText(view, plain(blocks));
        const inline = blocks.length === 1 && blocks[0].type === N.unit && blocks[0].childCount === 1 && slice.openStart >= 2 && slice.openEnd >= 2;
        if (inline && $from.parent.type === N.body) return false;
        open = { start: slice.openStart >= 2, end: slice.openEnd >= 2 };
      } else {
        const unit = $from.depth ? state.doc.child($from.index(0)) : null;
        const kind = unit && unit.type === N.unit ? unit.attrs.kind : "paragraph";
        const units = html ? unitsFromHTML(html) : unitsFromText(data ? data.getData("text/plain") : "", kind);
        blocks = units.map((unit) => unitNode(unit));
      }
      if (!blocks.length) return true;
      const tr = $from.parent.type === N.body || !state.selection.empty ? pasteUnits(state, blocks, open) : null;
      if (!tr) return pasteText(view, plain(blocks));
      view.dispatch(tr.scrollIntoView().setMeta("uiEvent", "paste"));
      return true;
    },
  },
});

function pasteText(view, words) {
  if (words) view.dispatch(view.state.tr.insertText(words).scrollIntoView().setMeta("uiEvent", "paste"));
  return true;
}

// --- commands ----------------------------------------------------------------------------------------

// The ranks a reviewer chooses from (the toolbar's Heading menu, and Ctrl/Cmd+Alt+1 pressed again on
// a heading); a reading may also give 5 or 6, which are kept and shown.
export const RANKS = [1, 2, 3, 4];

// Ctrl/Cmd+Alt+1 on headings: the next rank down (1, 2, 3, 4, then 1 again), the same for every
// heading selected, from the first one's (an unranked heading becomes 1). Anything else selected
// that is not a heading: it does not apply, and the key makes them headings.
function stepRank(state, dispatch) {
  const ranks = [];
  state.doc.nodesBetween(state.selection.from, state.selection.to, (node) => {
    if (node.type === N.unit) ranks.push(node.attrs.kind === "heading" ? rankOf("heading", node.attrs.rank) ?? 0 : -1);
    return false;
  });
  if (!ranks.length || ranks.includes(-1)) return false;
  const next = RANKS.find((rank) => rank > ranks[0]) ?? RANKS[0];
  return commands.rank(next)(state, dispatch);
}

export const commands = {
  mark: (type) => toggleFormat(schema.marks[type]),
  // Only a heading has a rank: one that becomes a paragraph or list item loses it.
  kind: (kind) => setUnitAttrs((attrs) => ({ ...attrs, kind, rank: rankOf(kind, attrs.rank) })),
  // A heading of rank 1 (the title) to 6, as printed: the units selected become headings of it.
  rank: (rank) => (isRank(rank) ? setUnitAttrs((attrs) => ({ ...attrs, kind: "heading", rank })) : () => false),
  stepRank,
  // Alignment as printed; choosing the kind's usual alignment clears the setting.
  align: (align) => setUnitAttrs((attrs) => ({ ...attrs, align: align === (attrs.kind === "heading" ? "center" : "left") ? null : align })),
  level: (delta) => setUnitAttrs((attrs, node) => {
    const current = attrs.level ?? derivedDepth(unitLead(node)) ?? 0;
    const level = Math.max(0, Math.min(3, current + delta));
    return { ...attrs, level };
  }),
  // Move the margin note under the cursor to the next (1) or previous (-1) paragraph.
  moveNote: (direction) => (state, dispatch) => {
    const { $from } = state.selection;
    if ($from.parent.type !== N.note) return false;
    const index = $from.index(0);
    let target = index + direction;
    while (target >= 0 && target < state.doc.childCount && state.doc.child(target).type !== N.unit) target += direction;
    if (target < 0 || target >= state.doc.childCount) return true;
    if (dispatch) {
      const note = $from.parent;
      const noteStart = $from.before($from.depth);
      const offset = $from.parentOffset;
      let tr = state.tr.delete(noteStart, noteStart + note.nodeSize);
      let pos = 0;
      for (let i = 0; i < target; i++) pos += tr.doc.child(i).nodeSize;
      const destination = pos + tr.doc.child(target).nodeSize - 1; // end of that unit: after its notes
      tr = tr.insert(destination, note);
      tr.setSelection(TextSelection.create(tr.doc, destination + 1 + Math.min(offset, note.content.size)));
      dispatch(tr.scrollIntoView());
    }
    return true;
  },
  addNote: (state, dispatch) => {
    const unit = unitAt(state.selection.$from);
    if (!unit || unit.node.type !== N.unit) return false;
    if (dispatch) {
      const at = unit.pos + unit.node.nodeSize - 1;
      const tr = state.tr.insert(at, N.note.create());
      tr.setSelection(TextSelection.create(tr.doc, at + 1));
      dispatch(tr.scrollIntoView());
    }
    return true;
  },
  insertTable: tables.insertTable,
  addRowBefore: tables.addRowBefore,
  addRowAfter: tables.addRowAfter,
  addColumnBefore: tables.addColumnBefore,
  addColumnAfter: tables.addColumnAfter,
  deleteRow: tables.deleteRow,
  deleteColumn: tables.deleteColumn,
  mergeCells: tables.mergeCells,
  splitCell: tables.splitCell,
  toggleHeaderRow: tables.toggleHeaderRow,
  deleteTable: tables.deleteTable,
  // The names the toolbar used before: a row below, a column to the right.
  addRow: tables.addRowAfter,
  addColumn: tables.addColumnAfter,
  findNext,
  replaceOne,
  replaceAll,
  undo,
  redo,
};

// Keys within the page; app.js lists them under "Keys".
export const keyCommands = {
  Enter: chainCommands(splitNote, splitUnit, tables.enterInTable, () => true),
  "Shift-Enter": () => true,
  Backspace: chainCommands(noteBackward, joinBackward),
  Delete: chainCommands(noteForward, joinForward),
  Tab: chainCommands(tables.nextCell(1), commands.level(1)),
  "Shift-Tab": chainCommands(tables.nextCell(-1), commands.level(-1)),
  "Mod-z": undo,
  "Mod-y": redo,
  "Shift-Mod-z": redo,
  "Mod-Alt-1": chainCommands(stepRank, commands.kind("heading")),
  "Mod-Alt-2": commands.kind("paragraph"),
  "Mod-Alt-3": commands.kind("list_item"),
  "Mod-Alt-n": commands.addNote,
  "Alt-Shift-ArrowDown": commands.moveNote(1),
  "Alt-Shift-ArrowUp": commands.moveNote(-1),
  "Mod-b": toggleFormat(schema.marks.strong),
  "Mod-i": toggleFormat(schema.marks.em),
  "Mod-u": toggleFormat(schema.marks.underline),
  "Mod-.": toggleFormat(schema.marks.sup),
  "Mod-,": toggleFormat(schema.marks.sub),
  "Mod-Alt-l": commands.align("left"),
  "Mod-Alt-e": commands.align("center"),
  "Mod-Alt-r": commands.align("right"),
  "Mod-Alt-j": commands.align("justify"),
  "Mod-g": findNext(1),
  "Shift-Mod-g": findNext(-1),
};

// The plugins of a page, without the view (tests build states with them).
export function plugins(extra = []) {
  return [history(), keymap(keyCommands), keymap(baseKeymap), tableEditing(), tables.headerRows, pastePlugin, hangPlugin, findPlugin, ...extra];
}

// --- the editor ------------------------------------------------------------------------------------

export function createEditor(mount, { units, machineUnits, flags, onChange, onFlagClick, onUnitFocus, onSelect, onMarkerEdit, onFind, onEscape, onNotice, label = "Page text", editable = true }) {
  // Keys that reach beyond the page: Ctrl/Cmd+F opens the find bar, Escape (and Alt+F10, as in a
  // word processor) moves to the toolbar, so the keyboard is never trapped in the text.
  const outside = keymap({
    "Mod-f": () => Boolean(onFind && (onFind(), true)),
    Escape: () => Boolean(onEscape && (onEscape(), true)),
    "Alt-F10": () => Boolean(onEscape && (onEscape(), true)),
  });
  let lastUnit = -1;
  const notes = [];
  const state = EditorState.create({ doc: toDoc(units, notes), plugins: plugins([outside, flagPlugin(flags, machineUnits, units, onFlagClick)]) });
  const view = new EditorView(mount, {
    state,
    editable: () => editable,
    attributes: { role: "textbox", "aria-multiline": "true", "aria-label": label },
    dispatchTransaction(tr) {
      const next = view.state.apply(tr);
      view.updateState(next);
      if (tr.docChanged) onChange && onChange();
      const index = next.selection.$from.depth ? next.selection.$from.index(0) : -1;
      if (index !== lastUnit) { lastUnit = index; onUnitFocus && onUnitFocus(index); }
      // Formatting turned on at the cursor changes only the stored marks: the toolbar shows it too.
      if (tr.selectionSet || tr.docChanged || tr.storedMarksSet) onSelect && onSelect(index);
    },
    handleDoubleClickOn(view2, pos, node, nodePos) {
      if (node.type === N.marker && onMarkerEdit) {
        onMarkerEdit(node.attrs.value, (value) => {
          const tr = value ? view2.state.tr.setNodeMarkup(nodePos, null, { value, source: null }) : view2.state.tr.delete(nodePos, nodePos + 1);
          view2.dispatch(tr);
        });
        return true;
      }
      return false;
    },
  });
  if (notes.length && onNotice) onNotice(notes.join("; "));

  function flagRange(id) {
    const found = flagKey.getState(view.state).set.find(undefined, undefined, (spec) => spec.flag === id);
    return found.length ? found[0] : null;
  }

  return {
    view,
    units: () => fromDoc(view.state.doc),
    run(command, { focus = true } = {}) { const done = command(view.state, view.dispatch); if (focus) view.focus(); return done; },
    // Replace the flagged words with a check reading's words (or remove / insert them).
    applyReading(id, words) {
      const range = flagRange(id);
      if (!range) return false;
      const tr = view.state.tr;
      if (range.spec.kind === "marker_conflict") {
        // Set the chosen number as the unit's number chip.
        const $at = view.state.doc.resolve(range.from);
        const bodyStart = $at.start($at.depth);
        const first = view.state.doc.nodeAt(bodyStart);
        const chip = N.marker.create({ value: words, source: null });
        if (first && first.type === N.marker) tr.replaceWith(bodyStart, bodyStart + 1, chip);
        else tr.insert(bodyStart, chip);
      } else if (range.spec.partial) {
        return false; // spans units: correct by hand
      } else if (range.from === range.to) tr.insertText(`${words} `, range.from);
      else if (words) tr.insertText(words, range.from, range.to);
      else tr.delete(range.from, range.to);
      tr.setMeta(flagKey, { resolved: id });
      view.dispatch(tr);
      return true;
    },
    resolve(id) { view.dispatch(view.state.tr.setMeta(flagKey, { resolved: id })); },
    reveal(id) {
      const range = flagRange(id);
      if (!range) return false;
      view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, range.from)).scrollIntoView());
      view.focus();
      return true;
    },
    highlighted: (id) => Boolean(flagRange(id)),
    inNote: () => view.state.selection.$from.parent.type === N.note,
    // What the toolbar shows: which buttons apply where the cursor is, and which are on.
    status() {
      const st = view.state;
      const { $from } = st.selection;
      const kinds = new Set(), aligns = new Set(), ranks = new Set();
      st.doc.nodesBetween(st.selection.from, st.selection.to, (node) => {
        if (node.type === N.unit) {
          kinds.add(node.attrs.kind);
          aligns.add(node.attrs.align || (node.attrs.kind === "heading" ? "center" : "left"));
          ranks.add(rankOf(node.attrs.kind, node.attrs.rank));
        }
        return false;
      });
      const can = (command) => Boolean(editable && command(st));
      return {
        formats: formatState(st),
        kind: kinds.size === 1 ? [...kinds][0] : null,
        // The rank of the headings selected when they all have the same one; null otherwise.
        rank: ranks.size === 1 ? [...ranks][0] : null,
        align: aligns.size === 1 ? [...aligns][0] : null,
        inNote: $from.parent.type === N.note,
        inTable: isInTable(st) || st.selection instanceof CellSelection,
        headerRow: tables.inHeaderRow(st),
        can: Object.fromEntries(["addRowBefore", "addRowAfter", "addColumnBefore", "addColumnAfter", "deleteRow", "deleteColumn", "mergeCells", "splitCell", "toggleHeaderRow", "deleteTable", "addNote", "insertTable"]
          .map((name) => [name, can(commands[name])])),
        find: findStatus(st),
      };
    },
    // Find on the page: the query and options, then how many matches and which is selected.
    find(query, options = {}) {
      view.dispatch(view.state.tr.setMeta(findKey, { query, options }));
      return findStatus(view.state);
    },
    findStatus: () => findStatus(view.state),
    // The words around the cursor, to find the printed line it sits on.
    caretContext() {
      const { $from } = view.state.selection;
      const text = $from.parent.textContent;
      const at = Math.min($from.parentOffset, text.length);
      return { index: $from.depth ? $from.index(0) : -1, before: text.slice(Math.max(0, at - 40), at), after: text.slice(at, at + 40) };
    },
    // Open flags in document order, with the DOM element that shows each one.
    openFlags() {
      return flagKey.getState(view.state).set.find().sort((a, b) => a.from - b.from).map((d) => ({ id: d.spec.flag, from: d.from }));
    },
    flagElement(id) {
      return view.dom.querySelector(`[data-flag="${CSS.escape(id)}"]`);
    },
    // Whether a check reading can be applied to this flag with one click.
    applicable(id) {
      const range = flagRange(id);
      return Boolean(range && !range.spec.partial);
    },
    focusUnit(index) {
      if (index < 0 || index >= view.state.doc.childCount) return;
      let pos = 0;
      for (let i = 0; i < index; i++) pos += view.state.doc.child(i).nodeSize;
      view.dispatch(view.state.tr.setSelection(TextSelection.near(view.state.doc.resolve(pos + 2))).scrollIntoView());
      view.focus();
    },
    destroy: () => view.destroy(),
  };
}
