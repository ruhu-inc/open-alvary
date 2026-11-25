// A published text on its record page: one language at a time, laid out from the structure the
// public API serves (published-text.ts), with a Contents panel beside it. The look is the review
// workbench's reader (document.tsx), whose page blocks the public API does not serve. Text is
// rendered as React text, never as HTML.
//
// The page is sent one text at a time (/site/v1/sources/{id}/text): a law in three languages is not
// refused for the size of all three, and the server chooses the text to open (the one a link names,
// else the record's own language). The switch fetches another text when it is asked for.

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ChevronDown } from "lucide-react";
import {
  foldsAway,
  layoutText,
  nodesFor,
  type Laid,
  type Piece,
  type TextNode,
  type TextVersion,
} from "./published-text";
import { LANGUAGE_NAMES } from "./site";

// A public text of the record without its words, for the switch.
export type ListedText = {
  id: string;
  language?: string | null;
  effective_from?: string | null;
  retrieved_at: string;
};
type ReaderPage = {
  items: TextVersion[]; // the one text, or none when the record has no public text
  texts: ListedText[];
  structure?: TextNode[];
  notice?: string | null;
};

export type Reading = {
  texts: ListedText[];
  version: TextVersion;
  laid: Laid | null; // null: the text has no structure, and is shown as plain text
  pending: string | null; // the text being fetched, by its ID, while the one before it stays shown
  failed: boolean; // the text last asked for could not be fetched
  choose: (text: ListedText) => void;
};

// What the data path takes for a version or a language: an ID. Anything else in a link is left out.
const NAMED = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$/;

function asked() {
  const query = new URLSearchParams(location.search);
  const named = (name: string) => {
    const value = query.get(name);
    return value && NAMED.test(value) ? value : null;
  };
  return { version: named("version"), language: named("lang") };
}

// The address names the text shown (?lang=fr), so a link to an article opens in the language it was
// copied in; a language with several texts names the text too. An article's anchor belongs to the
// text it was read in, so it is dropped.
function nameInAddress(text: TextVersion, texts: ListedText[]) {
  const query = new URLSearchParams(location.search);
  if (text.language) query.set("lang", text.language);
  else query.delete("lang");
  if (texts.filter((other) => other.language === text.language).length > 1)
    query.set("version", text.id);
  else query.delete("version");
  history.replaceState(
    history.state,
    "",
    location.pathname + "?" + query.toString(),
  );
}

type Held = {
  source: string;
  page?: ReaderPage;
  error?: string; // nothing could be shown
  pending?: string;
  failed?: boolean;
};

// The text being read and its layout, fetched one text at a time. A text already read in this visit
// to the record is kept, so going back to a language does not fetch it again.
export function useReading(source: string): {
  reading: Reading | null;
  loaded: boolean;
  error?: string;
} {
  // The text chosen with the switch; until then, whatever the address names.
  const [want, setWant] = useState<{
    source: string;
    version: string;
    attempt: number;
  } | null>(null);
  const [held, setHeld] = useState<Held>({ source: "" });
  const read = useRef({ source, pages: new Map<string, ReaderPage>() });
  const version = want?.source === source ? want.version : null;
  const attempt = want?.source === source ? want.attempt : 0;
  useEffect(() => {
    if (read.current.source !== source)
      read.current = { source, pages: new Map() };
    const pages = read.current.pages;
    const show = (page: ReaderPage) => {
      setHeld({ source, page });
      if (version && page.items[0]) nameInAddress(page.items[0], page.texts);
    };
    const kept = version ? pages.get(version) : undefined;
    if (kept) return show(kept);
    const query = new URLSearchParams();
    const link = asked();
    if (version) query.set("version", version);
    else {
      if (link.version) query.set("version", link.version);
      if (link.language) query.set("language", link.language);
    }
    const controller = new AbortController();
    setHeld((before) =>
      before.source === source && before.page && version
        ? { source, page: before.page, pending: version }
        : { source },
    );
    fetch(
      "/site/v1/sources/" +
        encodeURIComponent(source) +
        "/text" +
        (query.size ? "?" + query.toString() : ""),
      {
        signal: controller.signal,
        // Another text of the record being shown: the record's text is counted as read once.
        headers: version ? { "X-Text-Switch": "1" } : undefined,
      },
    )
      .then((answer) => {
        if (!answer.ok) throw new Error(`Request failed (${answer.status})`);
        return answer.json() as Promise<ReaderPage>;
      })
      .then((page) => {
        if (controller.signal.aborted) return;
        if (page.items[0]) pages.set(page.items[0].id, page);
        show(page);
      })
      .catch((problem) => {
        if (controller.signal.aborted || problem.name === "AbortError") return;
        // A text that cannot be fetched leaves the one being read where it is.
        setHeld((before) =>
          before.source === source && before.page
            ? { source, page: before.page, failed: true }
            : { source, error: problem.message },
        );
      });
    return () => controller.abort();
  }, [source, version, attempt]);

  const mine = held.source === source ? held : { source };
  const page = mine.page;
  const text = page?.items[0];
  const laid = useMemo(() => {
    if (!text) return null;
    const nodes = nodesFor(text, page?.structure);
    return nodes.length ? layoutText(text.canonical_text, nodes) : null;
  }, [text, page]);
  return {
    loaded: Boolean(page),
    error: mine.error,
    reading:
      page && text
        ? {
            texts: page.texts,
            version: text,
            laid,
            pending: mine.pending ?? null,
            failed: Boolean(mine.failed),
            choose: (next) => {
              if (next.id !== text.id || mine.failed)
                setWant({ source, version: next.id, attempt: attempt + 1 });
            },
          }
        : null,
  };
}

