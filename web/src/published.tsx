// A published text on its record page: one language at a time, laid out from the structure the
// public API serves (published-text.ts), with a Contents panel beside it. The look is the review
// workbench's reader (document.tsx), whose page blocks the public API does not serve. Text is
// rendered as React text, never as HTML.

import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import {
  chooseVersion,
  foldsAway,
  layoutText,
  nodesFor,
  type Laid,
  type Piece,
  type TextNode,
  type TextVersion,
} from "./published-text";
import { LANGUAGE_NAMES } from "./site";

export type PublishedVersion = TextVersion & { retrieved_at: string };
export type PublishedVersions = {
  items: PublishedVersion[];
  structure?: TextNode[];
  notice?: string | null;
};

export type Reading = {
  versions: PublishedVersion[];
  version: PublishedVersion;
  laid: Laid | null; // null: the text has no structure, and is shown as plain text
  choose: (version: PublishedVersion) => void;
};

function asked() {
  const query = new URLSearchParams(location.search);
  return { version: query.get("version"), language: query.get("lang") };
}

// The text being read and its layout. The choice of language is kept in the address (?lang=fr), so
// a link to an article opens in the language it was copied in.
export function useReading(
  data: PublishedVersions | undefined,
  recordLanguage?: string | null,
): Reading | null {
  const [chosen, setChosen] = useState<string | null>(null);
  const versions = data?.items;
  const version = useMemo(() => {
    if (!versions?.length) return undefined;
    return (
      versions.find((v) => v.id === chosen) ??
      chooseVersion(versions, recordLanguage, asked())
    );
  }, [versions, chosen, recordLanguage]);
  const laid = useMemo(() => {
    if (!version) return null;
    const nodes = nodesFor(version, data?.structure);
    return nodes.length ? layoutText(version.canonical_text, nodes) : null;
  }, [version, data?.structure]);
  if (!versions || !version) return null;
  return {
    versions,
    version,
    laid,
    choose: (next) => {
      setChosen(next.id);
      const query = new URLSearchParams(location.search);
      if (next.language) query.set("lang", next.language);
      else query.delete("lang");
      // Named by its language unless the language has several texts.
      if (
        chooseVersion(versions, null, { language: next.language })?.id ===
        next.id
      )
        query.delete("version");
      else query.set("version", next.id);
      // An article's anchor belongs to the text it was read in.
      history.replaceState(
        history.state,
        "",
        location.pathname + "?" + query.toString(),
      );
    },
  };
}

function languageName(code?: string | null) {
  return (code && LANGUAGE_NAMES[code]) || code || "Text";
}

// The record's texts by language; a language with several texts tells them apart by date.
export function TextLanguages({ reading }: { reading: Reading }) {
  const { versions, version: current } = reading;
  if (versions.length < 2) return null;
  const several = (v: PublishedVersion) =>
    versions.filter((w) => w.language === v.language).length > 1;
  return (
    <nav className="doc-languages pub-languages" aria-label="Language versions">
      {versions.map((v) => (
        <a
          key={v.id}
          href={
            "?lang=" +
            encodeURIComponent(v.language ?? "") +
            (several(v) ? "&version=" + encodeURIComponent(v.id) : "")
          }
          lang={v.language ?? undefined}
          hrefLang={v.language ?? undefined}
          aria-current={v.id === current.id ? "true" : undefined}
          className={v.id === current.id ? "current" : undefined}
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
            reading.choose(v);
          }}
        >
          {languageName(v.language)}
          {several(v) ? (
            <span className="doc-languages-note" lang="en">
              {v.effective_from
                ? "from " + v.effective_from
                : "read " + v.retrieved_at.slice(0, 10)}
            </span>
          ) : null}
        </a>
      ))}
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
    <div className="doc-text pub-text" lang={lang}>
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
