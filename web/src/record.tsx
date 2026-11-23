// The record page's own parts (design approved 2026-11-23): three cards under the title that answer
// where the official text is, whether it is in force and what it replaces; a row of ways to follow it;
// a section that says why the text is or is not on this site and how far the record has got; and one
// timeline of its checked links and its public changes.
import { useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  Bell,
  Check,
  Copy,
  FileText,
  Rss,
} from "lucide-react";
import { LABEL, useSite, type ChangeEvent } from "./changes";
import { KINDS, day, type History, type Related } from "./history";
import {
  LEGAL_STATUS,
  Link,
  RIGHTS_STATUS,
  officialHost,
  statusLabel,
  typeLabel,
  type Source,
} from "./site";
import { copyText } from "./tools";

const KIND_LABEL = Object.fromEntries(KINDS);
// The links that change what a law is: shown as the third card, newest first.
const EFFECT_KINDS = [
  "repealed_by",
  "superseded_by",
  "repeals",
  "supersedes",
  "amended_by",
  "amends",
  "commenced_by",
];
const record = (id: string) => "/sources/" + encodeURIComponent(id);

export function RecordCards({
  source: s,
  history,
}: {
  source: Source;
  history?: History;
}) {
  const effect = history?.items.find((r) => EFFECT_KINDS.includes(r.kind));
  const known = s.legal_status !== "unknown";
  return (
    <div className="record-cards">
      <a
        className="record-card"
        href={s.canonical_url}
        target="_blank"
        rel="noreferrer"
      >
        <span className="record-card-k">
          Official source <ArrowUpRight size={14} aria-hidden="true" />
        </span>
        <span className="record-card-v">{officialHost(s.canonical_url)}</span>
        <span className="record-card-d">
          {s.url_kind === "document"
            ? "The official document"
            : "The official page for it"}
          {s.authority_name ? ` · ${s.authority_name}` : ""}
        </span>
      </a>
      <div className="record-card">
        <span className="record-card-k">
          {s.document_type === "judgment" ? "Status" : "Legal status"}
        </span>
        <span className="record-card-v">
          <span className={"status-chip " + s.legal_status}>
            {statusLabel(s)}
          </span>
        </span>
        <span className="record-card-d">
          {known && s.status_evidence ? (
            <>
              Evidence:{" "}
              <a href={s.status_evidence} target="_blank" rel="noreferrer">
                {officialHost(s.status_evidence)}
              </a>
            </>
          ) : s.document_type === "judgment" ? (
            "Published as delivered"
          ) : (
            "Not yet confirmed against an official source"
          )}
        </span>
      </div>
      {effect ? (
        <div className="record-card">
          <span className="record-card-k">
            {KIND_LABEL[effect.kind] ?? effect.kind}
            {effect.verified ? <Check size={14} aria-label="checked" /> : null}
          </span>
          <span className="record-card-v">
            <Link to={record(effect.source_id)}>{effect.title}</Link>
          </span>
          <span className="record-card-d">
            {effect.effective_date
              ? `From ${day(effect.effective_date)} · `
              : ""}
            checked against the official text
          </span>
        </div>
      ) : s.citation !== s.title || !s.authority_name ? (
        <div className="record-card">
          <span className="record-card-k">Citation</span>
          <span className="record-card-v">{s.citation}</span>
          <span className="record-card-d">How the record is cited</span>
        </div>
      ) : (
        <div className="record-card">
          <span className="record-card-k">
            {s.document_type === "judgment" ? "Court" : "Issued by"}
          </span>
          <span className="record-card-v">{s.authority_name}</span>
          <span className="record-card-d">
            {s.year
              ? `${typeLabel(s.document_type)} of ${s.year}`
              : typeLabel(s.document_type)}
          </span>
        </div>
      )}
    </div>
  );
}

export function RecordActions({ source: s }: { source: Source }) {
  const [copied, setCopied] = useState<"" | "copied" | "failed">("");
  return (
    <div className="record-actions">
      <a
        className="button outline"
        href={"/console/monitor?new=1&work=" + encodeURIComponent(s.id)}
      >
        <Bell size={16} aria-hidden="true" /> Monitor this record
      </a>
      <a
        className="button ghost"
        href={"/api/changes/feed.atom?work=" + encodeURIComponent(s.id)}
      >
        <Rss size={16} aria-hidden="true" /> Feed
      </a>
      <button
        type="button"
        className="button ghost"
        onClick={async () =>
          setCopied((await copyText(s.citation)) ? "copied" : "failed")
        }
      >
        {copied === "copied" ? (
          <Check size={16} aria-hidden="true" />
        ) : (
          <Copy size={16} aria-hidden="true" />
        )}
        {copied === "copied"
          ? "Citation copied"
          : copied === "failed"
            ? "Copy failed"
            : "Copy citation"}
      </button>
    </div>
  );
}

