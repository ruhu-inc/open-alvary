// Pieces of the public site shared by the site itself and the review workbench's preview of it,
// so the preview shows exactly what readers will see.

import React, { createContext, useContext } from "react";
import { ArrowRight, ArrowUpRight, Menu, X } from "lucide-react";

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

// Plain-language names for the record's fields.
export const DOCUMENT_TYPES: Record<string, string> = {
  constitution: "Constitution",
  act: "Act",
  regulation: "Regulation",
  judgment: "Judgment",
  gazette: "Gazette",
  guidance: "Guidance",
  other: "Other",
};
export const LEGAL_STATUS: Record<string, string> = {
  unknown: "Not yet confirmed",
  current: "In force",
  historical: "Historical",
  repealed: "Repealed",
  superseded: "Replaced by a later law",
};
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
            aria-current={pathname === url ? "page" : undefined}
          >
            {label}
          </Link>
        ))}
      </nav>
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
        <Link to="/privacy">Privacy</Link>
        <Link to="/terms">Terms</Link>
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
export function SourceRow({
  source: s,
  number,
  compact = false,
  query,
}: {
  source: Source;
  number: number;
  compact?: boolean;
  query?: string; // words to mark in the title
}) {
  return (
    <Link className="source-row" to={"/sources/" + s.id}>
      <span className="source-number" aria-hidden="true">
        {String(number).padStart(2, "0")}
      </span>
      <span className="source-info">
        <span className="source-kicker">
          {jurisdictionName(s.jurisdiction_id)} <span>·</span>{" "}
          {typeLabel(s.document_type)}
          {s.year ? <> <span>·</span> {s.year}</> : null}
          {s.legal_status !== "unknown" && s.legal_status !== "current" ? (
            <> <span>·</span> {LEGAL_STATUS[s.legal_status] ?? s.legal_status}</>
          ) : null}
        </span>
        <h3>
          <Highlight text={s.title} query={query} />
        </h3>
        {!compact && <p>{s.summary || s.citation}</p>}
      </span>
      <span className={"badge " + (s.has_full_text ? "green" : "")}>
        {s.has_full_text ? "Full text" : "Details and official link"}
      </span>
      <ArrowUpRight size={20} aria-hidden="true" />
    </Link>
  );
}

// The source's record and what readers may do with it, beside its text.
export function SourceRecord({
  source: s,
  rights,
}: {
  source: Source;
  rights?: RightsRecord;
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
          <dt>Status</dt>
          <dd>{LEGAL_STATUS[s.legal_status] ?? s.legal_status}</dd>
          <dt>Text on this site</dt>
          <dd>
            {cleared ? "Full text available" : "Not yet: use the official source"}
          </dd>
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
      </div>
      <div className="notice">
        {cleared
          ? "This text may be reused on the terms above. Always cite the official source."
          : "This text has not been cleared for republishing yet. Being published officially does not, by itself, give permission to copy it."}
        <Link to="/governance">
          How we decide <ArrowRight size={15} aria-hidden="true" />
        </Link>
      </div>
    </>
  );
}
