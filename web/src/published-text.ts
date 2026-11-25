// A published text laid out from its structure, with no DOM and no imports, so the same file runs
// in the browser and under `node --test` (tests/test_published_text.mjs).
//
// The public API serves a text version's words (`canonical_text`) and its structure: nodes with
// UTF-8 byte spans over those words (`start_byte`, `end_byte`), a kind, the printed number (`label`)
// and the heading. The words are cut where the text itself breaks them (a blank line between two
// blocks, as oa-text-1 writes them), never where a node starts, because a node may open inside a
// block ("23. (1) …" holds section 23 and its subsection 1). Each block is then a heading, a
// paragraph or a table, from the nodes that open in it. The pieces, joined in order, are the
// text: every character once.

export type TextNode = {
  id: string;
  version_id: string;
  parent_id?: string | null;
  kind: string;
  locator: string;
  order: number;
  start_byte: number;
  end_byte: number;
  source_hash?: string | null;
  label?: string | null;
  heading?: string | null;
  cell_states?: Record<string, string> | null;
};

export type TextVersion = {
  id: string;
  language?: string | null;
  canonical_text: string;
  content_hash: string;
  effective_from?: string | null;
  retrieved_at?: string | null;
  prior_version_id?: string | null;
};

export type Piece =
  // The line feeds between two blocks, and the one that ends the text.
  | { type: "gap"; text: string }
  | {
      type: "heading";
      text: string;
      id: string | null;
      kind: string; // the kind of the node it heads
      level: number; // the HTML heading level, 3 to 6
      rank: 1 | 2 | 3 | 4; // 1 the document's title, 2 a part or chapter, 3 a division, 4 an article or section
      printed: boolean; // inside the document's own printed table of contents
    }
  | {
      type: "paragraph";
      text: string;
      id: string | null;
      depth: number; // 0 a section's or article's own words … 3 a sub-paragraph
      lead: number; // how many characters at the start are the provision's printed number
      printed: boolean;
    }
  | {
      type: "table";
      text: string;
      id: string | null;
      rows: string[][];
      states: Record<string, string>;
      printed: boolean;
    };

export type Entry = {
  id: string;
  label: string;
  part: boolean; // a title, part, chapter, division, schedule or annex, not a provision
};

export type Laid = { pieces: Piece[]; contents: Entry[] };

// What the Contents lists, and what a reader links to.
const PROVISIONS = new Set(["article", "section"]);
const PARTS = new Set([
  "title",
  "part",
  "chapter",
  "division",
  "schedule",
  "annex",
]);
// The document's own printed table of contents: shown where it is printed, never listed again.
const PRINTED_CONTENTS = new Set(["contents", "contents_entry"]);
// Provisions' inner units open with their number and their words in one block: never a heading.
const NEVER_HEADED = new Set([
  "subsection",
  "paragraph",
  "subparagraph",
  "definition",
  "contents_entry",
  "table",
  "quotation",
]);
const DEEPER = new Set(["subsection", "paragraph", "subparagraph"]);
const RANKS: Record<string, 1 | 2 | 3 | 4> = {
  document: 1,
  title: 2,
  part: 2,
  chapter: 2,
  schedule: 2,
  annex: 2,
  article: 4,
  section: 4,
};
// Locator tokens (sec_23__subsec_1, chap_ii__dvs_1) as the words a link reads by.
const TOKENS: Record<string, string> = {
  art: "article",
  sec: "section",
  chap: "chapter",
  dvs: "division",
  subdvs: "subdivision",
  sch: "schedule",
  sched: "schedule",
};

const encoder = new TextEncoder();

export function byteLength(text: string): number {
  return encoder.encode(text).length;
}

const squeeze = (value: string | null | undefined) =>
  (value ?? "").replace(/\s+/g, " ").trim();

