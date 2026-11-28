// Pieces of the public site shared by the site itself and the review workbench's preview of it,
// so the preview shows exactly what readers will see.

import React, { createContext, useContext } from "react";
import { ArrowRight, ArrowUpRight, Menu, X } from "lucide-react";
import { safeDecode } from "./safe";

export type Project = {
  repository_url?: string;
  maintainer?: string;
  contact_email?: string;
  metadata_licence?: string;
};
export const ProjectContext = createContext<Project>({});

// The jurisdictions the corpus covers, by name; any other is shown by its code.
const JURISDICTION_NAMES: Record<string, string> = {
  ng: "Nigeria",
  rw: "Rwanda",
};

export function jurisdictionName(id: string): string {
  return JURISDICTION_NAMES[id] ?? id.toUpperCase();
}

export type Source = {
  id: string;
  jurisdiction_id: string;
  title: string;
  citation: string;
  document_type: string;
  canonical_url: string;
  url_kind?: string; // "document" when the link is the official file itself
  status_evidence?: string | null; // where the legal status was confirmed
  legal_status: string;
  rights_status: string;
  catalogue_note: string | null;
  has_full_text: boolean;
  language?: string; // the language of this text, when the law is printed in several
  summary?: string | null; // what the instrument is, in a sentence
  year?: number | null;
  authority_id?: string;
  authority_name?: string | null; // on a record page: the issuing body's name
  jurisdiction_name?: string | null;
  language_name?: string | null;
};

// The Open Alvary mark: an arrowhead (a kite over two legs), drawn as an outline. Each line sits
// inside the mark's shapes, so the outer edge and gaps stay the same; small marks take the heavier
// line, which would otherwise fade. Shapes from brand/mark.py.
const MARK_LINES = {
  regular: {
    weight: 2.2,
    shapes: [
      "32,6.59 19.59,33.04 32,41.01 44.41,33.04",
      "17.56,37.37 28.81,44.31 8.31,57.1",
      "46.44,37.37 55.69,57.1 35.19,44.31",
    ],
  },
  heavy: {
    weight: 3.2,
    shapes: [
      "32,7.77 20.23,32.85 32,40.42 43.77,32.85",
      "17.78,38.09 27.86,44.31 9.48,55.78",
      "46.22,38.09 54.52,55.78 36.14,44.31",
    ],
  },
};
export function BrandMark({ size = 36 }: { size?: number }) {
  const line = MARK_LINES[size < 36 ? "heavy" : "regular"];
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 64 64"
      aria-hidden="true"
      focusable="false"
    >
      <g fill="none" stroke="currentColor" strokeWidth={line.weight}>
        {line.shapes.map((points) => (
          <polygon key={points} points={points} />
        ))}
      </g>
    </svg>
  );
}

// Real page addresses (/browse, /sources/<id>) on the public site; the review workbench's preview of
// it keeps #/ addresses, so links ask which style the page they sit on uses.
export const LinkStyle = createContext<"path" | "hash">("path");
export function Link({
  to,
  ...props
}: { to: string } & React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  const style = useContext(LinkStyle);
  return <a href={style === "hash" ? "#" + to : to} {...props} />;
}
// Moves the public site to another page without reloading it.
export function navigate(to: string) {
  history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

// Brings the address's #section into view once the page has drawn it: a page may still be loading
// its code or data, so it looks again on each frame for a few seconds. Whether there was a section.
export function scrollToAnchor(): boolean {
  const hash = location.hash;
  const id = safeDecode(hash.slice(1));
  if (!id || id.startsWith("/")) return false;
  let tries = 0;
  const look = () => {
    if (location.hash !== hash) return;
    const target = document.getElementById(id);
    if (target) target.scrollIntoView();
    else if (++tries < 180) requestAnimationFrame(look);
  };
  requestAnimationFrame(look);
  return true;
}

// Plain-language names for the record's fields.
export const DOCUMENT_TYPES: Record<string, string> = {
  constitution: "Constitution",
  act: "Act",
  regulation: "Regulation",
  judgment: "Judgment",
  gazette: "Gazette",
  guidance: "Guidance",
  treaty: "Treaty",
  other: "Other",
};
export const LEGAL_STATUS: Record<string, string> = {
  unknown: "Not yet confirmed",
  current: "In force",
  historical: "Historical",
  repealed: "Repealed",
  superseded: "Replaced by a later law",
};
// A judgment is decided, not in force or repealed, and nobody here follows what later courts did with
// it: its status says the one and its note the other (owner, 2026-11-18).
export const JUDGMENT_NOTE =
  "Published as delivered. Later appeals or overruling are not tracked here.";
export const statusLabel = (s: { document_type: string; legal_status: string }) =>
  s.document_type === "judgment" && s.legal_status === "unknown"
    ? "Decided"
    : (LEGAL_STATUS[s.legal_status] ?? s.legal_status);
export const RIGHTS_STATUS: Record<string, string> = {
  OPEN: "Open to reuse",
  OPEN_WITH_CONDITIONS: "Open to reuse, with conditions",
  LINK_ONLY: "Official link only",
  RESTRICTED: "Restricted",
  RIGHTS_UNDER_REVIEW: "Under review",
};
export function typeLabel(type: string): string {
  return DOCUMENT_TYPES[type] ?? type;
}

// The query's words, marked where they occur in a title.
export function Highlight({ text, query }: { text: string; query?: string }) {
  const words = (query ?? "")
    .split(/\s+/)
    .filter((w) => w.length > 1)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!words.length) return <>{text}</>;
  const parts = text.split(new RegExp(`(${words.join("|")})`, "gi"));
  return (
    <>
      {parts.map((part, i) => (i % 2 ? <mark key={i}>{part}</mark> : part))}
    </>
  );
}

