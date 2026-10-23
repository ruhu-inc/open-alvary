// Find and replace on the page: matches are highlighted in the words, margin notes, captions and
// cells. A replacement keeps the formatting of the first letter it replaces, a match never runs
// across a section-number chip, and one undo takes back a Replace all.

import { Plugin, PluginKey, TextSelection } from "prosemirror-state";
import { Decoration, DecorationSet } from "prosemirror-view";

export const findKey = new PluginKey("find");
const CHIP = "￼"; // stands for a number chip: one position, one character

export function searchPattern(query, { matchCase = false, wholeWord = false } = {}) {
  if (!query) return null;
  const body = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(wholeWord ? `(?<![\\p{L}\\p{N}_])${body}(?![\\p{L}\\p{N}_])` : body, `gu${matchCase ? "" : "i"}`);
}

// Every match in document order, as document positions: in a textblock each character, and each
// chip, takes one position, so an offset in its text is an offset from its start.
export function findMatches(doc, pattern) {
  const matches = [];
  if (!pattern) return matches;
  doc.descendants((node, pos) => {
    if (!node.isTextblock) return true;
    const value = node.textBetween(0, node.content.size, undefined, CHIP);
    for (const match of value.matchAll(pattern)) {
      if (match[0].length) matches.push({ from: pos + 1 + match.index, to: pos + 1 + match.index + match[0].length });
    }
    return false;
  });
  return matches;
}

const empty = { query: "", options: {}, matches: [], decorations: DecorationSet.empty };

function decorate(doc, matches, selection) {
  return DecorationSet.create(doc, matches.map((m) => {
    const current = selection.from === m.from && selection.to === m.to;
    return Decoration.inline(m.from, m.to, { class: current ? "find-hit current" : "find-hit" });
  }));
}

export const findPlugin = new Plugin({
  key: findKey,
  state: {
    init: () => empty,
    apply(tr, value, _, state) {
      const meta = tr.getMeta(findKey);
      const next = meta ? { ...value, ...meta } : value;
      if (!next.query) return meta ? empty : value;
      if (!meta && !tr.docChanged && !tr.selectionSet) return value;
      const matches = meta || tr.docChanged ? findMatches(state.doc, searchPattern(next.query, next.options)) : next.matches;
      return { ...next, matches, decorations: decorate(state.doc, matches, state.selection) };
    },
  },
  props: { decorations: (state) => findKey.getState(state).decorations },
});

// Which match is selected (1-based; 0 when none) and how many there are.
export function findStatus(state) {
  const { matches } = findKey.getState(state);
  const at = matches.findIndex((m) => m.from === state.selection.from && m.to === state.selection.to);
  return { count: matches.length, current: at + 1 };
}

// Select the next (1) or previous (-1) match after the selection, wrapping round the page.
export function findNext(direction) {
  return (state, dispatch) => {
    const { matches } = findKey.getState(state);
    if (!matches.length) return false;
    const { from, to } = state.selection;
    const match = direction > 0
      ? matches.find((m) => m.from >= to && !(m.from === from && m.to === to)) || matches[0]
      : matches.findLast((m) => m.to <= from && !(m.from === from && m.to === to)) || matches[matches.length - 1];
    if (dispatch) dispatch(state.tr.setSelection(TextSelection.create(state.doc, match.from, match.to)).scrollIntoView());
    return true;
  };
}

function replaceAt(tr, match, replacement) {
  const from = tr.mapping.map(match.from), to = tr.mapping.map(match.to);
  if (!replacement) return tr.delete(from, to);
  const first = tr.doc.nodeAt(from);
  return tr.replaceWith(from, to, tr.doc.type.schema.text(replacement, first ? first.marks : []));
}

// Replace the selected match (selecting the first one if none is) and go to the next.
export function replaceOne(replacement) {
  return (state, dispatch) => {
    const { matches } = findKey.getState(state);
    const match = matches.find((m) => m.from === state.selection.from && m.to === state.selection.to);
    if (!match) return findNext(1)(state, dispatch);
    if (dispatch) {
      const tr = replaceAt(state.tr, match, replacement);
      const after = tr.mapping.map(match.to);
      const rest = findMatches(tr.doc, searchPattern(findKey.getState(state).query, findKey.getState(state).options));
      const next = rest.find((m) => m.from >= after) || rest[0];
      tr.setSelection(next ? TextSelection.create(tr.doc, next.from, next.to) : TextSelection.create(tr.doc, after));
      dispatch(tr.scrollIntoView());
    }
    return true;
  };
}

export function replaceAll(replacement) {
  return (state, dispatch) => {
    const { matches } = findKey.getState(state);
    if (!matches.length) return false;
    if (dispatch) {
      const tr = state.tr;
      for (const match of matches) replaceAt(tr, match, replacement);
      dispatch(tr.scrollIntoView());
    }
    return true;
  };
}