// A version's nodes, or none where they cannot be trusted to describe its words: nodes of another
// version or another text, and spans that run past the end or cut a character in two.
export function nodesFor(
  version: TextVersion,
  structure: TextNode[] | undefined | null,
): TextNode[] {
  const nodes = (structure ?? []).filter(
    (node) => node.version_id === version.id,
  );
  if (!nodes.length) return [];
  const bytes = encoder.encode(version.canonical_text);
  const boundary = (at: number) =>
    at === bytes.length || (bytes[at] & 0xc0) !== 0x80;
  const sound = nodes.every(
    (node) =>
      Number.isInteger(node.start_byte) &&
      Number.isInteger(node.end_byte) &&
      node.start_byte >= 0 &&
      node.start_byte <= node.end_byte &&
      node.end_byte <= bytes.length &&
      boundary(node.start_byte) &&
      boundary(node.end_byte) &&
      (!node.source_hash || node.source_hash === version.content_hash),
  );
  return sound ? nodes : [];
}

type Block = { text: string; start: number; end: number };

// The text cut at its blank lines. Blocks and gaps alternate; joined, they are the text.
function cut(text: string): {
  parts: ({ gap: string } | Block)[];
  blocks: Block[];
} {
  const parts: ({ gap: string } | Block)[] = [];
  const blocks: Block[] = [];
  const breaks = /\n{2,}|^\n+|\n+$/g;
  let at = 0;
  let byte = 0;
  const block = (words: string) => {
    if (!words) return;
    const length = byteLength(words);
    const made = { text: words, start: byte, end: byte + length };
    byte += length;
    parts.push(made);
    blocks.push(made);
  };
  for (const found of text.matchAll(breaks)) {
    block(text.slice(at, found.index));
    parts.push({ gap: found[0] });
    byte += found[0].length; // line feeds are one byte each
    at = found.index + found[0].length;
  }
  block(text.slice(at));
  return { parts, blocks };
}

// Whether a block is the node's heading as printed: its number and heading on one line ("Article 2:
// Definition of terms"), its number alone ("ARTICLE 1", the heading on the next line), or its
// heading alone (a section's margin note, printed above "1. This Act …").
function headingOf(
  words: string,
  node: TextNode,
): "both" | "label" | "heading" | null {
  const text = squeeze(words);
  const label = squeeze(node.label);
  const heading = squeeze(node.heading);
  if (!text) return null;
  if (label && heading && text.startsWith(label) && text.endsWith(heading)) {
    const between = text.length - label.length - heading.length;
    if (between >= 0 && between <= 6) return "both";
  }
  if (heading && text === heading) return "heading";
  if (
    label &&
    text.startsWith(label) &&
    /^[\s.:—–-]{0,3}$/.test(text.slice(label.length))
  )
    return "label";
  return null;
}

function anchorFor(node: TextNode): string {
  const id = node.locator
    .toLowerCase()
    .split("__")
    .map((token) => {
      const [name, ...rest] = token.split("_");
      return [TOKENS[name] ?? name, ...rest].join("-");
    })
    .join("-")
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return id || "part";
}

function tableRows(words: string): string[][] {
  return words.split("\n").map((row) => row.split("\t"));
}

// A table as oa-text-1 writes one: a block of rows, the same number of tabs in each. Rwandan laws'
// tables have no node of their own, so the block's shape is all that tells them.
function isGrid(words: string): boolean {
  const tabs = words.split("\n").map((row) => row.split("\t").length - 1);
  return tabs[0] > 0 && tabs.every((count) => count === tabs[0]);
}

// Whether a printed table of contents is folded away until asked for: a long one that the Contents
// lists again. A short one stays open, and so does one the Contents cannot stand in for, such as a
// text whose structure put its whole body under "contents".
export function foldsAway(printedBlocks: number, entries: number): boolean {
  return printedBlocks > 12 && entries > 0 && printedBlocks <= 2 * entries + 10;
}

