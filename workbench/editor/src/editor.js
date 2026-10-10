// Structured document surface for the review workbench.
//
// The page is edited in place, laid out as printed: each unit (heading, paragraph, list item) is
// a block with its words and, beside it, its margin notes; section and paragraph numbers the
// readers supplied are small chips; tables are rows of editable cells. The schema allows only
// these shapes, so editing can change words and structure but never produce something the
// corpus cannot hold. Flags are highlights that stay attached to their words as the text changes.

import { Schema, Fragment } from "prosemirror-model";
import { EditorState, Plugin, PluginKey, TextSelection } from "prosemirror-state";
import { EditorView, Decoration, DecorationSet } from "prosemirror-view";
import { keymap } from "prosemirror-keymap";
import { baseKeymap } from "prosemirror-commands";
import { history, undo, redo } from "prosemirror-history";

const DEPTH = [
  [/^\d{1,3}[A-Z]?\./, 0],
  [/^\(\d{1,3}[A-Z]?\)/, 1],
  [/^\([ivxl]{2,6}\)/, 3],
  [/^\([a-z]{1,2}\)/, 2],
];

function derivedDepth(lead) {
  const text = (lead || "").trimStart();
  for (const [pattern, depth] of DEPTH) if (pattern.test(text)) return depth;
  return null;
}

function unitLead(node) {
  const body = node.firstChild;
  let marker = "";
  body.forEach((child) => { if (child.type.name === "marker" && !marker) marker = child.attrs.value; });
  return marker || body.textContent;
}

export const schema = new Schema({
  nodes: {
    doc: { content: "block+" },
    unit: {
      group: "block",
      content: "body note*",
      attrs: { kind: { default: "paragraph" }, level: { default: null } },
      toDOM(node) {
        const level = node.attrs.level ?? derivedDepth(unitLead(node));
        const cls = ["u", node.attrs.kind, level === null ? "" : `lvl${level}`, node.childCount > 1 ? "has-notes" : ""];
        return ["div", { class: cls.join(" ").trim() }, 0];
      },
    },
    body: { content: "inline*", toDOM: () => ["div", { class: "body" }, 0] },
    note: { content: "text*", marks: "", toDOM: () => ["aside", { class: "note" }, 0] },
    marker: {
      inline: true,
      group: "inline",
      atom: true,
      selectable: true,
      attrs: { value: {}, source: { default: null } },
      toDOM(node) {
        const source = node.attrs.source ? `Number restored from ${[].concat(node.attrs.source).join(", ")}` : "Section or paragraph number";
        return ["span", { class: "marker", title: `${source}. Double-click to change.` }, node.attrs.value];
      },
    },
    table_unit: {
      group: "block",
      content: "table_caption table_row+",
      attrs: { header_rows: { default: 0 } },
      toDOM: () => ["div", { class: "tbl" }, 0],
    },
    table_caption: { content: "text*", marks: "", toDOM: () => ["div", { class: "caption" }, 0] },
    table_row: { content: "table_cell+", toDOM: () => ["div", { class: "tr" }, 0] },
    table_cell: { content: "text*", marks: "", toDOM: () => ["div", { class: "td" }, 0] },
    text: { group: "inline" },
  },
  marks: {},
});

const N = schema.nodes;
const text = (value) => (value ? [schema.text(value)] : []);

// --- units <-> document ---------------------------------------------------------------------------

export function toDoc(units) {
  const blocks = units.map((unit) => {
    if (unit.kind === "table") {
      const rows = (unit.rows && unit.rows.length ? unit.rows : [[""]]).map((row) =>
        N.table_row.create(null, (row.length ? row : [""]).map((cell) => N.table_cell.create(null, text(cell)))));
      return N.table_unit.create({ header_rows: unit.header_rows || 0 }, [N.table_caption.create(null, text(unit.caption || "")), ...rows]);
    }
    const inline = [];
    if (unit.marker) inline.push(N.marker.create({ value: unit.marker, source: unit.marker_source || null }));
    inline.push(...text(unit.text || ""));
    const notes = (unit.notes || []).map((note) => N.note.create(null, text(note)));
    return N.unit.create({ kind: unit.kind, level: Number.isInteger(unit.level) ? unit.level : null }, [N.body.create(null, inline), ...notes]);
  });
  return N.doc.create(null, blocks.length ? blocks : [N.unit.create(null, [N.body.create()])]);
}

