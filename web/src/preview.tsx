// The review workbench's preview of a document on the public site: the site's own header, search
// result and document page, filled with the latest reviewed text. Served by the workbench to
// signed-in operators only; nothing here is published.

import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ArrowRight, BookOpen, Globe2, Search } from "lucide-react";
import "@fontsource/dm-sans/latin-400.css";
import "@fontsource/dm-sans/latin-500.css";
import "@fontsource/dm-sans/latin-600.css";
import "@fontsource/libre-caslon-display/latin-400.css";
import "./style.css";
import "./preview.css";
import {
  PageTitle,
  SiteFooter,
  SiteHeader,
  SourceRow,
  type Source,
} from "./site";
import { DocumentPage, type DocumentPageContent } from "./document";
import {
  legislationJsonLd,
  type DetailsStatus,
  type Provenance,
  type ReviewRecord,
  type Step,
  type View,
} from "./details";
import type { DownloadLink } from "./tools";

type Preview = {
  source: Source;
  details: DetailsStatus & { details: Record<string, unknown> };
  view: View;
  making: Step[];
  provenance: Provenance;
  review: ReviewRecord;
  pages: DocumentPageContent[];
  content_sha256: string;
  details_sha256: string | null;
  downloads: DownloadLink[];
  status: {
    approved: boolean;
    pages: number;
    checked_pages: number;
    details: {
      confirmed: boolean;
      status: string | null;
      missing: string[] | null;
    };
  };
};

const DETAIL_STATUS: Record<string, string> = {
  suggested: "details suggested from the document, not yet saved",
  draft: "details in draft",
  confirmed: "details confirmed",
};

// This page's address in the preview, optionally at a section: "?section=4".
const linkTo = (query?: string) =>
  `${location.origin}${location.pathname}#/sources/${encodeURIComponent(documentId)}${query ? "?" + query : ""}`;

