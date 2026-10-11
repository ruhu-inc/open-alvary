// A reviewed legal document as the public reads it: headings, numbered provisions, margin notes
// as provision titles, character formatting and tables, page by page. Text is rendered as React
// text, never as HTML.

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ChevronRight, FileText, Link2 } from "lucide-react";
import {
  DetailsPanels,
  KeyFacts,
  ReviewPanel,
  SourcePanel,
  citationFor,
  kindOf,
  summaryOf,
  titleOf,
  type DetailsStatus,
  type Provenance,
  type ReviewRecord,
  type Step,
  type View,
} from "./details";
import { DocumentTools, copyText, type DownloadLink } from "./tools";
import {
  PageTitle,
  SourceRecord,
  type RightsRecord,
  type Source,
  LANGUAGE_NAMES,
  type LanguageVersion,
} from "./site";

export type Mark = {
  type: "bold" | "italic" | "underline" | "superscript" | "subscript";
  start: number;
  end: number;
};

export type DocumentBlock =
  | {
      kind: "heading" | "paragraph" | "list_item";
      text: string;
      marker?: string;
      level?: number | null;
      align?: "left" | "center" | "right" | "justify" | null;
      marks?: Mark[];
      notes?: string[];
    }
  | {
      kind: "table";
      caption?: string;
      header_rows: number;
      rows: string[][];
    };

export type DocumentPageContent = {
  page: number;
  checked: boolean;
  blocks: DocumentBlock[];
};

// Numbering as printed: "3." a section, "(1)" a subsection, "(a)" a paragraph, "(ii)" a
// sub-paragraph. A section number may be read as a separate marker or as the first words.
const DEPTH: [RegExp, number][] = [
  [/^\d{1,3}[A-Z]?\./, 0],
  [/^\(\d{1,3}[A-Z]?\)/, 1],
  [/^\([ivxl]{2,6}\)/, 3],
  [/^\([a-z]{1,2}\)/, 2],
];
const SECTION_LEAD = /^\d{1,3}[A-Z]?\.\s+/;

function depthOf(lead: string): number | null {
  const text = lead.trimStart();
  for (const [pattern, depth] of DEPTH) if (pattern.test(text)) return depth;
  return null;
}

const TAGS = {
  bold: "strong",
  italic: "em",
  underline: "u",
  superscript: "sup",
  subscript: "sub",
} as const;

function Formatted({ text, marks = [] }: { text: string; marks?: Mark[] }) {
  if (!marks.length) return <>{text}</>;
  const cuts = [
    ...new Set([0, text.length, ...marks.flatMap((m) => [m.start, m.end])]),
  ]
    .filter((cut) => cut >= 0 && cut <= text.length)
    .sort((a, b) => a - b);
  return (
    <>
      {cuts.slice(0, -1).map((from, i) => {
        const to = cuts[i + 1];
        let piece: ReactNode = text.slice(from, to);
        for (const mark of marks)
          if (mark.start <= from && mark.end >= to) {
            const Tag = TAGS[mark.type];
            piece = <Tag>{piece}</Tag>;
          }
        return <span key={from}>{piece}</span>;
      })}
    </>
  );
}

// Splits a provision's number from its words, moving formatting with the words.
function split(block: Extract<DocumentBlock, { text: string }>) {
  const text = block.text ?? "";
  if (block.marker)
    return { lead: block.marker.trim(), text, marks: block.marks };
  const match = SECTION_LEAD.exec(text);
  if (!match) return { lead: null, text, marks: block.marks };
  const cut = match[0].length;
  const marks = (block.marks ?? [])
    .map((m) => ({
      ...m,
      start: Math.max(m.start, cut) - cut,
      end: m.end - cut,
    }))
    .filter((m) => m.end > m.start);
  return { lead: match[0].trim(), text: text.slice(cut), marks };
}

type Entry = { id: string; label: string; heading: boolean };

// The document's blocks laid out with stable anchors, and its contents: Parts and the titled
// sections under them. A heading with no titled section after it (as in an arrangement of
// sections) is left out of the contents. "#section-4" belongs to section 4 itself, not to its
// line in an arrangement of sections, so titled sections claim their anchors first.
function sectionOf(block: DocumentBlock) {
  if (block.kind === "table" || block.kind === "heading") return null;
  const { lead } = split(block);
  return lead !== null && depthOf(lead) === 0 ? lead : null;
}

