// A published text on its record page: one language at a time, laid out from the structure the
// website's data path serves (published-text.ts), with a Contents panel beside it. The look is the
// review workbench's reader (document.tsx). Text is rendered as React text, never as HTML.
//
// The page is sent one text at a time as `reader-1` (/site/v1/sources/{id}/text): the server
// chooses the text to open (the one a link names, else the record's own language) and says what a
// link asked for that the record does not have. The switch fetches another text when it is asked
// for. Every state a reader can meet is named here; none shows a status number alone.

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

const SCHEMA = "reader-1";

// A public text of the record without its words, for the switch.
export type ListedText = {
  id: string;
  language?: string | null;
  effective_from?: string | null;
  retrieved_at: string;
};
type Missed = ("version" | "language")[];
type ReaderAnswer = {
  schema: string;
  text: Omit<TextVersion, "canonical_text"> | null; // null: the record has no public text
  texts: ListedText[];
  missed: Missed | null;
  canonical_text?: string;
  nodes: TextNode[];
};

export type Reading = {
  texts: ListedText[];
  version: TextVersion;
  laid: Laid | null; // null: the text has no structure, and is shown as plain text
  pending: string | null; // the text being fetched, by its ID, while the one before it stays shown
  failed: boolean; // the text last asked for could not be fetched
  line: string | null; // what the link asked for that the record does not have
  choose: (text: ListedText) => void;
};

// Why there is no text to read. "none": the record has no public text. "gone": the record is not
// public (any more). "outdated": the data path speaks a newer contract than this page.
export type TextState =
  | "loading"
  | "ready"
  | "none"
  | "gone"
  | "outdated"
  | "too-large"
  | "busy"
  | "failed";

// What the data path takes for a version or a language: an ID.
const NAMED = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$/;

// What the address asks for. A value that is not an ID is not sent, and counts as something the
// record does not have.
function asked() {
  const query = new URLSearchParams(location.search);
  const read = (name: "version" | "lang") => {
    const value = query.get(name);
    return {
      value: value && NAMED.test(value) ? value : null,
      odd: value !== null && !NAMED.test(value),
    };
  };
  return { version: read("version"), language: read("lang") };
}

// The address names the text shown (?lang=fr); a language with several texts names the text too.
function nameInAddress(
  text: { id: string; language?: string | null },
  texts: ListedText[],
  keepAnchor: boolean,
) {
  const query = new URLSearchParams(location.search);
  if (text.language) query.set("lang", text.language);
  else query.delete("lang");
  if (texts.filter((other) => other.language === text.language).length > 1)
    query.set("version", text.id);
  else query.delete("version");
  const named = query.toString();
  history.replaceState(
    history.state,
    "",
    location.pathname +
      (named ? "?" + named : "") +
      (keepAnchor ? location.hash : ""),
  );
}

function languageName(code?: string | null) {
  return (code && LANGUAGE_NAMES[code.toLowerCase()]) || null;
}

// The line for a link that asked for a text the record does not have (PRD-03). It never repeats a
// value the site has no name for.
function missedLine(
  missed: Missed,
  askedLanguage: string | null,
  shown: string | null | undefined,
) {
  const showing = languageName(shown)
    ? `Showing the ${languageName(shown)} text.`
    : "Showing the available text.";
  if (missed.includes("version"))
    return `The text this link names is not here. ${showing}`;
  const name = languageName(askedLanguage);
  return name
    ? `This record has no ${name} text. ${showing}`
    : `This record has no text in that language. ${showing}`;
}

class Refused extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

// What a refusal means for the reader, by its code and never by its status alone.
function stateOf(problem: unknown): TextState {
  if (!(problem instanceof Refused)) return "failed";
  if (problem.status === 404 || problem.code === "unavailable") return "gone";
  if (problem.code === "result_too_large") return "too-large";
  if (
    ["rate_limited", "capacity_exhausted", "temporarily_unavailable"].includes(
      problem.code,
    )
  )
    return "busy";
  return "failed";
}

type Held = {
  source: string;
  state: TextState;
  answer?: ReaderAnswer;
  line?: string | null;
  pending?: string;
  failed?: boolean;
};

