import React, { useContext, useEffect, useRef, useState } from "react";
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
import {
  PageTitle,
  ProjectContext,
  ProjectRepository,
  SiteFooter,
  SiteHeader,
  SourceRecord,
  SourceRow,
  jurisdictionName,
  links,
  type Project,
  type RightsRecord,
  type Source,
} from "./site";

type Coverage = {
  catalogued_sources: number;
  open_full_text_sources: number;
  metadata_only_sources: number;
  published_versions: number;
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
        <a href="#/">Return home</a>
      </div>
    ) : (
      this.props.children
    );
  }
}

function NotFound() {
  return (
    <div className="page">
      <h1>Page not found</h1>
      <a href="#/">Return home</a>
    </div>
  );
}

function App() {
  const project = useData<Project>("/project");
  const [route, setRoute] = useState(location.hash.slice(1) || "/");
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    const change = () => {
      setRoute(location.hash.slice(1) || "/");
      setMenu(false);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
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
    ].find(([url]) => url === pathname)?.[1];
    document.title = title
      ? `${title} · Open Alvary`
      : "Open Alvary · A shared foundation for law";
    document.querySelector<HTMLElement>("main")?.focus({ preventScroll: true });
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
          ) : pathname === "/governance" ? (
            <Governance />
          ) : pathname === "/releases" ? (
            <Releases />
          ) : pathname === "/about" ? (
            <About />
          ) : pathname === "/privacy" ? (
            <Privacy />
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
            <a className="button primary" href="#/browse">
              Explore the corpus <ArrowRight size={17} />
            </a>
            <a className="text-link" href="#/developers">
              Build with the data <ArrowUpRight size={17} />
            </a>
          </div>
          <div className="pilot">
            <span className="flag">▌</span>
            <span>Starting in Nigeria</span>
            <span className="pilot-divider" />
            <span>v0.1 · Catalogue preview</span>
          </div>
        </div>
        <div
          className="hero-art"
          aria-label="A connected legal corpus with source, structure, provenance and access layers"
        >
          <div className="art-grid" />
          <div className="art-top">
            <span>THE OPEN CORPUS</span>
            <span>01 / NG</span>
          </div>
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="data-card layer-back">
            <div>
              <Globe2 size={18} /> Authoritative sources
            </div>
            <small>GOVERNMENT · COURTS · REGULATORS</small>
          </div>
          <div className="data-card layer-middle">
            <div>
              <ShieldCheck size={18} /> Rights & provenance
            </div>
            <small>VERIFY · ATTRIBUTE · TRACE</small>
          </div>
          <div className="data-card layer-front">
            <span className="tiny-label">STRUCTURED LEGAL DATA</span>
            <div className="code-row">
              <span>01</span>
              <b>source</b>
              <i>"ng"</i>
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
          <small>THE FIRST CHAPTER</small>
          <strong>
            Nigeria <span>NG</span>
          </strong>
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
          <a className="text-link" href="#/browse">
            View the catalogue <ArrowRight size={17} />
          </a>
        </div>
        <form
          className="home-search"
          onSubmit={(e) => {
            e.preventDefault();
            location.hash = "/browse?q=" + encodeURIComponent(q);
          }}
        >
          <Search size={20} />
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
          <span className="dot amber" /> The pilot contains catalogue metadata
          only. Legal full text remains withheld pending rights and content
          review.
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
          <a className="text-link" href="#/governance">
            Our approach to governance <ArrowUpRight size={17} />
          </a>
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
        <a className="button light" href="#/developers">
          For developers <ArrowUpRight size={17} />
        </a>
      </section>
    </>
  );
}
const PAGE_SIZE = 20;
function SourceList(props: { compact?: boolean; q?: string; filter?: string }) {
  return (
    <SourcePageList
      key={JSON.stringify([
        props.q ?? "",
        props.filter ?? "all",
        props.compact ?? false,
      ])}
      {...props}
    />
  );
}
function SourcePageList({
  compact = false,
  q = "",
  filter = "all",
}: {
  compact?: boolean;
  q?: string;
  filter?: string;
}) {
  const [page, setPage] = useState(1);
  const resultsRef = useRef<HTMLDivElement>(null);
  const pageNavigation = useRef(false);
  const movePage = (next: number) => {
    pageNavigation.current = true;
    setPage(next);
  };
  const params = new URLSearchParams({ access: filter, page: String(page) });
  if (q) params.set("q", q);
  const { data, error } = useData<{
    items: Source[];
    total: number;
    page: number;
    last_page: number;
  }>((q ? "/search?" : "/sources?") + params);
  useEffect(() => {
    if (data && page > 1 && page > data.last_page) {
      setPage(Math.max(1, data.last_page));
    }
  }, [data, page]);
  useEffect(() => {
    if (data && page <= Math.max(1, data.last_page) && pageNavigation.current) {
      resultsRef.current?.focus();
      pageNavigation.current = false;
    }
  }, [data, page]);
  if (!data || (page > 1 && page > data.last_page))
    return <Feedback error={error} />;
  // Fixed pages of 20, at most 25 of them for a search or filter (MAC-01).
  const first = (page - 1) * PAGE_SIZE;
  const items = compact ? data.items.slice(0, 6) : data.items;
  const capped = data.total > data.last_page * PAGE_SIZE;
  return (
    <>
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
          />
        ))}
      </div>
      {!items.length && (
        <div className="empty">
          <BookOpen size={30} />
          <h3>No sources found</h3>
          <p>
            Try another title or citation. No full text is cleared in this
            pilot.
          </p>
          <a href="#/browse">Reset search</a>
        </div>
      )}
      {data.total > 0 && (
        <div className="catalogue-pagination" aria-label="Catalogue pagination">
          <p role="status">
            Showing {first + 1}–{first + items.length} of {data.total} sources
          </p>
          {!compact && capped && page === data.last_page && (
            <p>
              Only the first {data.last_page * PAGE_SIZE} results are shown.
              Refine the search to narrow them.
            </p>
          )}
          {compact ? (
            <a href={"#/browse" + (q ? "?q=" + encodeURIComponent(q) : "")}>
              View all results <ArrowRight size={16} />
            </a>
          ) : (
            <nav aria-label="Results pages">
              <button disabled={page === 1} onClick={() => movePage(page - 1)}>
                Previous
              </button>
              <button
                disabled={page >= data.last_page}
                onClick={() => movePage(page + 1)}
              >
                Next
              </button>
            </nav>
          )}
        </div>
      )}
    </>
  );
}
function Browse({ route }: { route: string }) {
  const query = new URLSearchParams(route.split("?")[1]).get("q") || "";
  const [q, setQ] = useState(query);
  const [filter, setFilter] = useState("all");
  useEffect(() => setQ(query), [query]);
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
        <label>
          Access{" "}
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">All sources</option>
            <option value="metadata">Metadata only</option>
            <option value="open">Open full text</option>
          </select>
        </label>
      </div>
      <SourceList q={query} filter={filter} />
    </div>
  );
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
  if (!source.data)
    return (
      <div className="page">
        <Feedback error={source.error} />
      </div>
    );
  const s = source.data;
  return (
    <div className="page">
      <a className="breadcrumb" href="#/browse">
        Catalogue <ChevronRight size={14} />{" "}
        {jurisdictionName(s.jurisdiction_id)}
      </a>
      <PageTitle
        label={s.document_type.toUpperCase() + " · " + s.citation}
        title={s.title}
      >
        {s.catalogue_note}
      </PageTitle>
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
                <ShieldCheck size={36} />
                <h2>Rights review comes first.</h2>
                <p>
                  {versions.data.notice} You can consult the official source
                  directly.
                </p>
                <a
                  className="button primary"
                  href={s.canonical_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Visit official source <ExternalLink size={16} />
                </a>
              </div>
            )
          ) : (
            <Feedback error={versions.error} />
          )}
        </article>
        <aside>
          <SourceRecord source={s} rights={rights.data?.record} />
        </aside>
      </div>
    </div>
  );
}
function CoveragePage() {
  const { data, error } = useData<Coverage>("/coverage");
  return (
    <div className="page">
      <PageTitle
        label="COVERAGE & LIMITATIONS"
        title="An honest view of the corpus."
      >
        We publish what we have verified, and make the gaps visible. This is a
        catalogue pilot, not a complete collection of Nigerian law.
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
          <div className="info-panel">
            <h2>
              Nigeria <span className="badge">Pilot</span>
            </h2>
            <p>
              The initial catalogue covers four federal instruments. There is no
              claimed coverage of state law, judgments, regulations,
              translations, or all subsequent amendments.
            </p>
            <p>
              The 2007 Investments and Securities Act is retained as a
              historical candidate. Its official publisher identifies a 2025
              replacement.
            </p>
            <a className="text-link" href="#/browse">
              Explore the catalogue <ArrowRight size={16} />
            </a>
          </div>
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
function Releases() {
  const { data, error } = useData<Manifest[]>("/releases");
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
        data.map((r) => (
          <section className="release-card" key={r.download_base}>
            <div className="release-heading">
              <div>
                <div className="eyebrow">NIGERIA · {r.mode.toUpperCase()}</div>
                <h2>{r.release_id}</h2>
                <p>
                  {r.coverage.catalogued_sources} catalogue entries ·{" "}
                  {r.coverage.open_full_text_sources} cleared full-text sources
                </p>
              </div>
              <a
                className="button secondary"
                href={r.download_base + "/manifest.json"}
              >
                <Download size={16} /> Manifest
              </a>
            </div>
            <div className="download-grid">
              {Object.entries(r.files).map(([name, info]) => (
                <a key={name} href={r.download_base + "/" + name}>
                  <FileText size={16} />
                  <span>
                    {name}
                    <small>
                      {info.records !== undefined
                        ? `${info.records} records`
                        : "Release documentation"}
                    </small>
                  </span>
                  <Download size={15} />
                </a>
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  );
}
function Developers() {
  return (
    <div className="page">
      <PageTitle
        label="BUILD ON THE PUBLIC LAYER"
        title="Legal data. Without the guesswork."
      >
        Reading this website needs no account. For software and research, take
        the versioned releases: explicit rights states, provenance and
        checksums.
      </PageTitle>
      <div className="two-columns">
        <section>
          <h2>Download the data</h2>
          <p>
            Each release is a set of JSONL files with a SHA-256 manifest,
            recording its scope and the rights status of every record. No
            blanket licence is claimed over linked legal text.
          </p>
          <a className="text-link" href="#/releases">
            Go to the releases <ArrowUpRight size={16} />
          </a>
        </section>
        <section>
          <h2>Machine access</h2>
          <p>
            A REST API and an MCP server for developers and AI tools are being
            prepared, for organisations with a subscription or an approved
            waiver. They are not open yet; this page will explain how to get
            access when they are.
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
        jurisdictions, starting with Nigeria. This is an early metadata
        catalogue pilot.
      </PageTitle>
      <div className="two-columns">
        <section>
          <h2>What exists today</h2>
          <p>
            Four independently authored source records, explicit rights states,
            versioned metadata downloads and open-source
            software. No Nigerian legal full text has been cleared or
            redistributed.
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
              ? `Accountable maintainer: ${project.maintainer}.`
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
            availability, qualified reviewers and content quality. Nigeria is
            the first implementation pilot.
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
function Privacy() {
  const project = useContext(ProjectContext);
  return (
    <div className="page">
      <PageTitle
        label="PRIVACY & USE"
        title="A small public service, with clear limits."
      >
        The pilot has no account registration, advertising, third-party
        analytics, document uploads or AI chat.
      </PageTitle>
      <div className="two-columns">
        <section>
          <h2>Data used to operate the site</h2>
          <p>
            Your browser requests pages, fonts and data from this site. Search
            terms are sent to this site's server to return matching public
            records.
            Fonts are bundled locally. We do not set application cookies or use
            browser storage to track visitors.
          </p>
          <p>
            Hosting infrastructure processes connection information such as IP
            addresses and may retain request metadata and operational logs. The
            Docker configuration disables application access logs; a managed
            host such as Vercel has separate logging and retention settings. The
            operator must verify those settings for the deployed service.
          </p>
          <p>
            Please avoid entering personal, confidential or client-specific
            information in searches.
          </p>
        </section>
        <section>
          <h2>Links and correspondence</h2>
          <p>
            Official-source links and GitHub are separate services with their
            own terms and privacy practices. Opening an email link uses your
            chosen email provider.
          </p>
          <p>
            If you contact the project, the maintainer receives the information
            you choose to send. Share only what is necessary to address your
            request; do not attach private legal files.
          </p>
          {project.contact_email ? (
            <a className="text-link" href={`mailto:${project.contact_email}`}>
              Contact the maintainer <ArrowUpRight size={16} />
            </a>
          ) : (
            <p>A public contact must be configured before launch.</p>
          )}
          <h2>Using the catalogue</h2>
          <p>
            This service provides source discovery and provenance, not legal
            advice. Coverage is incomplete and currentness may be unverified.
            Consult the official source and the relevant rights record.
            Availability of a download does not grant rights to external source
            documents.
          </p>
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