function layout(pages: DocumentPageContent[]) {
  const titledNumbers = new Set(
    pages.flatMap((page) =>
      page.blocks
        .filter((block) => block.kind !== "table" && block.notes?.length)
        .map(sectionOf)
        .filter((section) => section !== null),
    ),
  );
  const used = new Set<string>();
  const anchor = (wanted: string) => {
    let id = wanted;
    for (let n = 2; used.has(id); n++) id = `${wanted}-${n}`;
    used.add(id);
    return id;
  };
  const contents: Entry[] = [];
  let pendingHeading: Entry | null = null;
  const laid = pages.map((page) => ({
    ...page,
    items: page.blocks.map((block, index) => {
      const key = `${page.page}-${index}`;
      if (block.kind === "table")
        return {
          key,
          block,
          id: null,
          lead: null,
          section: null,
          titled: false,
        };
      if (block.kind === "heading") {
        const id = anchor("part-" + key);
        pendingHeading = { id, label: block.text ?? "", heading: true };
        return { key, block, id, lead: null, section: null, titled: false };
      }
      const parts = split(block);
      const section = sectionOf(block);
      const titled = section !== null && Boolean(block.notes?.length);
      const id =
        section && (titled || !titledNumbers.has(section))
          ? anchor("section-" + section.replace(/\.$/, ""))
          : null;
      if (titled && id) {
        if (pendingHeading) contents.push(pendingHeading);
        pendingHeading = null;
        contents.push({
          id,
          label: `${section} ${block.notes![0]}`,
          heading: false,
        });
      }
      return { key, block, id, lead: parts, section, titled };
    }),
  }));
  return { laid, contents };
}

function scrollTo(id: string) {
  const target = document.getElementById(id);
  target?.scrollIntoView({ block: "start" });
  target?.focus({ preventScroll: true });
}

// A law's language versions, the one prevailing in a conflict marked as such.
export function LanguageSwitch({
  versions,
  current,
  href,
  view,
}: {
  versions: LanguageVersion[];
  current: string;
  href: (id: string) => string;
  view: View;
}) {
  const rows =
    (view.values["expressions"] as {
      language?: string;
      authority?: string;
    }[]) || [];
  const prevails = rows.find(
    (row) => row.authority === "authoritative",
  )?.language;
  return (
    <nav className="doc-languages" aria-label="Language versions">
      {versions.map((version) => (
        <a
          key={version.id}
          href={href(version.id)}
          lang={version.language}
          hrefLang={version.language}
          title={version.title}
          aria-current={version.id === current ? "page" : undefined}
          className={version.id === current ? "current" : undefined}
        >
          {LANGUAGE_NAMES[version.language] || version.language}
          {version.language === prevails ? (
            <span className="doc-languages-note">prevails</span>
          ) : null}
        </a>
      ))}
    </nav>
  );
}