// One of a law's language versions, each its own page.
export type LanguageVersion = { language: string; id: string; title: string };

// Each language by its own name, as a reader of it would look for it.
export const LANGUAGE_NAMES: Record<string, string> = {
  rw: "Ikinyarwanda",
  en: "English",
  fr: "Français",
  sw: "Kiswahili",
  ar: "العربية",
  pt: "Português",
};

export type RightsRecord = {
  status: string;
  licence: string | null;
  legal_note: string;
  reviewer: string | null;
  reviewed_at: string | null;
  attribution_text: string | null;
  conditions: string[];
};

export const links = [
  ["/browse", "Explore the corpus"],
  ["/coverage", "Coverage"],
  ["/changes", "Changes"],
  ["/governance", "Rights & governance"],
  ["/releases", "Releases"],
  ["/developers", "Developers"],
];

export function SiteHeader({
  pathname,
  menu,
  onToggle,
}: {
  pathname: string;
  menu: boolean;
  onToggle: () => void;
}) {
  return (
    <header>
      <Link className="brand" to="/" aria-label="Open Alvary home">
        <BrandMark size={40} />
        <span>
          open<span className="brand-light">alvary</span>
        </span>
      </Link>
      <button
        className="mobile-toggle"
        aria-label="Toggle navigation"
        aria-expanded={menu}
        aria-controls="primary-navigation"
        onClick={onToggle}
      >
        {menu ? <X /> : <Menu />}
      </button>
      <nav
        id="primary-navigation"
        aria-label="Main navigation"
        className={menu ? "expanded" : ""}
      >
        {links.map(([url, label]) => (
          <Link
            key={url}
            to={url}
            // The documentation belongs to Developers.
            aria-current={
              pathname === url ? "page" : url === "/developers" && pathname.startsWith("/docs") ? "true" : undefined
            }
          >
            {label}
          </Link>
        ))}
        {/* On a phone, signing in moves into the menu; the Law check button stays beside it. */}
        <a className="nav-signin" href="/console">
          Sign in
        </a>
      </nav>
      <div className="header-actions">
        <a className="header-signin" href="/console">
          Sign in
        </a>
        <a className="button primary header-check" href="/agents/">
          Law check
        </a>
      </div>
    </header>
  );
}

export function ProjectRepository() {
  const project = useContext(ProjectContext);
  return project.repository_url ? (
    <a href={project.repository_url} target="_blank" rel="noreferrer">
      GitHub <ArrowUpRight size={14} />
    </a>
  ) : (
    <span>Repository publication pending</span>
  );
}

export function SiteFooter() {
  return (
    <footer>
      <Link className="footer-brand" to="/">
        <BrandMark size={28} />
        <span className="footer-brand-name">
          open alvary<span>Public legal infrastructure for Africa.</span>
        </span>
      </Link>
      <p>Public legal data. Clear provenance. Open possibilities.</p>
      <div className="footer-links">
        <Link to="/about">About & contribute</Link>
        <Link to="/connect">Connect AI tools</Link>
        <Link to="/docs">Docs</Link>
        <Link to="/privacy">Privacy</Link>
        <Link to="/terms">Terms</Link>
        <Link to="/security">Security</Link>
        <ProjectRepository />
      </div>
    </footer>
  );
}

export function PageTitle({
  label,
  title,
  lang,
  children,
}: {
  label: string;
  title: string;
  lang?: string; // the title's language, when it is not the page's
  children: React.ReactNode;
}) {
  return (
    <div className="page-title">
      <div className="eyebrow">{label}</div>
      <h1 lang={lang}>{title}</h1>
      <p>{children}</p>
    </div>
  );
}