const documentId = decodeURIComponent(
  location.pathname.replace(/^\/preview\//, "").replace(/\/$/, ""),
);

function matches(source: Source, query: string) {
  const haystack = (source.title + " " + source.citation).toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

function SearchPage({ source, query }: { source: Source; query: string }) {
  const [q, setQ] = useState(query);
  useEffect(() => setQ(query), [query]);
  const found = matches(source, query);
  return (
    <div className="page">
      <PageTitle label="THE PUBLIC CATALOGUE" title="Explore the corpus.">
        Start with an official source. See its rights status, provenance, and
        available versions.
      </PageTitle>
      <form
        className="home-search"
        onSubmit={(e) => {
          e.preventDefault();
          location.hash = "/browse?q=" + encodeURIComponent(q);
        }}
      >
        <Search size={20} />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Search catalogue"
          placeholder="Search by title or citation"
        />
        <button>
          Search <ArrowRight size={16} />
        </button>
      </form>
      <div className="filters">
        <span>
          <Globe2 size={16} /> Nigeria · Pilot jurisdiction
        </span>
      </div>
      {found ? (
        <>
          <div
            role="region"
            aria-label="Catalogue results"
            className="source-list"
          >
            <SourceRow source={source} number={1} />
          </div>
          <div className="catalogue-pagination">
            <p role="status">Showing 1–1 of 1 sources</p>
          </div>
        </>
      ) : (
        <div className="empty">
          <BookOpen size={30} />
          <h3>No sources found</h3>
          <p>
            In this preview, search finds only this document: try words from its
            title.
          </p>
          <a href={"#/browse?q=" + encodeURIComponent(source.title)}>
            Reset search
          </a>
        </div>
      )}
    </div>
  );
}

function PreviewBar({
  preview,
  markUnchecked,
  onMark,
  onSearchRecord,
}: {
  preview: Preview | null;
  markUnchecked: boolean;
  onMark: (on: boolean) => void;
  onSearchRecord: () => void;
}) {
  const status = preview?.status;
  return (
    <div className="preview-bar" role="note">
      <strong>Preview</strong>
      <span className="preview-explain">
        How this document will look on the public site. Not published.
      </span>
      {status ? (
        <span className="preview-status">
          {status.checked_pages} of {status.pages} pages checked ·{" "}
          {DETAIL_STATUS[preview!.details.status] ?? "details"} ·{" "}
          {status.approved ? "approved" : "not approved"}
        </span>
      ) : null}
      {status && status.checked_pages < status.pages ? (
        <label>
          <input
            type="checkbox"
            checked={markUnchecked}
            onChange={(e) => onMark(e.target.checked)}
          />{" "}
          mark pages not yet checked
        </label>
      ) : null}
      {preview ? (
        <button type="button" className="preview-link" onClick={onSearchRecord}>
          Search engine record
        </button>
      ) : null}
      <a href={"/#/doc/" + encodeURIComponent(documentId)}>Back to review</a>
    </div>
  );
}

function Preview() {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [route, setRoute] = useState(location.hash.slice(1));
  const [menu, setMenu] = useState(false);
  const [markUnchecked, setMarkUnchecked] = useState(false);
  const [searchRecord, setSearchRecord] = useState(false);
  useEffect(() => {
    fetch(
      "/api/documents/" + encodeURIComponent(documentId) + "/public-preview",
    )
      .then(async (response) => {
        if (response.status === 401)
          throw new Error(
            "Sign in to the review workbench to see the preview.",
          );
        if (!response.ok) throw new Error("This document could not be found.");
        setPreview(await response.json());
      })
      .catch((error: Error) => setProblem(error.message));
  }, []);
  useEffect(() => {
    const change = () => {
      setRoute(location.hash.slice(1));
      setMenu(false);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  // Start where a reader starts: a search for the document's title.
  useEffect(() => {
    if (!preview || route) return;
    const start = "/browse?q=" + encodeURIComponent(preview.source.title);
    history.replaceState(null, "", "#" + start);
    setRoute(start);
  }, [preview, route]);
  const [pathname, search = ""] = route.split("?");
  const params = new URLSearchParams(search);
  const onDocument = preview && pathname === "/sources/" + preview.source.id;
  const page = params.get("page");
  const section = params.get("section");
  // Opened at a page (from the workbench) or a section (from a copied link): go there.
  useEffect(() => {
    const target = page ? "page-" + page : section ? "section-" + section : "";
    if (onDocument && target)
      document.getElementById(target)?.scrollIntoView({ block: "start" });
  }, [onDocument, page, section]);
  // What the public page will put in its head for search engines and link previews.
  const jsonLd = preview
    ? legislationJsonLd(
        preview.view,
        preview.making,
        preview.source,
        linkTo(),
        preview.downloads,
      )
    : null;
  useEffect(() => {
    if (!preview) return;
    let meta = document.querySelector<HTMLMetaElement>(
      'meta[name="description"]',
    );
    if (!meta) {
      meta = document.createElement("meta");
      meta.name = "description";
      document.head.append(meta);
    }
    meta.content =
      String(preview.view.values["text.summary.text"] ?? "") ||
      preview.source.catalogue_note;
  }, [preview]);
  useEffect(() => {
    if (preview)
      document.title = `Preview: ${preview.source.title} · Open Alvary`;
  }, [preview]);
  let body: React.ReactNode;
  if (problem)
    body = (
      <div className="page">
        <h1>Preview unavailable</h1>
        <p>{problem}</p>
        <a href="/">Open the review workbench</a>
      </div>
    );
  else if (!preview) body = <p className="page">Loading…</p>;
  else if (onDocument)
    body = (
      <DocumentPage
        source={preview.source}
        pages={preview.pages}
        contentHash={preview.content_sha256}
        detailsHash={preview.details_sha256}
        view={preview.view}
        making={preview.making}
        details={preview.details}
        provenance={preview.provenance}
        review={preview.review}
        downloads={preview.downloads}
        approved={preview.status.approved}
        linkTo={linkTo}
        markUnchecked={markUnchecked}
      />
    );
  else if (pathname === "/browse" || pathname === "/" || !pathname)
    body = <SearchPage source={preview.source} query={params.get("q") ?? ""} />;
  else
    body = (
      <div className="page">
        <h1>Not part of the preview</h1>
        <p>
          The preview shows this document&apos;s search result and its page; the
          rest of the public site is not included.
        </p>
        <a href={"#/browse?q=" + encodeURIComponent(preview.source.title)}>
          Back to the search result
        </a>
      </div>
    );
  return (
    <>
      <PreviewBar
        preview={preview}
        markUnchecked={markUnchecked}
        onMark={setMarkUnchecked}
        onSearchRecord={() => setSearchRecord(!searchRecord)}
      />
      {searchRecord && jsonLd ? (
        <section className="preview-record" aria-label="Search engine record">
          <p>
            What the public page will carry for search engines (schema.org
            Legislation) and link previews. Readers do not see it.
          </p>
          <pre>{JSON.stringify(jsonLd, null, 2)}</pre>
        </section>
      ) : null}
      <SiteHeader
        pathname={pathname || "/browse"}
        menu={menu}
        onToggle={() => setMenu(!menu)}
      />
      <main id="main-content" tabIndex={-1}>
        {body}
      </main>
      <SiteFooter />
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Preview />
  </React.StrictMode>,
);
