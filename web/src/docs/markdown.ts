// The documentation's Markdown, parsed into blocks. A small, strict subset, enough for the pages
// in content/ and the generated ones (reference.ts, errors.ts):
//
//   # Heading            ## Heading {#explicit-id}
//   paragraphs           **bold**, *italic*, `code`, [text](href)
//   - item / 1. item     {3} at the start of an item draws step number 3, as in the pictures
//   ```lang              code; consecutive curl / python / javascript blocks become tabs
//   | a | b |            tables (the second row is the separator)
//   > Note: text         a callout (Note, Tip or Warning)
//   ![alt](/guide/x.webp)  a picture, on its own line
//   :::cards ... :::     link cards: "- [Title](href) text" per card
//
// Pure TypeScript (no React), so the build can also turn pages into plain text and Markdown files.

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; children: Inline[] }
  | { kind: "em"; children: Inline[] }
  | { kind: "link"; href: string; children: Inline[] };

export type Block =
  | { kind: "heading"; level: 1 | 2 | 3 | 4; id: string; text: string; inline: Inline[] }
  | { kind: "paragraph"; inline: Inline[] }
  | { kind: "list"; ordered: boolean; start: number; items: { step?: number; inline: Inline[] }[] }
  | { kind: "code"; lang: string; text: string }
  | { kind: "tabs"; blocks: { lang: string; text: string }[] }
  | { kind: "table"; head: Inline[][]; rows: Inline[][][] }
  | { kind: "callout"; tone: "note" | "tip" | "warning"; inline: Inline[] }
  | { kind: "image"; alt: string; src: string }
  | { kind: "cards"; cards: { title: string; href: string; text: Inline[] }[] };

// Languages shown side by side as tabs, in this order.
export const TAB_LANGUAGES = ["curl", "python", "javascript"];

export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/`/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "");
}

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let rest = text;
  const push = (t: string) => {
    if (!t) return;
    const last = out[out.length - 1];
    if (last && last.kind === "text") last.text += t;
    else out.push({ kind: "text", text: t });
  };
  while (rest) {
    const match = /`([^`]+)`|\*\*(.+?)\*\*|\*([^*\s][^*]*?)\*|\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest);
    if (!match) {
      push(rest);
      break;
    }
    push(rest.slice(0, match.index));
    if (match[1] !== undefined) out.push({ kind: "code", text: match[1] });
    else if (match[2] !== undefined) out.push({ kind: "strong", children: parseInline(match[2]) });
    else if (match[3] !== undefined) out.push({ kind: "em", children: parseInline(match[3]) });
    else out.push({ kind: "link", href: match[5], children: parseInline(match[4]) });
    rest = rest.slice(match.index + match[0].length);
  }
  return out;
}

export function inlineText(inline: Inline[]): string {
  return inline.map((i) => (i.kind === "text" || i.kind === "code" ? i.text : inlineText(i.children))).join("");
}

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\||\|$/g, "")
    .split(/(?<!\\)\|/)
    .map((c) => c.trim().replace(/\\\|/g, "|"));
}

