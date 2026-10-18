// A record's place in the law, beside the record: whether it is current and, when it has been
// repealed or replaced, the law in force in its place now; the records it repeals, amends or is made
// under; those that repealed or amended it; and the instruments made under it. Only links between
// public records that a reviewer has checked against the official text (GET /site/v1/sources/{id}/relationships).
import { ArrowRight, ExternalLink } from "lucide-react";
import { LEGAL_STATUS, Link } from "./site";

export type Related = {
  kind: string;
  source_id: string;
  title: string;
  citation: string;
  legal_status: string;
  year?: number | null;
  effective_date?: string | null;
  evidence_url: string;
  verified: boolean;
};
export type History = {
  source_id: string;
  legal_status: string;
  current: { source_id: string; title: string; citation: string } | null;
  total: number;
  items: Related[];
  enables: Related[];
  enables_total: number;
};

// How the other record relates to this one, in the order a reader asks: what replaced it first.
const KINDS: [string, string][] = [
  ["repealed_by", "Repealed by"],
  ["superseded_by", "Replaced by"],
  ["amended_by", "Amended by"],
  ["made_under", "Made under"],
  ["repeals", "Repeals"],
  ["supersedes", "Replaces"],
  ["amends", "Amends"],
  ["cross_reference", "See also"],
];

const record = (id: string) => "/sources/" + encodeURIComponent(id);

function day(value: string) {
  return new Date(value + "T00:00:00Z").toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

function Item({ item }: { item: Related }) {
  return (
    <li>
      <Link to={record(item.source_id)}>{item.title}</Link>
      <span className="history-meta">
        {item.effective_date ? ` · from ${day(item.effective_date)}` : ""}
        {item.kind !== "repealed_by" && item.kind !== "superseded_by"
          ? ` · ${LEGAL_STATUS[item.legal_status] ?? item.legal_status}`
          : ""}
        {" · "}
        <a href={item.evidence_url} target="_blank" rel="noreferrer">
          official text <ExternalLink size={12} aria-hidden="true" />
        </a>
      </span>
    </li>
  );
}

// Under the title of a repealed or replaced record: the law to read instead.
export function CurrentLaw({ history }: { history?: History }) {
  if (!history?.current) return null;
  const replaced =
    history.legal_status === "superseded" ? "Replaced" : "Repealed";
  return (
    <div className="current-law" role="note">
      <strong>{replaced}.</strong> The law in force in its place now:{" "}
      <Link to={record(history.current.source_id)}>
        {history.current.title} <ArrowRight size={14} aria-hidden="true" />
      </Link>
    </div>
  );
}

export function RecordHistory({ history }: { history?: History }) {
  if (!history) return null;
  const groups = KINDS.map(
    ([kind, label]) =>
      [label, history.items.filter((r) => r.kind === kind)] as const,
  ).filter(([, items]) => items.length);
  const more = history.enables_total - history.enables.length;
  return (
    <section
      className="info-panel record-history"
      aria-labelledby="record-history"
    >
      <h3 id="record-history">History</h3>
      <p>
        {LEGAL_STATUS[history.legal_status] ?? history.legal_status}
        {history.current ? (
          <>
            . In force in its place now:{" "}
            <Link to={record(history.current.source_id)}>
              {history.current.title}
            </Link>
          </>
        ) : null}
        .
      </p>
      {groups.map(([label, items]) => (
        <div key={label}>
          <h4>{label}</h4>
          <ul>
            {items.map((item) => (
              <Item key={item.kind + item.source_id} item={item} />
            ))}
          </ul>
        </div>
      ))}
      {history.total > history.items.length ? (
        <p>and {history.total - history.items.length} more</p>
      ) : null}
      {history.enables_total ? (
        <details>
          <summary>
            {history.enables_total} instrument
            {history.enables_total === 1 ? " is" : "s are"} made under it
          </summary>
          <ul>
            {history.enables.map((item) => (
              <Item key={item.source_id} item={item} />
            ))}
          </ul>
          {more > 0 ? <p>and {more} more</p> : null}
        </details>
      ) : null}
      {!groups.length && !history.enables_total ? (
        <p className="history-none">No checked links to other records yet.</p>
      ) : null}
    </section>
  );
}
