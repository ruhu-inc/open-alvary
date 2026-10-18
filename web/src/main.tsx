import React, { useContext, useEffect, useRef, useState } from "react";
import { ChangesPage, RecordChanges } from "./changes";
import { CurrentLaw, type History, RecordHistory } from "./history";
import { createRoot } from "react-dom/client";
import {
  ArrowUpRight,
  ArrowRight,
  Search,
  BookOpen,
  ShieldCheck,
  Layers,
  Code2,
  Download,
  Globe2,
  ChevronRight,
  ExternalLink,
  FileText,
} from "lucide-react";
import "@fontsource/dm-sans/latin-400.css";
import "@fontsource/dm-sans/latin-500.css";
import "@fontsource/dm-sans/latin-600.css";
import "@fontsource/libre-caslon-display/latin-400.css";
import "./style.css";
import { AcceptableUse, Privacy, Terms } from "./legal";
import { Security } from "./security";
import {
  PageTitle,
  ProjectContext,
  ProjectRepository,
  SiteFooter,
  SiteHeader,
  SourceRecord,
  SourceRow,
  jurisdictionName,
  Link,
  navigate,
  typeLabel,
  LANGUAGE_NAMES,
  LEGAL_STATUS,
  links,
  type Project,
  type RightsRecord,
  type Source,
} from "./site";

// The documentation loads only when a /docs page is opened.
const DocsSite = React.lazy(() => import("./docs/DocsSite"));

type Coverage = {
  catalogued_sources: number;
  open_full_text_sources: number;
  metadata_only_sources: number;
  published_versions: number;
  jurisdictions: Record<string, number>; // sources per jurisdiction
};
type Manifest = {
  release_id: string;
  jurisdiction: string;
  mode: string;
  download_base: string;
  files: Record<string, { sha256: string; records?: number }>;
  coverage: Coverage;
};
function useData<T>(path: string) {
  const [state, set] = useState<{ path: string; data?: T; error?: string }>({
    path: "",
  });
  useEffect(() => {
    const controller = new AbortController();
    set({ path });
    // The website's own data path (MAC-01): anonymous, bounded, never the developer API.
    fetch("/site/v1" + path, { signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`Request failed (${r.status})`);
        return r.json();
      })
      .then((data) => {
        if (!controller.signal.aborted) set({ path, data });
      })
      .catch((e) => {
        if (!controller.signal.aborted && e.name !== "AbortError")
          set({ path, error: e.message });
      });
    return () => controller.abort();
  }, [path]);
  return state.path === path ? state : { data: undefined, error: undefined };
}
function Feedback({ error }: { error?: string }) {
  return (
    <div className="notice" role={error ? "alert" : "status"}>
      {error
        ? `Unable to load the corpus. ${error}. Please reload or try again shortly.`
        : "Loading the catalogue…"}
    </div>
  );
}
// Resolves to nothing in production: Vite replaces import.meta.env.DEV with false.
const DevReader = import.meta.env.DEV
  ? React.lazy(() => import("./dev/DevReader"))
  : () => null;

function decodeSegment(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

class PageBoundary extends React.Component<
  { children: React.ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div className="page" role="alert">
        <h1>Something went wrong</h1>
        <p>This page could not be displayed.</p>
        <Link to="/">Return home</Link>
      </div>
    ) : (
      this.props.children
    );
  }
}

function NotFound() {
  useEffect(() => {
    document.title = "Page not found · Open Alvary";
  }, []);
  return (
    <div className="page not-found">
      <h1>This page doesn’t exist.</h1>
      <p>The address may be mistyped, or the page may have moved.</p>
      <p>
        <Link className="button primary" to="/browse">
          Search the catalogue <ArrowRight size={17} aria-hidden="true" />
        </Link>{" "}
        <Link className="text-link" to="/">
          Go to the home page
        </Link>
      </p>
    </div>
  );
}