export function DocumentText({
  pages,
  lang,
  markUnchecked = false,
  onSectionLink,
}: {
  pages: DocumentPageContent[];
  lang?: string;
  markUnchecked?: boolean;
  onSectionLink?: (section: string, anchor: string) => void;
}) {
  const { laid } = layout(pages);
  return (
    <div className="doc-text" lang={lang}>
      {laid.map((page) => (
        <div
          key={page.page}
          className={
            "doc-pageblock" +
            (markUnchecked && !page.checked ? " doc-unchecked" : "")
          }
        >
          <div className="doc-page" id={"page-" + page.page} tabIndex={-1}>
            <span>Page {page.page}</span>
            {markUnchecked && !page.checked ? (
              <span className="doc-page-flag">not yet checked</span>
            ) : null}
          </div>
          {page.items.map((item) => {
            const { block } = item;
            if (block.kind === "table")
              return (
                <div
                  key={item.key}
                  className="reader-table"
                  role="region"
                  aria-label={block.caption || "Table"}
                  tabIndex={0}
                >
                  <table>
                    {block.caption ? <caption>{block.caption}</caption> : null}
                    <tbody>
                      {block.rows.map((row, r) => (
                        <tr key={r}>
                          {row.map((cell, c) =>
                            r < block.header_rows ? (
                              <th key={c} scope="col">
                                {cell}
                              </th>
                            ) : (
                              <td key={c}>{cell}</td>
                            ),
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              );
            const align = block.align ? " doc-align-" + block.align : "";
            if (block.kind === "heading")
              return (
                <h2
                  key={item.key}
                  id={item.id ?? undefined}
                  tabIndex={-1}
                  className={"doc-heading" + align}
                >
                  <Formatted text={block.text ?? ""} marks={block.marks} />
                </h2>
              );
            const parts = item.lead!;
            const level = block.level ?? depthOf(parts.lead ?? parts.text) ?? 0;
            if (item.section && item.titled) {
              // A section's first margin note is its title, as legislation reads on the web;
              // further notes (often cross-references) stay beside it in small type.
              const [title, ...more] = block.notes!;
              return (
                <section key={item.key} className="doc-section">
                  <h3 id={item.id ?? undefined} tabIndex={-1}>
                    <span className="doc-num">{item.section}</span>
                    {title}
                    {onSectionLink && item.id ? (
                      <button
                        type="button"
                        className="doc-permalink"
                        aria-label={`Copy a link to section ${item.section}`}
                        title="Copy a link to this section"
                        onClick={() =>
                          onSectionLink(
                            item.section!.replace(/\.$/, ""),
                            item.id!,
                          )
                        }
                      >
                        <Link2 size={15} />
                      </button>
                    ) : null}
                  </h3>
                  {more.length ? (
                    <p className="doc-note-more">{more.join(" · ")}</p>
                  ) : null}
                  <p className={"doc-p doc-lvl0" + align}>
                    <Formatted text={parts.text} marks={parts.marks} />
                  </p>
                </section>
              );
            }
            return (
              <div key={item.key}>
                {block.notes?.length ? (
                  <p className="doc-note">{block.notes.join(" ")}</p>
                ) : null}
                <p
                  id={item.id ?? undefined}
                  className={
                    `doc-p doc-lvl${level}` +
                    (item.section ? " doc-hang" : "") +
                    align
                  }
                >
                  {parts.lead !== null ? (
                    <span className="doc-num">{parts.lead} </span>
                  ) : null}
                  <Formatted text={parts.text} marks={parts.marks} />
                </p>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

export function DocumentContents({ pages }: { pages: DocumentPageContent[] }) {
  const { contents } = layout(pages);
  if (!contents.length) return null;
  return (
    <nav className="info-panel doc-contents" aria-label="Contents">
      <h3>Contents</h3>
      <ol>
        {contents.map((entry) => (
          <li
            key={entry.id}
            className={entry.heading ? "doc-contents-part" : ""}
          >
            <a
              href={"#" + entry.id}
              onClick={(event) => {
                // The site uses hash routing; move to the provision instead of changing route.
                event.preventDefault();
                scrollTo(entry.id);
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

// The page a reader reaches from a search result.
export function DocumentPage({
  source,
  rights,
  pages,
  contentHash,
  detailsHash,
  view,
  making,
  details,
  provenance,
  review,
  downloads = [],
  approved = false,
  linkTo,
  markUnchecked = false,
  versions = [],
  versionHref,
}: {
  source: Source;
  rights?: RightsRecord;
  pages: DocumentPageContent[];
  contentHash?: string;
  detailsHash?: string | null;
  view: View;
  making: Step[];
  details: DetailsStatus;
  provenance: Provenance;
  review: ReviewRecord;
  downloads?: DownloadLink[];
  approved?: boolean;
  linkTo: (query?: string) => string; // this page's address, optionally at a section
  markUnchecked?: boolean;
  versions?: LanguageVersion[]; // the law's other language versions, each its own page
  versionHref?: (id: string) => string;
}) {
  const s = source;
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 6000);
    return () => clearTimeout(timer);
  }, [notice]);
  const textRoot = useCallback(() => document.querySelector(".doc-text"), []);
  const citation = citationFor(view, s);
  // A law printed in several languages shares one record; each version shows its own title.
  const title = versions.length > 1 ? s.title : titleOf(view, s);
  return (
    <div className="page doc-page-view">
      <a className="breadcrumb" href="#/browse">
        Catalogue <ChevronRight size={14} /> {s.jurisdiction_id.toUpperCase()}
      </a>
      <PageTitle
        label={kindOf(view).toUpperCase() + " · " + citation}
        title={title}
        lang={s.language}
      >
        {summaryOf(view) || s.catalogue_note}
      </PageTitle>
      <KeyFacts view={view} making={making} approved={approved} />
      <div className="doc-overview">
        <DetailsPanels view={view} making={making} />
      </div>
      <div className="reader-grid">
        <article className="reader doc-reader">
          <div className="reader-bar">
            <FileText size={18} /> Source text{" "}
            <span className="badge green">Open full text</span>
          </div>
          {versions.length > 1 && versionHref ? (
            <LanguageSwitch
              versions={versions}
              current={s.id}
              href={versionHref}
              view={view}
            />
          ) : null}
          <DocumentTools
            citation={citation}
            pageUrl={linkTo()}
            downloads={downloads}
            textRoot={textRoot}
          />
          <DocumentText
            pages={pages}
            lang={s.language}
            markUnchecked={markUnchecked}
            onSectionLink={async (section, anchor) => {
              const url = linkTo("section=" + encodeURIComponent(section));
              const copied = await copyText(url);
              setNotice(
                copied
                  ? `Copied a link to section ${section} (${citationFor(view, s, section)})`
                  : `Link to section ${section}: ${url}`,
              );
              document.getElementById(anchor)?.focus({ preventScroll: true });
            }}
          />
          {contentHash ? <p className="hash">SHA-256 {contentHash}</p> : null}
        </article>
        <aside className="doc-aside">
          <DocumentContents pages={pages} />
          <ReviewPanel
            review={review}
            details={details}
            contentHash={contentHash}
            detailsHash={detailsHash}
          />
          <SourcePanel view={view} provenance={provenance} />
          <SourceRecord source={s} rights={rights} />
          {provenance.rights_basis ? (
            <p className="doc-panel-note doc-rights-basis">
              Proposed rights basis, pending rights review:{" "}
              {provenance.rights_basis}
            </p>
          ) : null}
        </aside>
      </div>
      <p className="doc-notice" role="status" hidden={!notice}>
        {notice}
      </p>
    </div>
  );
}
