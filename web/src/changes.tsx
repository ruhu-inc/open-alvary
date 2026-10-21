// The public record of changes (SUB C1, changes-contracts.md): records added, corrected or withdrawn
// and texts published, newest first, with the coverage they rest on and feeds to follow them.
import { useEffect, useMemo, useState } from "react";
import { Bell, Check, Copy, ExternalLink, History, Rss } from "lucide-react";
import { Link, PageTitle, jurisdictionName } from "./site";
import { copyText } from "./tools";

export type ChangeEvent = {
  event_id: string;
  event_type: string;
  committed_at: string;
  jurisdiction: { id: string; name: string } | null;
  work: { id: string; title: string; citation: string | null; url: string } | null;
  version: { id: string } | null;
  changed_fields: string[];
  change_reason: string | null;
  summary: string;
  sources: string[];
};
type Coverage = {
  feed_started_at: string | null;
  jurisdictions: { id: string; name: string; works: number; last_change_at: string | null }[];
  notice: string;
};

const TYPES: [string, string][] = [
  ["", "All changes"],
  ["catalogue_added", "Added"],
  ["metadata_corrected", "Corrected"],
  ["text_version_published", "Text published"],
  ["withdrawal", "Withdrawn"],
  ["coverage_changed", "Coverage"],
  ["membership_recorded", "Membership"],
];
const LABEL = Object.fromEntries(TYPES.filter(([t]) => t));

// The website's own data path, as other pages read it.
function useSite<T>(path: string) {
  const [state, set] = useState<{ path: string; data?: T; error?: string }>({ path: "" });
  useEffect(() => {
    const controller = new AbortController();
    fetch("/site/v1" + path, { signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`Request failed (${r.status})`);
        return r.json();
      })
      .then((data) => !controller.signal.aborted && set({ path, data }))
      .catch((e) => !controller.signal.aborted && e.name !== "AbortError" && set({ path, error: e.message }));
    return () => controller.abort();
  }, [path]);
  return state.path === path ? state : { data: undefined, error: undefined };
}

function day(value: string | null | undefined) {
  if (!value) return "";
  return new Date(value).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

function query(filters: Record<string, string>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  const text = params.toString();
  return text ? "?" + text : "";
}

export function ChangeList({ events, showWork = true }: { events: ChangeEvent[]; showWork?: boolean }) {
  return (
    <ol className="change-list">
      {events.map((e) => (
        <li key={e.event_id} className="change-row">
          <div className="change-meta">
            <span className={"badge" + (e.event_type === "withdrawal" ? "" : " green")}>
              {LABEL[e.event_type] ?? e.event_type}
            </span>
            <time dateTime={e.committed_at}>{day(e.committed_at)}</time>
            {e.jurisdiction ? <span>{e.jurisdiction.name || jurisdictionName(e.jurisdiction.id)}</span> : null}
          </div>
          {showWork && e.work ? (
            <h3>
              <Link to={"/sources/" + encodeURIComponent(e.work.id)}>{e.work.title}</Link>
            </h3>
          ) : null}
          <p>{e.summary}</p>
          {showWork && e.sources.length ? (
            <a className="text-link" href={e.sources[0]} target="_blank" rel="noreferrer">
              Official source <ExternalLink size={14} aria-hidden="true" />
            </a>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

export function ChangesPage({ route }: { route: string }) {
  const initial = new URLSearchParams(route.split("?")[1] ?? "");
  const [filters, setFilters] = useState({
    jurisdiction: initial.get("jurisdiction") ?? "",
    type: initial.get("type") ?? "",
    q: initial.get("q") ?? "",
  });
  const [typed, setTyped] = useState(filters.q);
  const [copied, setCopied] = useState(false);
  const coverage = useSite<Coverage>("/change-coverage");
  const recent = useSite<{ events: ChangeEvent[] }>("/changes/recent" + query(filters));
  useEffect(() => {
    // Keep the address in step so the view can be shared; search terms go in only when typed here.
    history.replaceState(null, "", "/changes" + query(filters));
    setCopied(false);
  }, [filters]);
  const feed = useMemo(() => location.origin + "/api/changes/feed.atom" + query(filters), [filters]);
  const countries = coverage.data?.jurisdictions ?? [];
  return (
    <div className="page">
      <PageTitle label="CHANGES" title="What changed in the catalogue.">
        Records added, corrected or withdrawn, and texts published, newest first. These are changes to
        what Open Alvary holds, not every change in a country's law.
      </PageTitle>
      <div className="change-coverage info-panel">
        <History size={18} aria-hidden="true" />
        <p>
          {coverage.data?.feed_started_at
            ? `Changes are recorded from ${day(coverage.data.feed_started_at)}. Earlier records are in the releases.`
            : "The record of changes has not started yet."}{" "}
          {countries.length
            ? "Covered: " +
              countries
                .map((c) => `${c.name} (${c.works} ${c.works === 1 ? "record" : "records"})`)
                .join(", ") +
              "."
            : ""}
        </p>
      </div>
      <form
        className="change-filters"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters({ ...filters, q: typed.trim() });
        }}
      >
        <label>
          Country
          <select value={filters.jurisdiction} onChange={(e) => setFilters({ ...filters, jurisdiction: e.target.value })}>
            <option value="">All countries</option>
            {countries.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Change
          <select value={filters.type} onChange={(e) => setFilters({ ...filters, type: e.target.value })}>
            {TYPES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="grow">
          Keyword
          <input type="search" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Title or citation" />
        </label>
        <button className="button primary" type="submit">
          Filter
        </button>
      </form>
      <div className="change-feeds">
        <button
          className="text-link"
          type="button"
          onClick={async () => setCopied(await copyText(feed))}
          aria-live="polite"
        >
          {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
          {copied ? "Feed link copied" : "Copy feed link"}
        </button>
        <a className="text-link" href={feed.replace("feed.atom", "feed.rss")}>
          <Rss size={14} aria-hidden="true" /> RSS
        </a>
        <Link className="text-link" to="/developers">
          Changes API and MCP
        </Link>
      </div>
      {recent.data ? (
        recent.data.events.length ? (
          <ChangeList events={recent.data.events} />
        ) : (
          <div className="empty">
            <History size={36} aria-hidden="true" />
            <h3>{filters.jurisdiction || filters.type || filters.q ? "No changes match." : "No changes yet."}</h3>
            <p>This page shows the last 90 days. The API replays everything still kept.</p>
          </div>
        )
      ) : (
        <p className="change-loading">{recent.error ? "Changes could not be loaded. Try again shortly." : "Loading…"}</p>
      )}
    </div>
  );
}

// The changes to one record, beside the record itself.
export function RecordChanges({ id }: { id: string }) {
  const recent = useSite<{ events: ChangeEvent[] }>("/changes/recent?work=" + encodeURIComponent(id));
  const events = recent.data?.events ?? [];
  return (
    <section className="info-panel record-changes" aria-labelledby="record-changes">
      <h3 id="record-changes">Changes to this record</h3>
      {events.length ? (
        <ChangeList events={events} showWork={false} />
      ) : (
        <p>{recent.data ? "No changes in the last 90 days." : "Loading…"}</p>
      )}
      <div className="record-follow">
        <a className="text-link" href={"/console/monitor?new=1&work=" + encodeURIComponent(id)}>
          <Bell size={14} aria-hidden="true" /> Monitor this record
        </a>
        <a className="text-link" href={"/api/changes/feed.atom?work=" + encodeURIComponent(id)}>
          <Rss size={14} aria-hidden="true" /> Feed
        </a>
      </div>
    </section>
  );
}