function languageName(code?: string | null) {
  return (code && LANGUAGE_NAMES[code]) || code || "Text";
}

// The record's texts by language; a language with several texts tells them apart by date.
export function TextLanguages({ reading }: { reading: Reading }) {
  const { texts, version: current } = reading;
  if (texts.length < 2) return null;
  const several = (text: ListedText) =>
    texts.filter((other) => other.language === text.language).length > 1;
  return (
    <nav className="doc-languages pub-languages" aria-label="Language versions">
      {texts.map((text) => (
        <a
          key={text.id}
          href={
            "?lang=" +
            encodeURIComponent(text.language ?? "") +
            (several(text) ? "&version=" + encodeURIComponent(text.id) : "")
          }
          lang={text.language ?? undefined}
          hrefLang={text.language ?? undefined}
          aria-current={text.id === current.id ? "true" : undefined}
          aria-busy={text.id === reading.pending ? "true" : undefined}
          className={text.id === current.id ? "current" : undefined}
          onClick={(event) => {
            if (
              event.metaKey ||
              event.ctrlKey ||
              event.shiftKey ||
              event.altKey
            )
              return;
            // The same page in another language: the reader stays where they are on it.
            event.preventDefault();
            reading.choose(text);
          }}
        >
          {languageName(text.language)}
          {several(text) ? (
            <span className="doc-languages-note" lang="en">
              {text.effective_from
                ? "from " + text.effective_from
                : "read " + text.retrieved_at.slice(0, 10)}
            </span>
          ) : null}
        </a>
      ))}
      {reading.failed ? (
        <span className="pub-languages-failed" role="alert">
          That text could not be loaded. Please try again.
        </span>
      ) : null}
    </nav>
  );
}

const CELL_STATES: Record<string, string> = {
  illegible: "Illegible in the original",
  unsupported: "Not text: not transcribed",
};