// Why the text is not here yet, and how far the record has got. Only what the record states: its
// official link, its rights status and whether a full text is published.
export function TextStatus({ source: s }: { source: Source }) {
  const rights = RIGHTS_STATUS[s.rights_status] ?? s.rights_status;
  const linkOnly =
    s.rights_status === "LINK_ONLY" || s.rights_status === "RESTRICTED";
  const steps: {
    title: string;
    detail: string;
    state: "done" | "now" | "next";
    tag?: string;
  }[] = [
    {
      title: "Official source found",
      detail:
        officialHost(s.canonical_url) +
        (s.authority_name ? ` · ${s.authority_name}` : ""),
      state: "done",
    },
    linkOnly
      ? {
          title: "Rights review",
          detail: "Its rights allow a link, not a copy.",
          state: "done",
          tag: rights,
        }
      : {
          title: "Rights review",
          detail: "May this text be republished?",
          state: "now",
          tag: rights,
        },
    {
      title: "Content review",
      detail: "Every page checked against the official copy",
      state: "next",
    },
    {
      title: "Text published here",
      detail: "Readable, searchable and citable by section",
      state: "next",
    },
  ];
  return (
    <section className="text-status" aria-labelledby="text-status">
      <div className="reader-bar">
        <FileText size={18} aria-hidden="true" /> Source text{" "}
        <span className="badge">Details and official link</span>
      </div>
      <div className="text-status-body">
        <h2 id="text-status">
          {linkOnly
            ? "The text isn’t on this site."
            : "The text isn’t on this site yet."}
        </h2>
        <p>
          {linkOnly
            ? "Its rights allow Open Alvary to link to it, not to republish it. Read it at the official source."
            : "Being published by the government does not, by itself, give permission to republish a text. Open Alvary adds a text only after its rights and its content have been reviewed. This record is part-way there:"}
        </p>
        {linkOnly ? null : (
          <ol className="text-steps">
            {steps.map((step) => (
              <li key={step.title} className={step.state}>
                <span className="text-step-dot" aria-hidden="true">
                  {step.state === "done" ? <Check size={13} /> : null}
                </span>
                <b>{step.title}</b>
                <small>
                  {step.detail}
                  {step.tag && step.state === "now" ? (
                    <>
                      {" "}
                      <em>{step.tag}</em>
                    </>
                  ) : null}
                  <span className="visually-hidden">
                    {step.state === "done"
                      ? " (done)"
                      : step.state === "now"
                        ? " (in progress)"
                        : " (not yet)"}
                  </span>
                </small>
              </li>
            ))}
          </ol>
        )}
        <div className="text-status-foot">
          <a
            className="button primary"
            href={s.canonical_url}
            target="_blank"
            rel="noreferrer"
          >
            Read it at the official source{" "}
            <ArrowUpRight size={16} aria-hidden="true" />
          </a>
          <Link className="text-link" to="/governance">
            How we decide <ArrowRight size={15} aria-hidden="true" />
          </Link>
        </div>
      </div>
    </section>
  );
}

type Entry = {
  key: string;
  date: string;
  sortable: string;
  what: React.ReactNode;
  detail?: React.ReactNode;
};

function linkEntry(item: Related): Entry {
  return {
    key: (item.assertion_id ?? "") + item.kind + item.source_id,
    date: item.effective_date ? day(item.effective_date) : "",
    sortable: item.effective_date ?? "",
    what: (
      <>
        {KIND_LABEL[item.kind] ?? item.kind}{" "}
        <Link to={record(item.source_id)}>{item.title}</Link>
      </>
    ),
    detail: (
      <>
        {LEGAL_STATUS[item.legal_status] && !item.kind.endsWith("_by")
          ? `${LEGAL_STATUS[item.legal_status]} · `
          : ""}
        <a href={item.evidence_url} target="_blank" rel="noreferrer">
          checked against the official text
        </a>
      </>
    ),
  };
}

// One timeline: the record's checked links to other records and its public changes, newest first.
export function RecordTimeline({
  source: s,
  history,
}: {
  source: Source;
  history?: History;
}) {
  const recent = useSite<{ events: ChangeEvent[] }>(
    "/changes/recent?work=" + encodeURIComponent(s.id),
  );
  const events = recent.data?.events ?? [];
  const entries: Entry[] = [
    ...(history?.items ?? []).map(linkEntry),
    ...events.map((e) => ({
      key: e.event_id,
      date: new Date(e.committed_at).toLocaleDateString("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      }),
      sortable: e.committed_at.slice(0, 10),
      what: (
        <span className="timeline-change">
          {LABEL[e.event_type] ?? e.event_type}
        </span>
      ),
      detail: e.summary,
    })),
  ].sort((a, b) => (b.sortable || "0").localeCompare(a.sortable || "0"));
  const made = history?.enables ?? [];
  return (
    <section className="record-timeline" aria-labelledby="record-timeline">
      <h2 id="record-timeline">History</h2>
      <p className="timeline-status">
        {statusLabel(s)}
        {history?.current ? (
          <>
            . In force in its place now:{" "}
            <Link to={record(history.current.source_id)}>
              {history.current.title}
            </Link>
          </>
        ) : null}
        .
      </p>
      <ol className="timeline">
        {entries.map((entry) => (
          <li key={entry.key}>
            <span className="timeline-when">
              {entry.date || "Date not recorded"}
            </span>
            <span className="timeline-what">{entry.what}</span>
            {entry.detail ? (
              <span className="timeline-detail">{entry.detail}</span>
            ) : null}
          </li>
        ))}
        <li className="open">
          <span className="timeline-when">
            {recent.data ? "Since then" : "Loading changes…"}
          </span>
          <span className="timeline-what muted">
            {entries.length
              ? "Later links appear once a reviewer checks them against the official text."
              : "No checked links to other records yet."}
          </span>
        </li>
      </ol>
      {history && history.total > history.items.length ? (
        <p className="timeline-more">
          and {history.total - history.items.length} more linked records
        </p>
      ) : null}
      {history?.enables_total ? (
        <details className="timeline-made">
          <summary>
            {history.enables_total} instrument
            {history.enables_total === 1 ? " is" : "s are"} made under it
          </summary>
          <ul>
            {made.map((item) => (
              <li key={item.source_id}>
                <Link to={record(item.source_id)}>{item.title}</Link>
              </li>
            ))}
          </ul>
          {history.enables_total > made.length ? (
            <p>and {history.enables_total - made.length} more</p>
          ) : null}
        </details>
      ) : null}
    </section>
  );
}