export function parse(markdown: string): Block[] {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  const used = new Map<string, number>();
  const uniqueId = (id: string) => {
    const n = used.get(id) ?? 0;
    used.set(id, n + 1);
    return n ? `${id}-${n}` : id;
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = /^```(\S*)\s*$/.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      const code = { lang: fence[1] || "text", text: body.join("\n") };
      const last = blocks[blocks.length - 1];
      if (TAB_LANGUAGES.includes(code.lang) && last?.kind === "tabs") last.blocks.push(code);
      else if (TAB_LANGUAGES.includes(code.lang) && last?.kind === "code" && TAB_LANGUAGES.includes(last.lang))
        blocks[blocks.length - 1] = { kind: "tabs", blocks: [last, code] };
      else blocks.push({ kind: "code", ...code });
      continue;
    }
    const heading = /^(#{1,4})\s+(.*?)(?:\s+\{#([\w-]+)\})?\s*$/.exec(line);
    if (heading) {
      const text = heading[2];
      const inline = parseInline(text);
      blocks.push({
        kind: "heading",
        level: heading[1].length as 1 | 2 | 3 | 4,
        id: uniqueId(heading[3] ?? slugify(inlineText(inline))),
        text: inlineText(inline),
        inline,
      });
      i++;
      continue;
    }
    if (line.startsWith(":::cards")) {
      const cards: { title: string; href: string; text: Inline[] }[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith(":::")) {
        const card = /^-\s+\[([^\]]+)\]\(([^)]+)\)\s*(.*)$/.exec(lines[i]);
        if (card) cards.push({ title: card[1], href: card[2], text: parseInline(card[3]) });
        i++;
      }
      i++;
      blocks.push({ kind: "cards", cards });
      continue;
    }
    const image = /^!\[([^\]]*)\]\(([^)\s]+)\)\s*$/.exec(line);
    if (image) {
      blocks.push({ kind: "image", alt: image[1], src: image[2] });
      i++;
      continue;
    }
    if (line.startsWith(">")) {
      const body: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) body.push(lines[i++].replace(/^>\s?/, ""));
      const text = body.join(" ");
      const tone = /^(Note|Tip|Warning):\s*/.exec(text);
      blocks.push({
        kind: "callout",
        tone: (tone?.[1].toLowerCase() ?? "note") as "note" | "tip" | "warning",
        inline: parseInline(tone ? text.slice(tone[0].length) : text),
      });
      continue;
    }
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const head = cells(line).map(parseInline);
      i += 2;
      const rows: Inline[][][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]).map(parseInline));
      blocks.push({ kind: "table", head, rows });
      continue;
    }
    const item = /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(line);
    if (item) {
      const ordered = /\d/.test(item[2]);
      const start = ordered ? parseInt(item[2], 10) : 1;
      const items: { step?: number; inline: Inline[] }[] = [];
      while (i < lines.length) {
        const next = /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(lines[i]);
        if (next && /\d/.test(next[2]) === ordered) {
          let text = next[3];
          i++;
          // Continuation lines, indented under the item.
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s/.test(lines[i]))
            text += " " + lines[i++].trim();
          const step = /^\{(\d+)\}\s*/.exec(text);
          items.push({
            step: step ? Number(step[1]) : undefined,
            inline: parseInline(step ? text.slice(step[0].length) : text),
          });
        } else break;
      }
      blocks.push({ kind: "list", ordered, start, items });
      continue;
    }
    // A paragraph: this line (whatever it starts with, so parsing always moves on) and the plain
    // lines after it.
    const body: string[] = [lines[i++].trim()];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,4}\s|```|>|!\[|:::|\s*([-*]|\d+\.)\s)/.test(lines[i]) &&
      !lines[i].trim().startsWith("|")
    )
      body.push(lines[i++].trim());
    blocks.push({ kind: "paragraph", inline: parseInline(body.join(" ")) });
  }
  return blocks;
}

// Front matter: --- title: … / description: … --- at the top of a page.
export function frontMatter(source: string): { meta: Record<string, string>; body: string } {
  const match = /^---\n([\s\S]*?)\n---\n?/.exec(source);
  if (!match) return { meta: {}, body: source };
  const meta: Record<string, string> = {};
  for (const line of match[1].split("\n")) {
    const pair = /^(\w+):\s*(.*)$/.exec(line);
    if (pair) meta[pair[1]] = pair[2].trim();
  }
  return { meta, body: source.slice(match[0].length) };
}

// The words of a page, by section, for search.
export function sections(blocks: Block[]): { id: string; heading: string; text: string }[] {
  const out: { id: string; heading: string; text: string }[] = [{ id: "", heading: "", text: "" }];
  for (const block of blocks) {
    if (block.kind === "heading" && block.level >= 2) {
      out.push({ id: block.id, heading: block.text, text: "" });
      continue;
    }
    const words =
      block.kind === "paragraph" || block.kind === "callout"
        ? inlineText(block.inline)
        : block.kind === "list"
          ? block.items.map((item) => inlineText(item.inline)).join(" ")
          : block.kind === "table"
            ? [...block.head, ...block.rows.flat()].map(inlineText).join(" ")
            : block.kind === "cards"
              ? block.cards.map((c) => c.title + " " + inlineText(c.text)).join(" ")
              : block.kind === "heading"
                ? block.text
                : "";
    out[out.length - 1].text += " " + words;
  }
  return out.filter((s) => s.heading || s.text.trim());
}