function Block({ piece }: { piece: Exclude<Piece, { type: "gap" }> }) {
  // What a link lands on takes focus, so a keyboard or screen reader carries on reading from it.
  const target = piece.id ? { id: piece.id, tabIndex: -1 } : {};
  if (piece.type === "heading") {
    const Tag = ("h" + piece.level) as "h3" | "h4" | "h5" | "h6";
    // A section's heading is its margin note, set as the workbench sets a titled section's.
    const look =
      piece.kind === "section"
        ? "pub-section-heading"
        : "doc-heading doc-rank-" + piece.rank;
    return (
      <Tag {...target} className={look}>
        {piece.text}
      </Tag>
    );
  }
  if (piece.type === "table")
    return (
      <div
        {...target}
        className="reader-table"
        role="region"
        aria-label="Table"
        tabIndex={0}
      >
        <table>
          <tbody>
            {piece.rows.map((row, r) => (
              <tr key={r}>
                {row.map((cell, c) => {
                  const state = piece.states[`${r},${c}`];
                  return (
                    <td key={c} data-state={state}>
                      {state ? (
                        <span className="cell-state">
                          {CELL_STATES[state] ?? state}
                        </span>
                      ) : null}
                      {cell}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  return (
    <p
      {...target}
      className={
        `doc-p doc-lvl${piece.depth}` + (piece.lead ? " doc-hang" : "")
      }
    >
      {piece.lead ? (
        <span className="doc-num">{piece.text.slice(0, piece.lead)}</span>
      ) : null}
      {piece.lead ? piece.text.slice(piece.lead) : piece.text}
    </p>
  );
}

// The text. Every character of it is in the page once, in order: the line feeds between two blocks
// are kept between their elements, where a browser shows nothing for them. A table's cells take the
// place of its tabs and line feeds.
//
// The law's own printed table of contents stays where it is printed, set apart. A long one that the
// Contents lists again is folded under its own heading until asked for: the Constitution's runs to
// some 230 lines before the first chapter.
export function PublishedText({ reading }: { reading: Reading }) {
  const { version, laid } = reading;
  const lang = version.language ?? undefined;
  useEffect(() => {
    // A printed copy has the whole text, whatever was folded on the screen.
    const unfold = () =>
      document
        .querySelectorAll<HTMLDetailsElement>("details.pub-printed-contents")
        .forEach((folded) => (folded.open = true));
    window.addEventListener("beforeprint", unfold);
    return () => window.removeEventListener("beforeprint", unfold);
  }, []);
  if (!laid)
    return (
      <pre className="legal-text" lang={lang}>
        {version.canonical_text}
      </pre>
    );
  const out: ReactNode[] = [];
  // The printed table of contents being gathered: its pieces, and how many of them are blocks.
  type Printed = { nodes: ReactNode[]; blocks: number; headed: boolean };
  let printed: Printed | null = null;
  const close = (key: number, run: Printed | null) => {
    printed = null;
    if (!run) return;
    const { nodes, blocks, headed } = run;
    if (headed && foldsAway(blocks, laid.contents.length)) {
      const [heading, ...rest] = nodes;
      out.push(
        <details
          key={"printed-" + key}
          className="pub-printed-contents"
          aria-label="Table of contents as printed"
        >
          <summary data-note={`as printed, ${blocks - 1} lines`}>
            {heading}
          </summary>
          {rest}
        </details>,
      );
      return;
    }
    out.push(
      <div
        key={"printed-" + key}
        className="pub-printed-contents"
        role="group"
        aria-label="Table of contents as printed"
      >
        {nodes}
      </div>,
    );
  };
  laid.pieces.forEach((piece, index) => {
    if (piece.type === "gap") {
      ((printed as Printed | null)?.nodes ?? out).push(piece.text);
      return;
    }
    if (!piece.printed) close(index, printed);
    else if (!printed)
      printed = { nodes: [], blocks: 0, headed: piece.type === "heading" };
    const block = <Block key={index} piece={piece} />;
    const run = printed as Printed | null;
    if (run) {
      run.nodes.push(block);
      run.blocks++;
    } else out.push(block);
  });
  close(laid.pieces.length, printed);
  return (
    <div
      className="doc-text pub-text"
      lang={lang}
      aria-busy={reading.pending ? "true" : undefined}
    >
      {out}
    </div>
  );
}

function useNarrow() {
  const query = "(max-width: 800px)";
  const [narrow, setNarrow] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const change = () => setNarrow(media.matches);
    change();
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  return narrow;
}

// The text's parts and provisions, each a link to its place in the text. Beside the text on a wide
// screen; above it and closed on a narrow one, where it would otherwise push the text off the screen.
export function TextContents({ reading }: { reading: Reading }) {
  const narrow = useNarrow();
  const [open, setOpen] = useState(false);
  const list = useId();
  const entries = reading.laid?.contents ?? [];
  if (!entries.length) return null;
  const shown = !narrow || open;
  return (
    <nav className="info-panel doc-contents pub-contents" aria-label="Contents">
      <h2 className="panel-title">
        {narrow ? (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={list}
            onClick={() => setOpen(!open)}
          >
            Contents <ChevronDown size={16} aria-hidden="true" />
          </button>
        ) : (
          "Contents"
        )}
      </h2>
      <ol
        id={list}
        hidden={!shown}
        lang={reading.version.language ?? undefined}
      >
        {entries.map((entry, index) => (
          <li
            key={index}
            className={entry.part ? "doc-contents-part" : undefined}
          >
            <a
              href={"#" + entry.id}
              onClick={(event) => {
                if (
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                )
                  return;
                event.preventDefault();
                const target = document.getElementById(entry.id);
                if (!target) return;
                // The address names the provision, so it can be copied; Back still leaves the record.
                history.replaceState(
                  history.state,
                  "",
                  location.pathname + location.search + "#" + entry.id,
                );
                target.scrollIntoView({ block: "start" });
                target.focus({ preventScroll: true });
              }}
            >
              {entry.label}
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}
