// The developer documentation at /docs: a sidebar with search, the page, and "On this page". Pages
// are Markdown (content/) and pages written from the API's own description (reference.ts); each is
// also published as Markdown at /docs/<page>.md and in /llms.txt by the build (web/docs-files.ts).

import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, ChevronDown, Copy, FileText, Menu, Search, X } from "lucide-react";
import { tokens } from "./highlight";
import { SIZES } from "./images";
import { type Block, type Inline, inlineText, parse, parseInline, sections, TAB_LANGUAGES } from "./markdown";
import { buildPages, type DocPage, docPath, type NavSection, type Plan, readingOrder } from "./pages";
import type { Examples, Spec, Tool } from "./reference";
import spec from "./data/openapi.json";
import tools from "./data/mcp-tools.json";
import plans from "./data/plans.json";
import examples from "./data/examples.json";
import { navigate } from "../site";
import "./docs.css";

const FILES = Object.fromEntries(
  Object.entries(import.meta.glob("./content/*.md", { query: "?raw", import: "default", eager: true })).map(
    ([path, source]) => [path.replace(/^.*\/|\.md$/g, ""), source as string],
  ),
);
const { pages, nav } = buildPages(
  FILES,
  spec as unknown as Spec,
  tools as unknown as Tool[],
  examples as unknown as Examples,
  plans as unknown as Plan[],
);
const ORDER = readingOrder(nav);
const parsed = new Map<string, Block[]>();
const blocksOf = (page: DocPage) => {
  if (!parsed.has(page.slug)) parsed.set(page.slug, parse(page.markdown));
  return parsed.get(page.slug)!;
};

const LANGUAGE_NAMES: Record<string, string> = {
  curl: "curl",
  python: "Python",
  javascript: "JavaScript",
  bash: "Shell",
  json: "JSON",
  http: "HTTP",
  text: "Text",
};
const read = (key: string) => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};
const write = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private windows: the choice lasts for this page only */
  }
};

// The language chosen in any tab group applies to every group on every page.
const Language = createContext<{ lang: string; choose: (lang: string) => void }>({ lang: "curl", choose: () => {} });

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(area);
    area.select();
    const done = document.execCommand("copy");
    area.remove();
    return done;
  }
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="docs-copy"
      onClick={async () => {
        if (await copyText(text)) {
          setDone(true);
          setTimeout(() => setDone(false), 1600);
        }
      }}
    >
      {done ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      <span>{done ? "Copied" : label}</span>
    </button>
  );
}

function Code({ lang, text }: { lang: string; text: string }) {
  return (
    <pre className="docs-pre" dir="ltr" tabIndex={0}>
      <code>
        {tokens(text, lang).map((token, i) =>
          token.kind === "plain" ? (
            token.text
          ) : (
            <span key={i} className={`tok-${token.kind}`}>
              {token.text}
            </span>
          ),
        )}
      </code>
    </pre>
  );
}

function CodeBlock({ lang, text }: { lang: string; text: string }) {
  return (
    <div className="docs-code">
      <div className="docs-code-bar">
        <span className="docs-code-lang">{LANGUAGE_NAMES[lang] ?? lang}</span>
        <CopyButton text={text} />
      </div>
      <Code lang={lang} text={text} />
    </div>
  );
}

function Tabs({ blocks }: { blocks: { lang: string; text: string }[] }) {
  const { lang, choose } = useContext(Language);
  const current = blocks.find((b) => b.lang === lang) ?? blocks[0];
  return (
    <div className="docs-code">
      <div className="docs-code-bar" role="tablist" aria-label="Language">
        <div className="docs-tabs">
          {blocks.map((b) => (
            <button
              key={b.lang}
              type="button"
              role="tab"
              aria-selected={b === current}
              className={b === current ? "on" : ""}
              onClick={() => choose(b.lang)}
            >
              {LANGUAGE_NAMES[b.lang] ?? b.lang}
            </button>
          ))}
        </div>
        <CopyButton text={current.text} />
      </div>
      <div role="tabpanel">
        <Code lang={current.lang} text={current.text} />
      </div>
    </div>
  );
}

function Inlines({ inline }: { inline: Inline[] }) {
  return (
    <>
      {inline.map((item, i) => {
        if (item.kind === "text") return <React.Fragment key={i}>{item.text}</React.Fragment>;
        if (item.kind === "code") return <code key={i}>{item.text}</code>;
        if (item.kind === "strong")
          return (
            <strong key={i}>
              <Inlines inline={item.children} />
            </strong>
          );
        if (item.kind === "em")
          return (
            <em key={i}>
              <Inlines inline={item.children} />
            </em>
          );
        const external = /^https?:/.test(item.href) && !item.href.startsWith("https://open.alvary.ai/docs");
        return (
          <a key={i} href={item.href} {...(external ? { rel: "noreferrer" } : {})}>
            <Inlines inline={item.children} />
          </a>
        );
      })}
    </>
  );
}

