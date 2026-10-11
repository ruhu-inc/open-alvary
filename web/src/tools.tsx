// Reading tools above a document's text: cite it, download it, print it, find words in it.

import { useEffect, useRef, useState } from "react";
import { Copy, Download, Printer, Quote, Search } from "lucide-react";

export type DownloadLink = {
  label: string;
  format: string;
  href: string;
  note?: string;
};

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function CopyRow({ label, value }: { label: string; value: string }) {
  const [done, setDone] = useState<"" | "copied" | "failed">("");
  return (
    <div className="doc-copy">
      <span className="doc-copy-label">{label}</span>
      <code>{value}</code>
      <button
        type="button"
        onClick={async () =>
          setDone((await copyText(value)) ? "copied" : "failed")
        }
      >
        <Copy size={14} />{" "}
        {done === "copied"
          ? "Copied"
          : done === "failed"
            ? "Select and copy"
            : "Copy"}
      </button>
    </div>
  );
}

// Matches of `query` in the document text, highlighted without changing the page (the CSS Custom
// Highlight API); where it is not supported, the reader is still taken to each match.
type Registry = {
  set: (name: string, highlight: unknown) => void;
  delete: (name: string) => void;
};
const highlights = (): Registry | null =>
  (globalThis.CSS as unknown as { highlights?: Registry })?.highlights ?? null;
const Highlight = (
  globalThis as unknown as {
    Highlight?: new (...ranges: Range[]) => unknown;
  }
).Highlight;

function findRanges(root: Element, query: string): Range[] {
  const ranges: Range[] = [];
  const needle = query.toLowerCase();
  if (needle.length < 2) return ranges;
  for (const block of root.querySelectorAll("p, h2, h3, th, td, caption")) {
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    const nodes: { node: Text; start: number }[] = [];
    let text = "";
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      nodes.push({ node: node as Text, start: text.length });
      text += node.nodeValue ?? "";
    }
    const at = (offset: number) => {
      let index = nodes.length - 1;
      while (index > 0 && nodes[index].start > offset) index--;
      return { node: nodes[index].node, offset: offset - nodes[index].start };
    };
    const lower = text.toLowerCase();
    for (
      let i = lower.indexOf(needle);
      i >= 0;
      i = lower.indexOf(needle, i + needle.length)
    ) {
      const range = document.createRange();
      const start = at(i);
      const end = at(i + needle.length);
      range.setStart(start.node, start.offset);
      range.setEnd(end.node, end.offset);
      ranges.push(range);
    }
  }
  return ranges;
}

function FindInDocument({ root }: { root: () => Element | null }) {
  const [query, setQuery] = useState("");
  const [ranges, setRanges] = useState<Range[]>([]);
  const [current, setCurrent] = useState(0);
  useEffect(() => {
    const element = root();
    const found = element ? findRanges(element, query) : [];
    setRanges(found);
    setCurrent(0);
    const registry = highlights();
    if (registry && Highlight) {
      if (found.length) registry.set("doc-find", new Highlight(...found));
      else registry.delete("doc-find");
    }
    return () => {
      highlights()?.delete("doc-find");
      highlights()?.delete("doc-find-current");
    };
  }, [query, root]);
  useEffect(() => {
    const range = ranges[current];
    if (!range) return;
    const registry = highlights();
    if (registry && Highlight)
      registry.set("doc-find-current", new Highlight(range));
    range.startContainer.parentElement?.scrollIntoView({ block: "center" });
  }, [ranges, current]);
  const step = (by: number) =>
    ranges.length && setCurrent((current + by + ranges.length) % ranges.length);
  return (
    <div className="doc-find" role="search">
      <Search size={15} />
      <input
        type="search"
        value={query}
        placeholder="Find in this document"
        aria-label="Find in this document"
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            step(event.shiftKey ? -1 : 1);
          }
        }}
      />
      {query.length >= 2 ? (
        <span className="doc-find-count" role="status">
          {ranges.length ? `${current + 1} of ${ranges.length}` : "No matches"}
        </span>
      ) : null}
      <button
        type="button"
        aria-label="Previous match"
        disabled={!ranges.length}
        onClick={() => step(-1)}
      >
        ↑
      </button>
      <button
        type="button"
        aria-label="Next match"
        disabled={!ranges.length}
        onClick={() => step(1)}
      >
        ↓
      </button>
    </div>
  );
}

export function DocumentTools({
  citation,
  pageUrl,
  downloads,
  textRoot,
}: {
  citation: string;
  pageUrl: string;
  downloads: DownloadLink[];
  textRoot: () => Element | null;
}) {
  const [open, setOpen] = useState<"" | "cite" | "download">("");
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => panel.current?.focus(), [open]);
  const toggle = (which: "cite" | "download") =>
    setOpen(open === which ? "" : which);
  return (
    <div className="doc-tools">
      <div className="doc-tool-buttons">
        <button
          type="button"
          aria-expanded={open === "cite"}
          onClick={() => toggle("cite")}
        >
          <Quote size={15} /> Cite
        </button>
        {downloads.length ? (
          <button
            type="button"
            aria-expanded={open === "download"}
            onClick={() => toggle("download")}
          >
            <Download size={15} /> Download
          </button>
        ) : null}
        <button type="button" onClick={() => window.print()}>
          <Printer size={15} /> Print
        </button>
        <FindInDocument root={textRoot} />
      </div>
      {open === "cite" ? (
        <div className="doc-tool-panel" ref={panel} tabIndex={-1}>
          <CopyRow label="Citation" value={citation} />
          <CopyRow label="Link" value={pageUrl} />
          <p className="doc-tool-note">
            To cite or link one section, use the link button beside its title.
          </p>
        </div>
      ) : null}
      {open === "download" ? (
        <div className="doc-tool-panel" ref={panel} tabIndex={-1}>
          <ul className="doc-downloads">
            {downloads.map((d) => (
              <li key={d.href}>
                <a href={d.href} download>
                  {d.label} <span className="doc-format">{d.format}</span>
                </a>
                {d.note ? (
                  <span className="doc-tool-note"> {d.note}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