// The page's address and query, as the site routes them.
function currentRoute() {
  return location.pathname + location.search;
}
function App() {
  const project = useData<Project>("/project");
  const [route, setRoute] = useState(() => {
    // Old #/ addresses keep working: /#/sources/x becomes /sources/x.
    if (location.hash.startsWith("#/"))
      history.replaceState(null, "", location.hash.slice(1));
    return currentRoute();
  });
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    const change = () => {
      setRoute(currentRoute());
      setMenu(false);
      window.scrollTo(0, 0);
    };
    // Links to the site's own pages move without reloading it; files, data and other sites load.
    const click = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
        return;
      const a = (e.target as HTMLElement).closest("a");
      if (!a || a.target || a.hasAttribute("download") || !a.href) return;
      const url = new URL(a.href, location.href);
      if (
        url.origin !== location.origin ||
        /^\/(site|api|downloads|assets|console)(\/|$)/.test(url.pathname) ||
        /\.[a-z0-9]+$/i.test(url.pathname) ||
        (url.pathname === location.pathname && url.search === location.search && url.hash)
      )
        return;
      e.preventDefault();
      navigate(url.pathname + url.search);
    };
    window.addEventListener("popstate", change);
    document.addEventListener("click", click);
    return () => {
      window.removeEventListener("popstate", change);
      document.removeEventListener("click", click);
    };
  }, []);
  const pathname = route.split("?")[0];
  const sourceId = pathname.startsWith("/sources/")
    ? decodeSegment(pathname.slice(9))
    : null;
  useEffect(() => {
    const title = [
      ...links,
      ["/about", "About & contribute"],
      ["/privacy", "Privacy"],
      ["/terms", "Terms of service"],
      ["/acceptable-use", "Acceptable use"],
      ["/security", "Security"],
    ].find(([url]) => url === pathname)?.[1];
    // Record pages and the not-found page name themselves.
    if (title) document.title = `${title} · Open Alvary`;
    else if (pathname === "/")
      document.title = "Open Alvary · A shared foundation for African law";
    document
      .querySelector('link[rel="canonical"]')
      ?.setAttribute("href", location.origin + pathname);
    document.querySelector<HTMLElement>("main")?.focus({ preventScroll: true });
    // A page opened at an anchor (/docs/errors#gone) renders after the browser looked for it.
    const anchor = decodeURIComponent(location.hash.slice(1));
    if (anchor) requestAnimationFrame(() => document.getElementById(anchor)?.scrollIntoView());
  }, [pathname]);
  return (
    <ProjectContext.Provider value={project.data ?? {}}>
      <a
        className="skip-link"
        href="#main-content"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to main content
      </a>
      <SiteHeader
        pathname={pathname}
        menu={menu}
        onToggle={() => setMenu(!menu)}
      />
      <main id="main-content" tabIndex={-1}>
        <PageBoundary key={pathname}>
          {pathname === "/" ? (
            <Home />
          ) : pathname === "/browse" ? (
            <Browse route={route} />
          ) : sourceId !== null ? (
            <Reader id={sourceId} />
          ) : pathname === "/coverage" ? (
            <CoveragePage />
          ) : pathname === "/changes" ? (
            <ChangesPage route={route} />
          ) : pathname === "/governance" ? (
            <Governance />
          ) : pathname === "/releases" ? (
            <Releases />
          ) : pathname === "/about" ? (
            <About />
          ) : pathname === "/privacy" ? (
            <Privacy />
          ) : pathname === "/terms" ? (
            <Terms />
          ) : pathname === "/acceptable-use" ? (
            <AcceptableUse />
          ) : pathname === "/security" ? (
            <Security />
          ) : pathname === "/docs" || pathname.startsWith("/docs/") ? (
            <React.Suspense fallback={<p className="page">Loading the documentation…</p>}>
              <DocsSite path={pathname} />
            </React.Suspense>
          ) : pathname === "/developers" ? (
            <Developers />
          ) : import.meta.env.DEV && pathname === "/dev/reader" ? (
            <React.Suspense fallback={<p>Loading…</p>}>
              <DevReader />
            </React.Suspense>
          ) : (
            <NotFound />
          )}
        </PageBoundary>
      </main>
      <SiteFooter />
    </ProjectContext.Provider>
  );
}
function Home() {
  const { data, error } = useData<Coverage>("/coverage");
  const [q, setQ] = useState("");
  return (
    <>
      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow">
            <span className="dot" /> OPEN KNOWLEDGE. SHARED INFRASTRUCTURE.
          </div>
          <h1>
            A shared foundation
            <br />
            for <em>African law.</em>
          </h1>
          <p className="lead">
            Public legal information, structured for people and machines. Built
            with clear rights, traceable sources, and access that belongs to
            everyone.
          </p>
          <div className="hero-actions">
            <Link className="button primary" to="/browse">
              Explore the corpus <ArrowRight size={17} aria-hidden="true" />
            </Link>
            <Link className="text-link" to="/developers">
              Build with the data <ArrowUpRight size={17} aria-hidden="true" />
            </Link>
          </div>
          <div className="pilot">
            <span>v0.1 · Catalogue preview</span>
            <span className="pilot-divider" />
            <span>Jurisdictions are added as their sources are verified</span>
          </div>
        </div>
        <div
          className="hero-art"
          role="img"
          aria-label="A connected legal corpus with source, structure, provenance and access layers"
        >
          <div className="art-grid" />
          <div className="art-top">
            <span>THE OPEN CORPUS</span>
            <span>v0.1</span>
          </div>
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="data-card layer-back">
            <div>
              <Globe2 size={18} /> Authoritative sources
            </div>
          </div>
          <div className="data-card layer-middle">
            <div>
              <ShieldCheck size={18} /> Rights & provenance
            </div>
          </div>
          <div className="data-card layer-front">
            <span className="tiny-label">STRUCTURED LEGAL DATA</span>
            <div className="code-row">
              <span>01</span>
              <b>source</b>
              <i>"official"</i>
            </div>
            <div className="code-row">
              <span>02</span>
              <b>versions</b>
              <i>[ … ]</i>
            </div>
            <div className="code-row">
              <span>03</span>
              <b>structure</b>
              <i>{"{ … }"}</i>
            </div>
            <div className="code-row">
              <span>04</span>
              <b>provenance</b>
              <i>traceable</i>
            </div>
            <div className="card-bottom">
              <span className="dot" /> MACHINE-READABLE BY DESIGN{" "}
              <Code2 size={16} />
            </div>
          </div>
          <div className="art-bottom">
            <span className="dot" /> ONE PUBLIC LAYER. MANY POSSIBILITIES.
          </div>
        </div>
      </section>
      <section className="stats-strip">
        <div>
          <strong>
            {data ? Object.keys(data.jurisdictions).length : "—"}
            <span>
              {data && Object.keys(data.jurisdictions).length === 1
                ? "jurisdiction"
                : "jurisdictions"}
            </span>
          </strong>
          <small>Added as their sources are verified</small>
        </div>
        <div>
          <strong>
            {data?.catalogued_sources ?? "—"}
            <span>sources catalogued</span>
          </strong>
          <small>Official-source references</small>
        </div>
        <div>
          <strong>
            {data?.open_full_text_sources ?? "—"}
            <span>cleared for full text</span>
          </strong>
          <small>Publication follows verification</small>
        </div>
        <div className="stat-note">
          <ShieldCheck size={22} />
          <p>
            Publicly accessible doesn’t
            <br />
            automatically mean redistributable.
          </p>
        </div>
      </section>
      {error && <Feedback error={error} />}
      <section className="section">
        <div className="section-heading">
          <div>
            <div className="eyebrow">EXPLORE THE COLLECTION</div>
            <h2>Start with the source.</h2>
          </div>
          <Link className="text-link" to="/browse">
            View the catalogue <ArrowRight size={17} aria-hidden="true" />
          </Link>
        </div>
        <form
          className="home-search"
          onSubmit={(e) => {
            e.preventDefault();
            navigate("/browse" + (q.trim() ? "?q=" + encodeURIComponent(q.trim()) : ""));
          }}
        >
          <Search size={20} aria-hidden="true" />
          <input
            aria-label="Search legal sources"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by title, citation, or keyword"
          />
          <button type="submit">
            Search <ArrowRight size={16} />
          </button>
        </form>
        <SourceList compact />
        <div className="quiet-note">
          <span className="dot amber" /> Today the catalogue holds details and
          official links. Full texts are added once their rights and content
          have been reviewed.
        </div>
      </section>
      <section className="principles">
        <div>
          <div className="eyebrow">OPEN BY PRINCIPLE. CAREFUL BY DESIGN.</div>
          <h2>
            Useful data starts
            <br />
            with trust.
          </h2>
          <Link className="text-link" to="/governance">
            Our approach to governance <ArrowUpRight size={17} aria-hidden="true" />
          </Link>
        </div>
        <div className="principle">
          <ShieldCheck />
          <h3>Rights before release</h3>
          <p>
            Every source passes a rights review. Uncertain permissions never
            become assumed permissions.
          </p>
        </div>
        <div className="principle">
          <Layers />
          <h3>Every version, traceable</h3>
          <p>
            Source links, content hashes, and structural anchors keep the data
            connected to its origin.
          </p>
        </div>
        <div className="principle">
          <Code2 />
          <h3>Made to build on</h3>
          <p>
            Versioned releases anyone can download, with checksums. Useful
            for research and software, with or without AI.
          </p>
        </div>
      </section>
      <section className="open-banner">
        <div>
          <div className="eyebrow">A PUBLIC LAYER, A SHARED OPPORTUNITY</div>
          <h2>
            For researchers. For developers.
            <br />
            For everyone working with law.
          </h2>
        </div>
        <Link className="button light" to="/developers">
          For developers <ArrowUpRight size={17} />
        </Link>
      </section>
    </>
  );
}
const PAGE_SIZE = 20;
type Facet = { value: string; count: number; label?: string | null };
type BrowseResult = {
  items: Source[];
  total: number;
  page: number;
  last_page: number;
  sort: string;
  facets: Record<string, Facet[]>;
};
// The catalogue's filters, in the order a reader narrows a search.
const FILTERS: [string, string][] = [
  ["jurisdiction", "Jurisdiction"],
  ["type", "Type"],
  ["year", "Year"],
  ["language", "Language"],
  ["status", "Status"],
  ["access", "Text"],
];
const ACCESS_LABELS: Record<string, string> = {
  open: "Full text on this site",
  metadata: "Details and official link",
};
function facetLabel(name: string, f: Facet): string {
  if (name === "jurisdiction") return f.label || jurisdictionName(f.value);
  if (name === "type") return typeLabel(f.value);
  if (name === "language") return f.label || LANGUAGE_NAMES[f.value] || f.value;
  if (name === "status") return LEGAL_STATUS[f.value] ?? f.value;
  if (name === "access") return ACCESS_LABELS[f.value] ?? f.value;
  return f.value;
}
function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}
// The catalogue for a route's query: results, filters with counts, sorting and pages. Compact, on the
// home page, it shows the first few sources and a link to the rest.
function SourceList({
  compact = false,
  params = new URLSearchParams(),
  onChange,
}: {
  compact?: boolean;
  params?: URLSearchParams;
  onChange?: (changes: Record<string, string | null>) => void;
}) {
  const q = params.get("q") || "";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const query = new URLSearchParams();
  if (q) query.set("q", q);
  for (const [name] of FILTERS) {
    const value = params.get(name);
    if (value) query.set(name, value);
  }
  if (params.get("sort")) query.set("sort", params.get("sort")!);
  if (page > 1) query.set("page", String(page));
  const { data, error } = useData<BrowseResult>(
    (q ? "/search?" : "/sources?") + query,
  );
  const resultsRef = useRef<HTMLDivElement>(null);
  if (!data) return <Feedback error={error} />;
  const first = (page - 1) * PAGE_SIZE;
  const items = compact ? data.items.slice(0, 6) : data.items;
  const capped = data.total > data.last_page * PAGE_SIZE;
  const selected = FILTERS.some(([name]) => params.get(name));
  const move = (next: number) => {
    onChange?.({ page: next > 1 ? String(next) : null });
    resultsRef.current?.focus();
  };
  return (
    <>
      {!compact && onChange && (
        <div className="filters" role="group" aria-label="Filter and sort the catalogue">
          {FILTERS.map(([name, label]) => {
            const values = data.facets[name] ?? [];
            const value = params.get(name) || "";
            if (values.length < 2 && !value) return null;
            return (
              <label key={name}>
                {label}
                <select
                  value={value}
                  onChange={(e) => onChange({ [name]: e.target.value || null })}
                >
                  <option value="">All</option>
                  {values.map((f) => (
                    <option key={f.value} value={f.value}>
                      {facetLabel(name, f)} ({f.count})
                    </option>
                  ))}
                </select>
              </label>
            );
          })}
          <label>
            Sort
            <select
              value={data.sort}
              onChange={(e) => onChange({ sort: e.target.value })}
            >
              {q && <option value="relevance">Best match</option>}
              <option value="title">Title A–Z</option>
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
            </select>
          </label>
          {selected && (
            <button
              type="button"
              className="clear-filters"
              onClick={() =>
                onChange(Object.fromEntries(FILTERS.map(([name]) => [name, null])))
              }
            >
              Clear filters
            </button>
          )}
        </div>
      )}
      {!compact && (
        <p className="result-count" role="status">
          {plural(data.total, "source", "sources")}
          {q ? ` for “${q}”` : ""}
          {data.last_page > 1
            ? ` · showing ${first + 1}–${first + items.length}`
            : ""}
        </p>
      )}
      <div
        ref={resultsRef}
        tabIndex={-1}
        role="region"
        aria-label="Catalogue results"
        className={"source-list " + (compact ? "compact" : "")}
      >
        {items.map((s, i) => (
          <SourceRow
            key={s.id}
            source={s}
            number={first + i + 1}
            compact={compact}
            query={q}
          />
        ))}
      </div>
      {!items.length && (
        <div className="empty">
          <BookOpen size={30} aria-hidden="true" />
          <h3>No sources found</h3>
          <p>Try other words, or clear the filters.</p>
          <Link to="/browse">Start again</Link>
        </div>
      )}
      {compact && data.total > items.length && (
        <div className="catalogue-pagination">
          <p>{plural(data.total, "source", "sources")} in the catalogue</p>
          <Link to="/browse">
            View all <ArrowRight size={16} aria-hidden="true" />
          </Link>
        </div>
      )}
      {!compact && data.last_page > 1 && (
        <nav className="catalogue-pagination" aria-label="Results pages">
          <p>
            Page {page} of {data.last_page}
            {capped && page === data.last_page
              ? ` · only the first ${data.last_page * PAGE_SIZE} results are shown; refine the search to narrow them`
              : ""}
          </p>
          <div>
            <button disabled={page === 1} onClick={() => move(page - 1)}>
              Previous
            </button>
            <button disabled={page >= data.last_page} onClick={() => move(page + 1)}>
              Next
            </button>
          </div>
        </nav>
      )}
    </>
  );
}
function Browse({ route }: { route: string }) {
  const params = new URLSearchParams(route.split("?")[1] || "");
  const query = params.get("q") || "";
  const [q, setQ] = useState(query);
  useEffect(() => setQ(query), [query]);
  // Every choice lives in the address, so a search can be bookmarked and shared.
  const change = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [name, value] of Object.entries(changes))
      value ? next.set(name, value) : next.delete(name);
    if (!("page" in changes)) next.delete("page");
    const s = next.toString();
    navigate("/browse" + (s ? "?" + s : ""));
  };
  return (
    <div className="page">
      <PageTitle label="THE PUBLIC CATALOGUE" title="Explore the corpus.">
        Search by title, citation or subject. Each source shows its rights
        status, where it comes from and what you may do with it.
      </PageTitle>
      <form
        className="home-search"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          change({ q: q.trim() || null, sort: null });
        }}
      >
        <Search size={20} aria-hidden="true" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Search the catalogue"
          placeholder="Search by title, citation or subject"
        />
        <button>
          Search <ArrowRight size={16} aria-hidden="true" />
        </button>
      </form>
      <SourceList params={params} onChange={change} />
    </div>
  );
}
// The record's own title and description in the browser and in search results.
function RecordTitle({ source: s }: { source: Source }) {
  useEffect(() => {
    document.title = `${s.title} · Open Alvary`;
    document
      .querySelector('meta[name="description"]')
      ?.setAttribute("content", s.summary || `${typeLabel(s.document_type)}: ${s.citation}`);
  }, [s]);
  return null;
}
function Reader({ id }: { id: string }) {
  const source = useData<Source>("/sources/" + encodeURIComponent(id));
  const versions = useData<{
    items: {
      id: string;
      canonical_text: string;
      content_hash: string;
      retrieved_at: string;
    }[];
    notice: string;
  }>("/sources/" + encodeURIComponent(id) + "/versions");
  const rights = useData<{
    record: RightsRecord;
    decision: { reasons: string[] };
  }>("/rights/" + encodeURIComponent(id));
  const history = useData<History>("/sources/" + encodeURIComponent(id) + "/relationships");
  if (!source.data)
    return (
      <div className="page">
        <Feedback error={source.error} />
      </div>
    );
  const s = source.data;
  return (
    <div className="page">
      <RecordTitle source={s} />
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link to="/browse">Catalogue</Link>
        <ChevronRight size={14} aria-hidden="true" />
        <Link to={"/browse?jurisdiction=" + encodeURIComponent(s.jurisdiction_id)}>
          {s.jurisdiction_name || jurisdictionName(s.jurisdiction_id)}
        </Link>
      </nav>
      <PageTitle label={typeLabel(s.document_type) + " · " + s.citation} title={s.title}>
        {s.summary || s.citation}
      </PageTitle>
      <CurrentLaw history={history.data} />
      <div className="reader-grid">
        <article className="reader">
          <div className="reader-bar">
            <FileText size={18} /> Source text{" "}
            <span className="badge">
              {s.has_full_text ? "Open full text" : "Metadata only"}
            </span>
          </div>
          {versions.data ? (
            versions.data.items.length ? (
              versions.data.items.map((v) => (
                <section key={v.id}>
                  <h3>{v.id}</h3>
                  <pre className="legal-text">{v.canonical_text}</pre>
                  <p className="hash">SHA-256 {v.content_hash}</p>
                </section>
              ))
            ) : (
              <div className="empty">
                <ShieldCheck size={36} aria-hidden="true" />
                <h2>The text isn’t on this site yet.</h2>
                <p>
                  It will be added once its rights and content have been
                  reviewed. Until then, read it at the official source.
                </p>
                <a
                  className="button primary"
                  href={s.canonical_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Read the official source <ExternalLink size={16} aria-hidden="true" />
                </a>
              </div>
            )
          ) : (
            <Feedback error={versions.error} />
          )}
        </article>
        <aside>
          <SourceRecord source={s} rights={rights.data?.record} />
          <RecordHistory history={history.data} />
          <RecordChanges id={s.id} />
        </aside>
      </div>
    </div>
  );
}
// What each jurisdiction's coverage does and does not include.
const JURISDICTION_NOTES: Record<string, string[]> = {
  ng: [
    "Five federal instruments, with details and official links. Not yet covered: state law, judgments, regulations, translations and later amendments.",
    "The Investments and Securities Act 2007 is kept for historical reference: the Investments and Securities Act 2025 repealed it.",
  ],
};
function CoveragePage() {
  const { data, error } = useData<Coverage>("/coverage");
  return (
    <div className="page">
      <PageTitle
        label="COVERAGE & LIMITATIONS"
        title="An honest view of the corpus."
      >
        We publish what we have verified and make the gaps visible.
        Jurisdictions appear here as their sources are verified.
      </PageTitle>
      {data ? (
        <>
          <div className="metric-grid">
            {[
              [data.catalogued_sources, "Catalogued sources"],
              [data.open_full_text_sources, "Cleared full-text sources"],
              [data.metadata_only_sources, "Metadata-only sources"],
              [data.published_versions, "Published versions"],
            ].map(([n, label]) => (
              <div className="metric" key={label}>
                <strong>{n}</strong>
                <span>{label}</span>
              </div>
            ))}
          </div>
          {Object.entries(data.jurisdictions).map(([jurisdiction, count]) => (
            <div className="info-panel" key={jurisdiction}>
              <h2>
                {jurisdictionName(jurisdiction)}{" "}
                <span className="badge">{plural(count, "source", "sources")}</span>
              </h2>
              {(JURISDICTION_NOTES[jurisdiction] ?? []).map((note) => (
                <p key={note}>{note}</p>
              ))}
              <Link
                className="text-link"
                to={"/browse?jurisdiction=" + encodeURIComponent(jurisdiction)}
              >
                Explore its sources <ArrowRight size={16} aria-hidden="true" />
              </Link>
            </div>
          ))}
        </>
      ) : (
        <Feedback error={error} />
      )}
    </div>
  );
}
function Governance() {
  return (
    <div className="page">
      <PageTitle
        label="RIGHTS & GOVERNANCE"
        title="Open where rights are clear."
      >
        Official publication is a source of provenance. It is not, by itself,
        permission to redistribute.
      </PageTitle>
      <h2 className="flow-heading">How a source becomes public</h2>
      <div className="governance-flow">
        <div className="flow-top">
          <div className="flow-box good">
            <ShieldCheck />
            <h3>Authoritative public sources</h3>
            <p>
              Government, courts, regulators, gazettes.
              <br />
              Each source receives its own rights review.
            </p>
          </div>
          <div className="flow-box excluded">
            <h3>Restricted or uncertain material</h3>
            <p>
              Full text stays outside public access.
              <br />
              Restricted/licensed content is excluded.
            </p>
          </div>
        </div>
        <div className="flow-box gate">
          <h3>Rights & provenance gate</h3>
          <p>
            Official URL · exact content hash · licence evidence · reviewer ·
            privacy · independent content verification
          </p>
        </div>
        <div className="flow-box good corpus-box">
          <h3>The open legal corpus</h3>
          <p>
            Cleared text + structured metadata + provenance + version history
          </p>
        </div>
        <div className="flow-bottom">
          <div className="flow-box">
            <h3>Public & research</h3>
            <p>Researchers, legal-aid groups, universities, developers.</p>
          </div>
          <div className="flow-box">
            <h3>Data for software</h3>
            <p>Versioned releases with checksums, free to download.</p>
          </div>
        </div>
      </div>
      <div className="two-columns">
        <section>
          <h2>Three access modes</h2>
          <p>
            <b>Open full text.</b> Exact versions approved for redistribution,
            commercial reuse, derivatives, and applicable attribution
            conditions.
          </p>
          <p>
            <b>Metadata / link only.</b> Independently authored catalogue facts
            and official links. No full text or excerpts from uncleared
            material.
          </p>
          <p>
            <b>Restricted / licensed.</b> Excluded from this public repository
            and its data services. Any lawful commercial access is managed
            separately.
          </p>
        </section>
        <section>
          <h2>Accountability before scale</h2>
          <p>
            Rights review and content verification are separate decisions.
            Privacy, database rights, translation provenance, and licence
            conditions all need a recorded outcome.
          </p>
          <p>
            Corrections create new versions. A reported rights issue should
            suspend publication while a maintainer investigates. Public launch
            requires named reviewers and a working takedown channel.
          </p>
          <p>
            Open corpus data does not imply unlimited free hosted compute.
            Hosted service tiers may be introduced separately.
          </p>
        </section>
      </div>
    </div>
  );
}
const MODES: Record<string, [string, string]> = {
  metadata: ["Metadata", "Records, rights and official links, without legal texts."],
  corpus: ["Corpus", "Records plus every legal text cleared for reuse."],
};
function releaseDate(id: string) {
  const day = /^\d{4}-\d{2}-\d{2}/.exec(id)?.[0];
  return day
    ? new Date(day + "T00:00:00Z").toLocaleDateString("en-GB", {
        day: "numeric",
        month: "long",
        year: "numeric",
        timeZone: "UTC",
      })
    : id;
}
function ReleaseFiles({ release: r }: { release: Manifest }) {
  const files = Object.entries(r.files);
  const empty = files.filter(([, info]) => info.records === 0);
  const shown = files.filter(([, info]) => info.records !== 0);
  const file = ([name, info]: [string, { sha256: string; records?: number }]) => (
    <a key={name} href={r.download_base + "/" + name}>
      <FileText size={16} aria-hidden="true" />
      <span>
        {name}
        <small>
          {info.records !== undefined
            ? plural(info.records, "record", "records")
            : "Release documentation"}
        </small>
      </span>
      <Download size={15} aria-hidden="true" />
    </a>
  );
  return (
    <>
      <div className="download-grid">{shown.map(file)}</div>
      {empty.length > 0 && (
        <details className="empty-files">
          <summary>{plural(empty.length, "empty file", "empty files")}</summary>
          <div className="download-grid">{empty.map(file)}</div>
        </details>
      )}
    </>
  );
}
function Releases() {
  const { data, error } = useData<Manifest[]>("/releases");
  const groups = new Map<string, Manifest[]>();
  for (const r of data ?? []) {
    const key = r.release_id + "|" + r.jurisdiction;
    groups.set(key, [...(groups.get(key) ?? []), r]);
  }
  return (
    <div className="page">
      <PageTitle label="VERSIONED & VERIFIABLE" title="Take the data with you.">
        Download JSONL data and a SHA-256 manifest. Each release records its
        scope and rights status; no blanket licence is claimed over linked legal
        text.
      </PageTitle>
      {!data ? (
        <Feedback error={error} />
      ) : !data.length ? (
        <p>No releases published yet.</p>
      ) : (
        [...groups.values()].map((modes) => (
          <section className="release-card" key={modes[0].release_id + modes[0].jurisdiction}>
            <div className="release-heading">
              <div>
                <div className="eyebrow">{jurisdictionName(modes[0].jurisdiction)}</div>
                <h2>{releaseDate(modes[0].release_id)}</h2>
                <p>
                  {plural(modes[0].coverage.catalogued_sources, "catalogue entry", "catalogue entries")} ·{" "}
                  {plural(modes[0].coverage.open_full_text_sources, "text", "texts")} cleared for reuse
                </p>
              </div>
            </div>
            {modes.map((r) => (
              <div className="release-mode" key={r.download_base}>
                <div className="release-mode-heading">
                  <div>
                    <h3>{MODES[r.mode]?.[0] ?? r.mode}</h3>
                    <p>{MODES[r.mode]?.[1]}</p>
                  </div>
                  <a className="button secondary" href={r.download_base + "/manifest.json"}>
                    <Download size={16} aria-hidden="true" /> Manifest
                  </a>
                </div>
                <ReleaseFiles release={r} />
              </div>
            ))}
          </section>
        ))
      )}
    </div>
  );
}
type Tier = {
  id: string;
  name: string;
  allowance: { requests_per_minute: number; concurrency: number; jurisdictions?: number; watches: number; digest: string; webhook_endpoints: number };
  listing: { order: number; price: string; action?: string; limits?: string };
};

const API_ORIGIN = "https://api.open.alvary.ai";

// The plans as the API publishes them (the same list as the console's Plan and limits page).
function Plans() {
  const [tiers, setTiers] = useState<Tier[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/v1/tiers", { signal: controller.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: { tiers: Tier[] }) => setTiers(d.tiers.filter((t) => t.listing?.action).sort((a, b) => a.listing.order - b.listing.order)))
      .catch((e) => e.name !== "AbortError" && setFailed(true));
    return () => controller.abort();
  }, []);
  if (failed) return <p>See Plan and limits in the console for the current plans.</p>;
  if (!tiers) return <p>Loading the plans…</p>;
  const n = (v: number) => v.toLocaleString("en");
  return (
    <div className="plan-table-wrap">
      <table className="plan-table">
        <thead>
          <tr>
            <th>Plan</th>
            <th>Price</th>
            <th>Requests a minute</th>
            <th>At once</th>
            <th>Countries</th>
            <th>Monitors</th>
            <th>Webhooks</th>
          </tr>
        </thead>
        <tbody>
          {tiers.map((t) => (
            <tr key={t.id}>
              <th scope="row">{t.name}</th>
              <td>{t.listing.price}</td>
              <td>{t.listing.limits ?? n(t.allowance.requests_per_minute)}</td>
              <td>{t.listing.limits ? "" : n(t.allowance.concurrency)}</td>
              <td>{t.allowance.jurisdictions ? `${t.allowance.jurisdictions} of your choice` : "All"}</td>
              <td>
                {n(t.allowance.watches)}, {t.allowance.digest} email
              </td>
              <td>{t.allowance.webhook_endpoints ? n(t.allowance.webhook_endpoints) : "None"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Developers() {
  return (
    <div className="page developers">
      <PageTitle
        label="BUILD ON THE PUBLIC LAYER"
        title="Legal data. Without the guesswork."
      >
        Search and read the published corpus from your own code and AI tools,
        through a REST API and an MCP server. Reading this website and
        downloading the releases need no account.
      </PageTitle>

      <section className="start-steps" aria-labelledby="start">
        <h2 id="start">Start in three steps</h2>
        <ol>
          <li>
            <b>Sign in to the console.</b> Every new organisation gets the free
            Starter plan at once, with no card.
          </li>
          <li>
            <b>Create an API key</b>, or connect Claude, ChatGPT, Claude Code or
            Codex.
          </li>
          <li>
            <b>Make your first request</b>, as below.
          </li>
        </ol>
        <div className="start-actions">
          <a className="button primary" href="/console">
            Open the console <ArrowRight size={16} aria-hidden="true" />
          </a>
          <Link className="text-link" to="/docs">
            Read the documentation <ArrowRight size={16} aria-hidden="true" />
          </Link>
          <Link className="text-link" to="/docs/console">
            The console, step by step, with pictures <ArrowRight size={16} aria-hidden="true" />
          </Link>
        </div>
      </section>

      <div className="two-columns">
        <section>
          <h2>REST API</h2>
          <p>
            Send your key in the Authorization header. Answers are JSON;
            refusals are problem documents with a code.
          </p>
          <pre className="dev-code" dir="ltr">{`curl -G ${API_ORIGIN}/api/v1/search \\
  -d q=land -d jurisdiction=rw \\
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"`}</pre>
          <dl className="endpoints">
            <dt><code>GET /api/v1/search?q=</code></dt>
            <dd>Search titles and citations. Filter with <code>jurisdiction</code> and <code>access</code>; page with <code>limit</code> and <code>offset</code>.</dd>
            <dt><code>GET /api/v1/sources</code></dt>
            <dd>List records, with the same filters.</dd>
            <dt><code>GET /api/v1/sources/{"{id}"}</code></dt>
            <dd>One record: metadata, rights status and official links. <code>/versions</code> lists its texts; <code>/api/v1/rights/{"{id}"}</code> explains its rights decision.</dd>
            <dt><code>GET /api/v1/citations/{"{citation}"}</code></dt>
            <dd>The records a citation points to.</dd>
            <dt><code>GET /api/v1/jurisdictions</code>, <code>/coverage</code></dt>
            <dd>The countries held, and counts for each.</dd>
            <dt><code>GET /api/v1/changes</code></dt>
            <dd>What changed, in order (below).</dd>
          </dl>
          <p>
            Every endpoint and field, with examples:{" "}
            <Link className="text-link" to="/docs/api">
              the API reference <ArrowRight size={16} aria-hidden="true" />
            </Link>
          </p>
        </section>
        <section>
          <h2>MCP server</h2>
          <p>
            AI tools connect to <code>{API_ORIGIN}/mcp</code>. They sign you in
            and you approve them for one organisation; no key is needed. Tools
            that take a header can send a key instead.
          </p>
          <dl className="endpoints">
            <dt>Claude</dt>
            <dd>Settings → Connectors → Add custom connector, and paste the address.</dd>
            <dt>Claude Code</dt>
            <dd><code>claude mcp add --transport http open-alvary {API_ORIGIN}/mcp</code>, then <code>/mcp</code> and Authenticate.</dd>
            <dt>Codex</dt>
            <dd><code>codex mcp add open-alvary --url {API_ORIGIN}/mcp</code>, then <code>codex mcp login open-alvary</code>.</dd>
            <dt>ChatGPT and other tools</dt>
            <dd>The same address, in the tool's settings for MCP connectors.</dd>
          </dl>
          <p>
            Tools: <code>search_catalogue</code>, <code>get_source</code>,{" "}
            <code>get_relationships</code>, <code>list_changes</code>,{" "}
            <code>get_change</code> and <code>get_coverage</code>.
          </p>
          <Link className="text-link" to="/docs/console#apps">
            Connecting a tool, with pictures <ArrowRight size={16} aria-hidden="true" />
          </Link>
        </section>
      </div>

      <section className="info-panel developer-plans">
        <h2>Plans and limits</h2>
        <p>
          Every plan reads the same published corpus. Limits are shared by every
          key and app in an organisation; with a plan that names countries,
          others still answer at the Starter rate. Ask for another plan in the
          console, under Plan and limits.
        </p>
        <Plans />
        <p>
          Past a limit, requests are refused with <code>429</code> and a{" "}
          <code>Retry-After</code> header. Every refusal's code is explained on
          the <Link to="/docs/errors">errors page</Link>.
        </p>
      </section>

      <section className="info-panel developer-changes">
        <h2>Follow changes</h2>
        <p>
          Every record added, corrected or withdrawn, and every text published,
          is recorded in order. No account is needed.
        </p>
        <dl>
          <dt>Replay and keep up</dt>
          <dd>
            <code>GET /api/changes</code> answers changes oldest first with a{" "}
            <code>resume_token</code>; send it back to get only newer ones.
            Filter with <code>jurisdiction</code>, <code>work</code>,{" "}
            <code>type</code>, <code>class</code>, <code>language</code> and{" "}
            <code>q</code>. Deduplicate by <code>feed_epoch</code>,{" "}
            <code>event_id</code> and <code>event_revision</code>; a{" "}
            <code>feed_reset</code> answer means reconcile from a current release.
          </dd>
          <dt>Feeds</dt>
          <dd>
            <code>/api/changes/feed.atom</code> and{" "}
            <code>/api/changes/feed.rss</code>, with the same filters: the last
            90 days, newest first.
          </dd>
          <dt>MCP</dt>
          <dd>
            The <code>list_changes</code> and <code>get_change</code> tools.
          </dd>
          <dt>Monitors and webhooks</dt>
          <dd>
            In the console, monitors follow countries, records and kinds of
            change, with an inbox and daily or weekly email. On paid plans they
            can also POST each match to your HTTPS endpoint, signed with
            HMAC-SHA256 over <code>timestamp.body</code> (headers{" "}
            <code>Open-Alvary-Timestamp</code> and{" "}
            <code>Open-Alvary-Signature</code>), retried for 24 hours.{" "}
            <Link to="/docs/console#monitor">Setting up a monitor</Link>.
          </dd>
          <dt>What it covers</dt>
          <dd>
            <code>/api/change-coverage</code>: when recording began and what is
            held for each country. Changes cover what Open Alvary holds, not
            every change in a country's law.
          </dd>
        </dl>
      </section>

      <div className="two-columns">
        <section>
          <h2>Download the data</h2>
          <p>
            Each release is a set of JSONL files with a SHA-256 manifest,
            recording its scope and the rights status of every record. No
            blanket licence is claimed over linked legal text. No account is
            needed.
          </p>
          <Link className="text-link" to="/releases">
            Go to the releases <ArrowUpRight size={16} aria-hidden="true" />
          </Link>
        </section>
        <section>
          <h2>Help</h2>
          <p>
            The <Link to="/docs">documentation</Link> has guides, the API
            reference and a <Link to="/docs/console">console guide</Link> with
            pictures. Each refusal from the API has a code on the{" "}
            <Link to="/docs/errors">errors page</Link>. For anything else, write
            to <a href="mailto:hello@ruhu.ai">hello@ruhu.ai</a>.
          </p>
        </section>
      </div>
      <div className="notice">
        Search covers catalogue metadata and cleared full text only.
      </div>
    </div>
  );
}

function About() {
  const project = useContext(ProjectContext);
  return (
    <div className="page">
      <PageTitle
        label="THE PROJECT & THE PEOPLE"
        title="A public foundation, built in the open."
      >
        Open Alvary is building reusable legal-data infrastructure for African
        jurisdictions, each added as its sources are verified. This is an early
        catalogue preview.
      </PageTitle>
      <div className="two-columns">
        <section>
          <h2>What exists today</h2>
          <p>
            A catalogue of independently authored source records with explicit
            rights states, versioned metadata downloads and open-source
            software. No legal full text has been cleared or republished yet.
          </p>
          <p>
            The public website, API and ingestion software are MIT-licensed.
            Researchers, legal-aid groups and other developers use the same
            public interfaces.
          </p>
          <ProjectRepository />
        </section>
        <section>
          <h2>Who maintains it</h2>
          <p>
            {project.maintainer
              ? `Accountable maintainer: ${project.maintainer.replace(/\.$/, "")}.`
              : "Maintainer identity is being confirmed before public launch."}
          </p>
          <p>
            Rights and content review are separate responsibilities. No
            independent legal review panel or institutional partnership is
            claimed.
          </p>
          <h3>Contact & corrections</h3>
          {project.contact_email ? (
            <p>
              <a className="text-link" href={`mailto:${project.contact_email}`}>
                {project.contact_email}
              </a>
              <br />
              Report a source error, rights concern, accessibility issue or
              interest in contributing. Include the source ID and official URL.
              Please do not attach confidential documents.
            </p>
          ) : (
            <p>
              A monitored contact address must be configured before public
              launch.
            </p>
          )}
        </section>
      </div>
      <div className="info-panel">
        <h2>Contribute carefully</h2>
        <p>
          Useful contributions include source discovery, parser improvements,
          schema feedback, accessibility fixes and tests. Submit independently
          authored metadata and official links. Do not upload uncleared legal
          text, publisher compilations, private client documents or proprietary
          code.
        </p>
        {project.repository_url && (
          <a
            className="text-link"
            href={`${project.repository_url}/blob/main/CONTRIBUTING.md`}
            target="_blank"
            rel="noreferrer"
          >
            Read the contribution guide <ArrowUpRight size={16} />
          </a>
        )}
      </div>
      <div className="two-columns">
        <section>
          <h2>Licensing boundaries</h2>
          <p>
            <b>Software and project documentation:</b> MIT. Dependency licences
            remain applicable.
          </p>
          <p>
            <b>Original catalogue metadata:</b>{" "}
            {project.metadata_licence === "CC-BY-4.0"
              ? "CC BY 4.0. Attribute Open Alvary contributors, link to the licence and indicate changes. This does not licence any linked source text."
              : "A data licence is awaiting owner approval. No blanket open-data licence is asserted."}
          </p>
          <p>
            <b>Legal texts:</b> source-specific rights only. Official
            availability does not establish permission to republish.
          </p>
        </section>
        <section>
          <h2>What comes next</h2>
          <p>
            Qualified rights review, source-specific parsers, tested OCR
            integration, detailed legal anchors and review operations. Expansion
            across African jurisdictions depends on verified permissions, source
            availability, qualified reviewers and content quality.
          </p>
          <p>
            We are preparing a funding application for development and
            evaluation. Funding has not been awarded. Proposed outcomes are
            milestones, not existing capabilities.
          </p>
          {project.repository_url && (
            <a
              className="text-link"
              href={project.repository_url}
              target="_blank"
              rel="noreferrer"
            >
              View the repository <ArrowRight size={16} />
            </a>
          )}
        </section>
      </div>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