function Picture({ alt, src }: { alt: string; src: string }) {
  const name = src.replace(/^.*\/|\.webp$/g, "");
  const [width, height] = SIZES[name] ?? [];
  return (
    <figure className={"docs-shot" + (width && width < 1280 ? " part" : "")}>
      <a href={src} target="_blank" rel="noreferrer" title="Open the full-size picture">
        <img src={src} alt={alt} width={width} height={height} loading="lazy" decoding="async" />
      </a>
    </figure>
  );
}

function Blocks({ blocks }: { blocks: Block[] }) {
  return (
    <>
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "heading": {
            const Tag = `h${block.level}` as "h2";
            return (
              <Tag key={i} id={block.id}>
                <Inlines inline={block.inline} />
                {block.level > 1 ? (
                  <a className="docs-anchor" href={`#${block.id}`} aria-label={`Link to ${block.text}`}>
                    #
                  </a>
                ) : null}
              </Tag>
            );
          }
          case "paragraph":
            return (
              <p key={i}>
                <Inlines inline={block.inline} />
              </p>
            );
          case "list": {
            const steps = block.items.some((item) => item.step !== undefined);
            const items = block.items.map((item, j) => (
              <li key={j}>
                {item.step !== undefined ? (
                  <span className="docs-step" aria-label={`(${item.step})`}>
                    {item.step}
                  </span>
                ) : null}
                <span>
                  <Inlines inline={item.inline} />
                </span>
              </li>
            ));
            if (steps)
              return (
                <ul key={i} className="docs-steps">
                  {items}
                </ul>
              );
            return block.ordered ? (
              <ol key={i} start={block.start}>
                {items}
              </ol>
            ) : (
              <ul key={i}>{items}</ul>
            );
          }
          case "code":
            return <CodeBlock key={i} lang={block.lang} text={block.text} />;
          case "tabs":
            return <Tabs key={i} blocks={block.blocks} />;
          case "table": {
            const headless = block.head.every((cell) => !inlineText(cell).trim());
            return (
              <div key={i} className="docs-table" tabIndex={0}>
                <table>
                  {headless ? null : (
                    <thead>
                      <tr>
                        {block.head.map((cell, j) => (
                          <th key={j}>
                            <Inlines inline={cell} />
                          </th>
                        ))}
                      </tr>
                    </thead>
                  )}
                  <tbody>
                    {block.rows.map((row, j) => (
                      <tr key={j}>
                        {row.map((cell, k) =>
                          headless && k === 0 ? (
                            <th key={k} scope="row">
                              <Inlines inline={cell} />
                            </th>
                          ) : (
                            <td key={k}>
                              <Inlines inline={cell} />
                            </td>
                          ),
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          }
          case "callout":
            return (
              <div key={i} className={`docs-callout ${block.tone}`}>
                <strong>{block.tone === "tip" ? "Tip" : block.tone === "warning" ? "Warning" : "Note"}</strong>
                <span>
                  <Inlines inline={block.inline} />
                </span>
              </div>
            );
          case "image":
            return <Picture key={i} alt={block.alt} src={block.src} />;
          case "cards":
            return (
              <div key={i} className="docs-cards">
                {block.cards.map((card) => (
                  <a key={card.href} className="docs-card" href={card.href}>
                    <strong>{card.title}</strong>
                    <span>
                      <Inlines inline={card.text} />
                    </span>
                  </a>
                ))}
              </div>
            );
        }
      })}
    </>
  );
}

// ---------- search ----------
type Hit = { slug: string; title: string; heading: string; id: string; snippet: string; score: number };
let index: { slug: string; title: string; heading: string; id: string; text: string }[] | null = null;

function search(query: string): Hit[] {
  index ??= [...pages.values()].flatMap((page) =>
    sections(blocksOf(page)).map((s) => ({
      slug: page.slug,
      title: page.title,
      heading: s.heading,
      id: s.id,
      text: s.text,
    })),
  );
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  const hits: Hit[] = [];
  for (const entry of index) {
    const title = entry.title.toLowerCase();
    const heading = entry.heading.toLowerCase();
    const text = entry.text.toLowerCase();
    if (!terms.every((t) => title.includes(t) || heading.includes(t) || text.includes(t))) continue;
    const score = terms.reduce(
      (sum, t) => sum + (title.includes(t) ? 8 : 0) + (heading.includes(t) ? 5 : 0) + (text.includes(t) ? 1 : 0),
      entry.heading ? 0 : 1,
    );
    const at = Math.max(0, text.indexOf(terms[0]) - 40);
    const snippet =
      (at ? "…" : "") +
      entry.text
        .trim()
        .slice(at, at + 140)
        .replace(/\s+/g, " ") +
      "…";
    hits.push({ ...entry, snippet, score });
  }
  hits.sort((a, b) => b.score - a.score);
  // One hit per page and heading, at most ten.
  const seen = new Set<string>();
  return hits.filter((h) => !seen.has(h.slug + "#" + h.id) && seen.add(h.slug + "#" + h.id)).slice(0, 10);
}

function SearchBox({ onGo }: { onGo: () => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const hits = useMemo(() => search(query), [query]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement).closest("input, textarea, select, [contenteditable]");
      if ((e.key === "/" && !typing) || (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey))) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, []);
  const href = (hit: Hit) => docPath(hit.slug) + (hit.id ? `#${hit.id}` : "");
  const go = (hit: Hit) => {
    setQuery("");
    onGo();
    if (docPath(hit.slug) === location.pathname) location.hash = hit.id;
    else navigate(href(hit));
  };
  return (
    <div className="docs-search" role="search">
      <Search size={16} aria-hidden="true" />
      <input
        ref={input}
        type="search"
        placeholder="Search the docs"
        aria-label="Search the docs"
        value={query}
        onChange={(e) => (setQuery(e.target.value), setActive(0))}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") (e.preventDefault(), setActive((a) => Math.min(a + 1, hits.length - 1)));
          else if (e.key === "ArrowUp") (e.preventDefault(), setActive((a) => Math.max(a - 1, 0)));
          else if (e.key === "Enter" && hits[active]) (e.preventDefault(), go(hits[active]));
          else if (e.key === "Escape") setQuery("");
        }}
      />
      <kbd aria-hidden="true">/</kbd>
      {query ? (
        <div className="docs-hits" role="listbox" aria-label="Results">
          {hits.length ? (
            hits.map((hit, i) => (
              <a
                key={hit.slug + hit.id}
                href={href(hit)}
                role="option"
                aria-selected={i === active}
                className={i === active ? "on" : ""}
                onMouseEnter={() => setActive(i)}
                onClick={() => (setQuery(""), onGo())}
              >
                <strong>
                  {hit.title}
                  {hit.heading ? <span> › {hit.heading}</span> : null}
                </strong>
                <small>{hit.snippet}</small>
              </a>
            ))
          ) : (
            <p className="docs-no-hits">Nothing found. Try fewer or other words.</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

// ---------- layout ----------
function Sidebar({ slug, open, onGo }: { slug: string; open: boolean; onGo: () => void }) {
  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set());
  const link = (item: string) => {
    const page = pages.get(item)!;
    return (
      <li key={item}>
        <a href={docPath(item)} aria-current={item === slug ? "page" : undefined} onClick={onGo}>
          {page.title}
        </a>
      </li>
    );
  };
  return (
    <aside className={"docs-sidebar" + (open ? " open" : "")} aria-label="Documentation">
      <SearchBox onGo={onGo} />
      <a className="docs-console-link" href="/console">
        Open the console <ArrowRight size={14} aria-hidden="true" />
      </a>
      {nav.map((section: NavSection) => (
        <div key={section.title} className="docs-nav-section">
          <h2>{section.title}</h2>
          <ul>
            {section.items.map((item) => {
              if (typeof item === "string") return link(item);
              const expanded = openGroups.has(item.group) || item.items.includes(slug);
              return (
                <li key={item.group} className="docs-nav-group">
                  <button
                    type="button"
                    aria-expanded={expanded}
                    onClick={() =>
                      setOpenGroups((now) => {
                        const next = new Set(now);
                        if (expanded) next.delete(item.group);
                        else next.add(item.group);
                        return next;
                      })
                    }
                  >
                    {item.group}
                    <ChevronDown size={14} aria-hidden="true" />
                  </button>
                  {expanded ? <ul>{item.items.map(link)}</ul> : null}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </aside>
  );
}

function OnThisPage({ blocks }: { blocks: Block[] }) {
  const headings = blocks.filter(
    (b): b is Extract<Block, { kind: "heading" }> => b.kind === "heading" && (b.level === 2 || b.level === 3),
  );
  const [current, setCurrent] = useState("");
  useEffect(() => {
    const elements = headings.map((h) => document.getElementById(h.id)).filter(Boolean) as HTMLElement[];
    const spy = () => {
      const passed = elements.filter((e) => e.getBoundingClientRect().top < 120);
      setCurrent(passed.length ? passed[passed.length - 1].id : (elements[0]?.id ?? ""));
    };
    spy();
    window.addEventListener("scroll", spy, { passive: true });
    return () => window.removeEventListener("scroll", spy);
  }, [blocks]);
  if (headings.length < 2) return <div className="docs-toc" />;
  return (
    // Not <nav>: the site styles those as its header menu.
    <div className="docs-toc" role="navigation" aria-label="On this page">
      <h2>On this page</h2>
      <ul>
        {headings.map((h) => (
          <li key={h.id} className={(h.level === 3 ? "sub" : "") + (h.id === current ? " on" : "")}>
            <a href={`#${h.id}`}>{h.text}</a>
          </li>
        ))}
      </ul>
    </div>
  );
}

function sectionOf(slug: string): string {
  return (
    nav.find((s) => s.items.some((i) => (typeof i === "string" ? i === slug : i.items.includes(slug))))?.title ?? ""
  );
}

export default function DocsSite({ path }: { path: string }) {
  const slug = path.replace(/^\/docs\/?/, "").replace(/\/$/, "");
  const page = pages.get(slug);
  const [lang, setLang] = useState(() => {
    const saved = read("docs-language");
    return saved && TAB_LANGUAGES.includes(saved) ? saved : "curl";
  });
  const [menu, setMenu] = useState(false);
  const blocks = page ? blocksOf(page) : [];
  useEffect(() => {
    document.title = page ? `${page.title} · Open Alvary docs` : "Page not found · Open Alvary docs";
    setMenu(false);
    const anchor = decodeURIComponent(location.hash.slice(1));
    if (anchor) requestAnimationFrame(() => document.getElementById(anchor)?.scrollIntoView());
  }, [slug]);
  const at = ORDER.indexOf(slug);
  const previous = at > 0 ? pages.get(ORDER[at - 1]) : undefined;
  const next = at >= 0 && at < ORDER.length - 1 ? pages.get(ORDER[at + 1]) : undefined;
  const markdownUrl = `/docs/${slug || "index"}.md`;
  return (
    <Language.Provider value={{ lang, choose: (l) => (setLang(l), write("docs-language", l)) }}>
      <div className="docs">
        <button type="button" className="docs-menu-button" aria-expanded={menu} onClick={() => setMenu(!menu)}>
          {menu ? <X size={16} aria-hidden="true" /> : <Menu size={16} aria-hidden="true" />}
          {page ? `${sectionOf(slug)} › ${page.title}` : "Documentation"}
        </button>
        <Sidebar slug={slug} open={menu} onGo={() => setMenu(false)} />
        <article className="docs-page">
          {page ? (
            <>
              <div className="docs-head">
                <p className="docs-section">{sectionOf(slug)}</p>
                <div className="docs-actions">
                  <CopyButton text={`# ${page.title}\n\n${page.markdown}\n`} label="Copy page" />
                  <a className="docs-copy" href={markdownUrl}>
                    <FileText size={14} aria-hidden="true" />
                    <span>Markdown</span>
                  </a>
                </div>
              </div>
              <h1>{page.title}</h1>
              {page.description ? (
                <p className="docs-lede">
                  <Inlines inline={parseInline(page.description)} />
                </p>
              ) : null}
              <Blocks blocks={blocks} />
              <div className="docs-pager" role="navigation" aria-label="Previous and next">
                {previous ? (
                  <a href={docPath(previous.slug)} className="prev">
                    <small>
                      <ArrowLeft size={14} aria-hidden="true" /> Previous
                    </small>
                    {previous.title}
                  </a>
                ) : (
                  <span />
                )}
                {next ? (
                  <a href={docPath(next.slug)} className="next">
                    <small>
                      Next <ArrowRight size={14} aria-hidden="true" />
                    </small>
                    {next.title}
                  </a>
                ) : null}
              </div>
              <p className="docs-foot">
                Something wrong or missing on this page? Write to <a href="mailto:hello@ruhu.ai">hello@ruhu.ai</a>.
              </p>
            </>
          ) : (
            <>
              <h1>Page not found</h1>
              <p className="docs-lede">
                There is no documentation page at this address. Search above, or start from the{" "}
                <a href="/docs">overview</a>.
              </p>
            </>
          )}
        </article>
        <OnThisPage blocks={blocks} />
      </div>
    </Language.Provider>
  );
}