export function fromDoc(doc) {
  const units = [];
  doc.forEach((block) => {
    if (block.type === N.table_unit) {
      const rows = [];
      let caption = "";
      block.forEach((child) => {
        if (child.type === N.table_caption) caption = child.textContent;
        else { const row = []; child.forEach((cell) => row.push(cell.textContent)); rows.push(row); }
      });
      units.push({ kind: "table", caption, header_rows: Math.min(block.attrs.header_rows, rows.length), rows });
      return;
    }
    const body = block.firstChild;
    let marker = null;
    let words = "";
    body.forEach((child) => {
      if (child.type === N.marker) { if (!marker) marker = child.attrs; }
      else words += child.text || "";
    });
    const unit = { kind: block.attrs.kind, text: words.trim() };
    if (marker && marker.value) { unit.marker = marker.value; if (marker.source) unit.marker_source = marker.source; }
    const notes = [];
    block.forEach((child, _, index) => { if (index > 0 && child.textContent.trim()) notes.push(child.textContent.trim()); });
    if (notes.length) unit.notes = notes;
    if (Number.isInteger(block.attrs.level)) unit.level = block.attrs.level;
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

function initialDecorations(doc, flags, machineUnits, units) {
  const map = tokenMap(machineUnits);
  const decorations = [];
  for (const flag of flags) {
    const [from, to] = flag.tokens;
    const anchor = from < map.length ? map[from] : map[map.length - 1];
    if (!anchor || anchor.field === "marker" || anchor.field.startsWith("cell") || anchor.field === "caption") continue;
    if (!sameUnit(machineUnits[anchor.u], units[anchor.u])) continue; // edited since: listed, not highlighted
    let span = true;
    for (let i = from; i < to; i++) if (!map[i] || map[i].u !== anchor.u || map[i].field !== anchor.field) span = false;
    if (!span) continue;
    const start = fieldStart(doc, anchor.u, anchor.field);
    if (start === null) continue;
    const spec = { flag: flag.id, severity: flag.severity };
    if (from === to || flag.kind === "missing_from_primary") {
      const at = from < map.length ? start + anchor.start : start + (machineUnits[anchor.u].text || "").length;
      decorations.push(Decoration.widget(at, () => {
        const caret = document.createElement("span");
        caret.className = `gap ${flag.severity}`;
        caret.title = "Words only the check readings have here";
        caret.dataset.flag = flag.id;
        caret.textContent = "⁁";
        return caret;
      }, { ...spec, side: -1 }));
    } else {
      decorations.push(Decoration.inline(start + map[from].start, start + map[to - 1].end, { class: `flag ${flag.severity}`, "data-flag": flag.id }, spec));
    }
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

// Enter in a unit's words: the rest of the words become a new unit of the same kind and level.
function splitUnit(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.body) return false;
  const unit = unitAt($from);
  if (dispatch) {
    const rest = $from.parent.content.cut($from.parentOffset);
    const after = unit.pos + unit.node.nodeSize;
    const tr = state.tr.delete($from.pos, $from.end());
    const fresh = N.unit.create({ kind: unit.node.attrs.kind === "heading" ? "paragraph" : unit.node.attrs.kind, level: unit.node.attrs.level }, [N.body.create(null, trimStart(rest))]);
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
    return trimmed ? fragment.replaceChild(0, schema.text(trimmed)) : fragment.cut(first.nodeSize);
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

// Backspace at the start of a unit's words: join it to the unit before (words and notes).
function joinBackward(state, dispatch) {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.body || $from.parentOffset !== 0) return false;
  const unit = unitAt($from);
  const index = $from.index(0);
  if (index === 0) return true;
  const previous = state.doc.child(index - 1);
  if (previous.type !== N.unit) return true;
  if (dispatch) {
    const previousPos = unit.pos - previous.nodeSize;
    const previousBodyEnd = previousPos + 1 + previous.firstChild.nodeSize - 1;
    const words = unit.node.firstChild.content;
    const notes = [];
    unit.node.forEach((child, _, i) => { if (i > 0) notes.push(child); });
    const tr = state.tr.delete(unit.pos, unit.pos + unit.node.nodeSize);
    const needsSpace = previous.firstChild.textContent && words.size;
    const joined = needsSpace ? Fragment.from(schema.text(" ")).append(words) : words;
    tr.insert(previousBodyEnd, joined);
    if (notes.length) tr.insert(tr.mapping.map(previousPos + previous.nodeSize) - 1, notes);
    tr.setSelection(TextSelection.create(tr.doc, previousBodyEnd + (needsSpace ? 1 : 0)));
    dispatch(tr.scrollIntoView());
  }
  return true;
}

function setUnitAttrs(change) {
  return (state, dispatch) => {
    const unit = unitAt(state.selection.$from);
    if (!unit || unit.node.type !== N.unit) return false;
    const attrs = change(unit.node.attrs, unit.node);
    if (!attrs) return false;
    if (dispatch) dispatch(state.tr.setNodeMarkup(unit.pos, null, attrs));
    return true;
  };
}

export const commands = {
  kind: (kind) => setUnitAttrs((attrs) => ({ ...attrs, kind })),
  level: (delta) => setUnitAttrs((attrs, node) => {
    const current = attrs.level ?? derivedDepth(unitLead(node)) ?? 0;
    const level = Math.max(0, Math.min(3, current + delta));
    return { ...attrs, level };
  }),
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
  insertTable: (state, dispatch) => {
    const unit = unitAt(state.selection.$from);
    const at = unit ? unit.pos + unit.node.nodeSize : state.doc.content.size;
    if (dispatch) {
      const cell = () => N.table_cell.create();
      const table = N.table_unit.create({ header_rows: 1 }, [N.table_caption.create(), N.table_row.create(null, [cell(), cell()]), N.table_row.create(null, [cell(), cell()])]);
      dispatch(state.tr.insert(at, table).scrollIntoView());
    }
    return true;
  },
  addRow: (state, dispatch) => {
    const { $from } = state.selection;
    for (let depth = $from.depth; depth > 0; depth--) {
      if ($from.node(depth).type === N.table_row) {
        const row = $from.node(depth);
        const after = $from.after(depth);
        if (dispatch) dispatch(state.tr.insert(after, N.table_row.create(null, Array.from({ length: row.childCount }, () => N.table_cell.create()))));
        return true;
      }
    }
    return false;
  },
  addColumn: (state, dispatch) => {
    const unit = unitAt(state.selection.$from);
    if (!unit || unit.node.type !== N.table_unit) return false;
    if (dispatch) {
      const tr = state.tr;
      let pos = unit.pos + 1;
      unit.node.forEach((child) => {
        if (child.type === N.table_row) tr.insert(tr.mapping.map(pos + child.nodeSize - 1), N.table_cell.create());
        pos += child.nodeSize;
      });
      dispatch(tr);
    }
    return true;
  },
  undo,
  redo,
};

// --- the editor ------------------------------------------------------------------------------------

export function createEditor(mount, { units, machineUnits, flags, onChange, onFlagClick, onUnitFocus, onMarkerEdit, editable = true }) {
  const keys = keymap({
    Enter: (state, dispatch) => splitNote(state, dispatch) || splitUnit(state, dispatch) || true,
    "Shift-Enter": () => true,
    Backspace: joinBackward,
    Tab: commands.level(1),
    "Shift-Tab": commands.level(-1),
    "Mod-z": undo,
    "Mod-y": redo,
    "Shift-Mod-z": redo,
    "Mod-Alt-1": commands.kind("heading"),
    "Mod-Alt-2": commands.kind("paragraph"),
    "Mod-Alt-3": commands.kind("list_item"),
    "Mod-Alt-n": commands.addNote,
  });
  let lastUnit = -1;
  const state = EditorState.create({
    doc: toDoc(units),
    plugins: [history(), keys, keymap(baseKeymap), flagPlugin(flags, machineUnits, units, onFlagClick)],
  });
  const view = new EditorView(mount, {
    state,
    editable: () => editable,
    dispatchTransaction(tr) {
      const next = view.state.apply(tr);
      view.updateState(next);
      if (tr.docChanged) onChange && onChange();
      const index = next.selection.$from.depth ? next.selection.$from.index(0) : -1;
      if (index !== lastUnit) { lastUnit = index; onUnitFocus && onUnitFocus(index); }
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

  function flagRange(id) {
    const found = flagKey.getState(view.state).set.find(undefined, undefined, (spec) => spec.flag === id);
    return found.length ? found[0] : null;
  }

  return {
    view,
    units: () => fromDoc(view.state.doc),
    run(command) { command(view.state, view.dispatch); view.focus(); },
    // Replace the flagged words with a check reading's words (or remove / insert them).
    applyReading(id, words) {
      const range = flagRange(id);
      if (!range) return false;
      const tr = view.state.tr;
      if (range.from === range.to) tr.insertText(`${words} `, range.from);
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