// The text's pieces in reading order and its contents. `nodes` are one version's (nodesFor).
export function layoutText(text: string, nodes: TextNode[]): Laid {
  const { parts, blocks } = cut(text);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const chain = (node: TextNode) => {
    const up: TextNode[] = [];
    for (
      let at: TextNode | undefined = node, steps = 0;
      at && steps < 64;
      steps++
    ) {
      up.push(at);
      at = at.parent_id ? byId.get(at.parent_id) : undefined;
    }
    return up;
  };
  const depthOf = new Map(nodes.map((node) => [node.id, chain(node).length]));
  // Document order: by where a node starts, the enclosing node first.
  const ordered = nodes
    .filter((node) => node.end_byte > node.start_byte)
    .sort(
      (a, b) =>
        a.start_byte - b.start_byte ||
        b.end_byte - a.end_byte ||
        depthOf.get(a.id)! - depthOf.get(b.id)! ||
        a.order - b.order,
    );
  const printed = (node: TextNode) =>
    chain(node).some((at) => PRINTED_CONTENTS.has(at.kind));
  const listed = (node: TextNode) =>
    (PROVISIONS.has(node.kind) || PARTS.has(node.kind)) && !printed(node);

  // The nodes that open in each block, in document order, and whether at its first byte.
  const opening = new Map<Block, { node: TextNode; exact: boolean }[]>();
  let from = 0;
  for (const node of ordered) {
    while (from < blocks.length && blocks[from].end <= node.start_byte) from++;
    const block = blocks[from];
    if (!block || block.start >= node.end_byte) continue;
    if (!opening.has(block)) opening.set(block, []);
    opening.get(block)!.push({ node, exact: node.start_byte <= block.start });
  }

  const used = new Set<string>();
  const anchor = (wanted: string) => {
    let id = wanted;
    for (let n = 2; used.has(id); n++) id = `${wanted}-${n}`;
    used.add(id);
    return id;
  };

  const pieces: Piece[] = [];
  const contents: Entry[] = [];
  const open: TextNode[] = []; // the nodes around the block being laid, outermost first
  let next = 0;
  // A node whose number stood alone, waiting for its heading on the next line.
  let due: { node: TextNode; entry: Entry | null } | null = null;

  for (const part of parts) {
    if ("gap" in part) {
      pieces.push({ type: "gap", text: part.gap });
      continue;
    }
    const block = part;
    while (next < ordered.length && ordered[next].start_byte <= block.start)
      open.push(ordered[next++]);
    while (open.length && open[open.length - 1].end_byte <= block.start)
      open.pop();
    const owner: TextNode | undefined = open[open.length - 1];
    const around = owner ? chain(owner) : [];
    const inPrinted = around.some((at) => PRINTED_CONTENTS.has(at.kind));
    const starts = opening.get(block) ?? [];

    // The anchor: the outermost listed node that opens here. Others opening here share it.
    const linked = starts
      .filter(({ node }) => listed(node))
      .map(({ node }) => node);
    const id = linked.length ? anchor(anchorFor(linked[0])) : null;

    // The heading: the first node opening at this block's first byte whose heading it is.
    let headed: { node: TextNode; how: "both" | "label" | "heading" } | null =
      null;
    for (const { node, exact } of starts) {
      if (!exact || NEVER_HEADED.has(node.kind)) continue;
      if (node.kind !== "contents" && printed(node)) continue;
      const how = headingOf(block.text, node);
      if (how) {
        headed = { node, how };
        break;
      }
    }
    const waited = due;
    due = null;

    const entries = new Map<TextNode, Entry>();
    for (const node of linked) {
      const entry = {
        id: id!,
        label: [squeeze(node.label), squeeze(node.heading)]
          .filter(Boolean)
          .join(" "),
        part: PARTS.has(node.kind),
      };
      entries.set(node, entry);
      contents.push(entry);
    }

    const level = (node: TextNode) =>
      Math.min(
        6,
        3 +
          chain(node).filter(
            (at) =>
              at !== node && (PARTS.has(at.kind) || PROVISIONS.has(at.kind)),
          ).length,
      );
    const rank = (node: TextNode) => RANKS[node.kind] ?? 3;

    if (headed) {
      const { node, how } = headed;
      const entry = entries.get(node) ?? null;
      const label = squeeze(node.label);
      const words = squeeze(block.text);
      // Listed as printed where the line holds the number; a margin note is listed after its number.
      if (entry)
        entry.label = how === "heading" && label ? `${label} ${words}` : words;
      if (how === "label" && squeeze(node.heading)) due = { node, entry };
      pieces.push({
        type: "heading",
        text: block.text,
        id,
        kind: node.kind,
        level: level(node),
        rank: rank(node),
        printed: inPrinted,
      });
      continue;
    }
    if (
      waited &&
      !starts.length &&
      block.start < waited.node.end_byte &&
      squeeze(block.text) === squeeze(waited.node.heading)
    ) {
      if (waited.entry)
        waited.entry.label = `${squeeze(waited.node.label)} ${squeeze(block.text)}`;
      pieces.push({
        type: "heading",
        text: block.text,
        id: null,
        kind: waited.node.kind,
        level: level(waited.node),
        rank: rank(waited.node),
        printed: inPrinted,
      });
      continue;
    }
    for (const entry of entries.values())
      if (!entry.label)
        entry.label = squeeze(block.text).slice(0, 80) || "Untitled";

    const captioned =
      owner?.kind === "table" && squeeze(block.text) !== squeeze(owner.heading);
    if (captioned || isGrid(block.text)) {
      pieces.push({
        type: "table",
        text: block.text,
        id,
        rows: tableRows(block.text),
        states: (captioned && owner?.cell_states) || {},
        printed: inPrinted,
      });
      continue;
    }
    // A provision that opens with its number and its words: the number is set apart.
    const provision = starts.find(
      ({ node, exact }) => exact && PROVISIONS.has(node.kind) && !printed(node),
    );
    const number = provision ? (provision.node.label ?? "").trim() : "";
    let lead = 0;
    if (number && number.length <= 6 && block.text.startsWith(number)) {
      const space = /^[ \t]*/.exec(block.text.slice(number.length))![0].length;
      if (block.text.length > number.length + space)
        lead = number.length + space;
    }
    pieces.push({
      type: "paragraph",
      text: block.text,
      id,
      depth: Math.min(3, around.filter((at) => DEEPER.has(at.kind)).length),
      lead,
      printed: inPrinted,
    });
  }
  return { pieces, contents };
}

// The text to open first: the one asked for (a link's ?version= or ?lang=), else the record's own
// language, else the first language served. Within a language, the latest text that no later
// reading corrects.
export function chooseVersion<V extends TextVersion>(
  versions: V[],
  recordLanguage?: string | null,
  wanted: { version?: string | null; language?: string | null } = {},
): V | undefined {
  if (!versions.length) return undefined;
  const asked = versions.find((version) => version.id === wanted.version);
  if (asked) return asked;
  const same = (a?: string | null, b?: string | null) =>
    Boolean(a) && a!.toLowerCase() === (b ?? "").toLowerCase();
  const replaced = new Set(
    versions.map((version) => version.prior_version_id).filter(Boolean),
  );
  const inLanguage = (language?: string | null) => {
    const texts = versions.filter((version) =>
      same(version.language, language),
    );
    const current = texts.filter((version) => !replaced.has(version.id));
    return (current.length ? current : texts).reduce<V | undefined>(
      (best, version) =>
        !best || (version.effective_from ?? "") >= (best.effective_from ?? "")
          ? version
          : best,
      undefined,
    );
  };
  return (
    inLanguage(wanted.language) ??
    inLanguage(recordLanguage) ??
    inLanguage(versions[0].language) ??
    versions[0]
  );
}