// The text being read and its layout, fetched one text at a time. A text already read in this visit
// to the record is kept in the page's memory, so going back to a language asks for nothing; it ends
// when the reader leaves the record, and nothing is stored in the browser.
export function useReading(source: string): {
  reading: Reading | null;
  state: TextState;
  retry: () => void;
} {
  // The text chosen with the switch (until then, whatever the address names), and each new try.
  const [want, setWant] = useState<{
    source: string;
    version: string | null;
    attempt: number;
  }>({ source, version: null, attempt: 0 });
  const [held, setHeld] = useState<Held>({ source: "", state: "loading" });
  const read = useRef({ source, answers: new Map<string, ReaderAnswer>() });
  const version = want.source === source ? want.version : null;
  const attempt = want.source === source ? want.attempt : 0;
  useEffect(() => {
    if (read.current.source !== source)
      read.current = { source, answers: new Map() };
    const answers = read.current.answers;
    const kept = version ? answers.get(version) : undefined;
    if (kept?.text) {
      // A text chosen with the switch: the anchor belonged to the text before it.
      setHeld({ source, state: "ready", answer: kept });
      nameInAddress(kept.text, kept.texts, false);
      return;
    }
    const link = asked();
    const query = new URLSearchParams();
    if (version) query.set("version", version);
    else {
      if (link.version.value) query.set("version", link.version.value);
      if (link.language.value) query.set("language", link.language.value);
    }
    const controller = new AbortController();
    setHeld((before) =>
      before.source === source && before.answer?.text && version
        ? { ...before, pending: version, failed: false }
        : { source, state: "loading" },
    );
    fetch(
      "/site/v1/sources/" +
        encodeURIComponent(source) +
        "/text" +
        (query.size ? "?" + query.toString() : ""),
      { signal: controller.signal },
    )
      .then(async (response) => {
        if (response.ok) return response.json() as Promise<ReaderAnswer>;
        const problem = await response.json().catch(() => ({}));
        throw new Refused(response.status, String(problem?.code ?? ""));
      })
      .then((answer) => {
        if (controller.signal.aborted) return;
        if (answer?.schema !== SCHEMA)
          return setHeld({ source, state: "outdated" });
        if (!answer.text) return setHeld({ source, state: "none", answer });
        answers.set(answer.text.id, answer);
        if (version) {
          setHeld({ source, state: "ready", answer });
          return nameInAddress(answer.text, answer.texts, false);
        }
        // Opened from a link. What it asked for that is not there, in one line; and an address
        // that names the text shown. The anchor stays only if the text it belongs to was found.
        const missed: Missed = [
          ...(answer.missed ?? []),
          ...(link.version.odd && !answer.missed?.includes("version")
            ? (["version"] as const)
            : []),
          ...(link.language.odd && !answer.missed?.includes("language")
            ? (["language"] as const)
            : []),
        ];
        const line = missed.length
          ? missedLine(missed, link.language.value, answer.text.language)
          : null;
        setHeld({ source, state: "ready", answer, line });
        const named = new URLSearchParams(location.search);
        const other =
          (named.get("lang") ?? answer.text.language ?? "").toLowerCase() !==
          (answer.text.language ?? "").toLowerCase();
        if (missed.length || other)
          nameInAddress(answer.text, answer.texts, !missed.length);
      })
      .catch((problem) => {
        if (controller.signal.aborted || problem.name === "AbortError") return;
        const state = stateOf(problem);
        // A record that has gone takes its text with it. Any other failure of a switch leaves the
        // text being read where it is.
        setHeld((before) =>
          version &&
          before.source === source &&
          before.answer?.text &&
          state !== "gone"
            ? { ...before, pending: undefined, failed: true }
            : { source, state },
        );
      });
    return () => controller.abort();
  }, [source, version, attempt]);

  const mine: Held =
    held.source === source ? held : { source, state: "loading" };
  const answer = mine.answer;
  const text = useMemo(
    () =>
      answer?.text
        ? { ...answer.text, canonical_text: answer.canonical_text ?? "" }
        : null,
    [answer],
  );
  const laid = useMemo(() => {
    if (!text) return null;
    const nodes = nodesFor(text, answer?.nodes);
    return nodes.length ? layoutText(text.canonical_text, nodes) : null;
  }, [text, answer]);
  return {
    state: mine.state,
    retry: () => setWant({ source, version, attempt: attempt + 1 }),
    reading:
      answer && text && mine.state === "ready"
        ? {
            texts: answer.texts,
            version: text,
            laid,
            pending: mine.pending ?? null,
            failed: Boolean(mine.failed),
            line: mine.line ?? null,
            choose: (next) => {
              if (next.id === text.id && !mine.failed) return;
              // The line belonged to the link that was opened: it goes with the first choice.
              setHeld((before) => ({ ...before, line: null }));
              setWant({ source, version: next.id, attempt: attempt + 1 });
            },
          }
        : null,
  };
}

// Why there is no text, in words, with what the reader can do about it.
export function TextProblem({
  state,
  official,
  retry,
}: {
  state: TextState;
  official: string;
  retry: () => void;
}) {
  if (state === "loading")
    return (
      <div className="notice" role="status">
        Loading the text…
      </div>
    );
  const again = (
    <button type="button" className="button secondary" onClick={retry}>
      Try again
    </button>
  );
  return (
    <div className="notice pub-problem" role="alert">
      {state === "too-large" ? (
        <p>
          This text is too large to show on this page. Read the{" "}
          <a href={official} target="_blank" rel="noreferrer">
            official copy
          </a>
          .
        </p>
      ) : state === "outdated" ? (
        <>
          <p>This page is out of date.</p>
          <button
            type="button"
            className="button secondary"
            onClick={() => location.reload()}
          >
            Reload
          </button>
        </>
      ) : state === "busy" ? (
        <>
          <p>The text cannot be sent right now. Please try again shortly.</p>
          {again}
        </>
      ) : (
        <>
          <p>This text could not be loaded. Please try again.</p>
          {again}
        </>
      )}
    </div>
  );
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
          {languageName(text.language) ?? text.language ?? "Text"}
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
