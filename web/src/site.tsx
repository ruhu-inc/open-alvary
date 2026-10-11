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

export type Source = {
  id: string;
  jurisdiction_id: string;
  title: string;
  citation: string;
  document_type: string;
  canonical_url: string;
  legal_status: string;
  rights_status: string;
  catalogue_note: string;
  has_full_text: boolean;
  language?: string; // the language of this text, when the law is printed in several
};

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
      <a className="brand" href="#/" aria-label="Open Alvary home">
        <span className="brand-icon">
          a<span>↗</span>
        </span>
        <span>
          open<span className="brand-light">alvary</span>
          <small>PUBLIC LEGAL INFRASTRUCTURE</small>
        </span>
      </a>
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
          <a
            key={url}
            href={"#" + url}
            aria-current={pathname === url ? "page" : undefined}
          >
            {label}
          </a>
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
      <a className="footer-brand" href="#/">
        open alvary<span>A shared foundation for law.</span>
      </a>
      <p>Public legal data. Clear provenance. Open possibilities.</p>
      <div className="footer-links">
        <a href="#/about">About & contribute</a>
        <a href="#/privacy">Privacy</a>
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
}: {
  source: Source;
  number: number;
  compact?: boolean;
}) {
  return (
    <a className="source-row" href={"#/sources/" + s.id}>
      <span className="source-number">{String(number).padStart(2, "0")}</span>
      <span className="source-info">
        <span className="source-kicker">
          {s.jurisdiction_id.toUpperCase()} <span>·</span>{" "}
          {s.document_type.toUpperCase()}
          {s.legal_status === "repealed" ? " · HISTORICAL" : ""}
        </span>
        <h3>{s.title}</h3>
        {!compact && (
          <p>
            {s.citation} · {s.catalogue_note}
          </p>
        )}
      </span>
      <span className={"badge " + (s.has_full_text ? "green" : "")}>
        {s.has_full_text ? "Open full text" : "Metadata only"}
      </span>
      <ArrowUpRight size={20} />
    </a>
  );
}

// The source's record and rights note, beside its text.
export function SourceRecord({
  source: s,
  rights,
}: {
  source: Source;
  rights?: RightsRecord;
}) {
  return (
    <>
      <div className="info-panel">
        <h3>Source record</h3>
        <dl>
          <dt>Jurisdiction</dt>
          <dd>
            {s.jurisdiction_id === "ng"
              ? "Nigeria"
              : s.jurisdiction_id.toUpperCase()}
          </dd>
          <dt>Access</dt>
          <dd>
            {s.has_full_text ? "Open full text" : "Metadata / official link"}
          </dd>
          <dt>Legal status</dt>
          <dd>
            {s.legal_status === "unknown"
              ? "Currentness unverified"
              : s.legal_status}
          </dd>
          <dt>Rights status</dt>
          <dd>{s.rights_status.replaceAll("_", " ").toLowerCase()}</dd>
          <dt>Licence</dt>
          <dd>{rights?.licence || "Not verified"}</dd>
          <dt>Attribution</dt>
          <dd>{rights?.attribution_text || "Not yet determined"}</dd>
          {rights?.conditions.length ? (
            <>
              <dt>Reuse conditions</dt>
              <dd>{rights.conditions.join("; ")}</dd>
            </>
          ) : null}
          <dt>Rights reviewer</dt>
          <dd>{rights?.reviewer || "Not yet assigned"}</dd>
        </dl>
      </div>
      <div className="notice">
        {rights?.legal_note ||
          "No redistribution permission has been verified."}
        <a href="#/governance">
          Read the rights policy <ArrowRight size={15} />
        </a>
      </div>
    </>
  );
}