// One catalogue search result.
// One record in a catalogue list: its year, title, summary and kind, its status when one is known, and
// its official site. The title links to the record; the official site opens the source itself.
export function SourceRow({
  source: s,
  number,
  query,
}: {
  source: Source;
  number: number;
  compact?: boolean;
  query?: string; // words to mark in the title
}) {
  const summary = s.summary && s.summary !== s.title && s.summary !== s.citation ? s.summary : null;
  const status = s.legal_status !== "unknown" ? statusLabel(s) : null;
  return (
    <article className="source-row" data-result={query ? number : undefined}>
      <span className="source-year">{s.year ?? ""}</span>
      <span className="source-info">
        <h3>
          <Link to={"/sources/" + s.id}>
            <Highlight text={s.title} query={query} />
          </Link>
        </h3>
        {summary ? <p>{summary}</p> : null}
        <span className="source-kicker">
          {typeLabel(s.document_type)} <span aria-hidden="true">·</span>{" "}
          {jurisdictionName(s.jurisdiction_id)}
          {s.year ? (
            <span className="source-kicker-year">
              {" "}
              <span aria-hidden="true">·</span> {s.year}
            </span>
          ) : null}
          {s.has_full_text ? (
            <>
              {" "}
              <span aria-hidden="true">·</span> Full text
            </>
          ) : null}
        </span>
      </span>
      <span className="source-side">
        {status ? <span className={"status-chip " + s.legal_status}>{status}</span> : null}
        <a className="source-host" href={s.canonical_url} target="_blank" rel="noreferrer">
          {officialHost(s.canonical_url)} <ArrowUpRight size={14} aria-hidden="true" />
          <span className="visually-hidden"> (official source of {s.title}, opens in a new tab)</span>
        </a>
      </span>
    </article>
  );
}

// The official site's name for a link: the domain of a government site, or "Official document"
// where the file sits on a storage service whose address means nothing to a reader.
export function officialHost(url: string): string {
  let host = "";
  try {
    host = new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "Official source";
  }
  return /amazonaws\.com$|storage\.googleapis\.com$|blob\.core\.windows\.net$/.test(host)
    ? "Official document"
    : host;
}

// The source's record and what readers may do with it, beside its text.
export function SourceRecord({
  source: s,
  rights,
  alone,
  brief,
}: {
  source: Source;
  rights?: RightsRecord;
  alone?: boolean; // no document view beside it, whose status panel carries a judgment's note
  brief?: boolean; // the record page shows status, text and its rights note in their own sections
}) {
  const cleared = s.has_full_text;
  return (
    <>
      <div className="info-panel">
        <h2 className="panel-title">About this record</h2>
        <dl>
          <dt>Type</dt>
          <dd>{typeLabel(s.document_type)}</dd>
          {s.year ? (
            <>
              <dt>Year</dt>
              <dd>{s.year}</dd>
            </>
          ) : null}
          {s.authority_name ? (
            <>
              <dt>Issued by</dt>
              <dd>{s.authority_name}</dd>
            </>
          ) : null}
          <dt>Jurisdiction</dt>
          <dd>{s.jurisdiction_name || jurisdictionName(s.jurisdiction_id)}</dd>
          <dt>Language</dt>
          <dd>
            {s.language_name ||
              (s.language && LANGUAGE_NAMES[s.language]) ||
              s.language}
          </dd>
          <dt>Citation</dt>
          <dd>{s.citation}</dd>
          {brief ? null : (
            <>
              <dt>Status</dt>
              <dd>{statusLabel(s)}</dd>
              <dt>Text on this site</dt>
              <dd>
                {cleared ? "Full text available" : "Not yet: use the official source"}
              </dd>
            </>
          )}
          <dt>Reuse</dt>
          <dd>{RIGHTS_STATUS[s.rights_status] ?? s.rights_status}</dd>
          {rights?.licence ? (
            <>
              <dt>Licence</dt>
              <dd>{rights.licence}</dd>
            </>
          ) : null}
          {rights?.conditions.length ? (
            <>
              <dt>Conditions</dt>
              <dd>{rights.conditions.join("; ")}</dd>
            </>
          ) : null}
          {rights?.attribution_text ? (
            <>
              <dt>Attribution</dt>
              <dd>{rights.attribution_text}</dd>
            </>
          ) : null}
        </dl>
        {s.catalogue_note ? <p className="record-note">{s.catalogue_note}</p> : null}
        {alone && s.document_type === "judgment" && s.legal_status === "unknown" ? (
          <p className="record-note">{JUDGMENT_NOTE}</p>
        ) : null}
      </div>
      {brief ? null : <div className="notice">
        {cleared
          ? "This text may be reused on the terms above. Always cite the official source."
          : "This text has not been cleared for republishing yet. Being published officially does not, by itself, give permission to copy it."}
        <Link to="/governance">
          How we decide <ArrowRight size={15} aria-hidden="true" />
        </Link>
      </div>}
    </>
  );
}
