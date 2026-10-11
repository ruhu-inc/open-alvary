// A document's details as the public reads them beside the text (standard DETAILS-1): identity,
// passage and assent, publication, status and related laws, how the text was checked, and where
// it came from. A detail that is not given says why ("Not stated in this copy"), never blank.

import type { ReactNode } from "react";
import type { Source } from "./site";

export type Absence = "not_in_source" | "not_applicable" | "unknown";

export type Relation = {
  kind:
    | "repeals"
    | "amends"
    | "amended_by"
    | "repealed_by"
    | "supersedes"
    | "superseded_by"
    | "cross_reference";
  target: string;
  target_id?: string;
  note?: string;
};

export type Details = {
  short_title?: string;
  long_title?: string;
  document_type?: string;
  jurisdiction?: string;
  number?: string;
  year?: string;
  citation?: string;
  enacting_body?: string;
  bill_title?: string;
  passed_senate?: string;
  passed_house?: string;
  authenticated_on?: string;
  authenticated_by?: string;
  assent_date?: string;
  assented_by?: string;
  commencement_date?: string;
  gazette_name?: string;
  gazette_number?: string;
  gazette_volume?: string;
  gazette_date?: string;
  gazette_place?: string;
  gazette_issue?: string;
  gazette_notice?: string;
  gazette_pages?: string;
  official_publisher?: string;
  text_form?: string;
  version_label?: string;
  consolidation?: string;
  summary?: string;
  subjects?: string[];
  legal_status?: string;
  status_checked_on?: string;
  status_note?: string;
  relations?: Relation[];
  language?: string;
  language_status?: string;
  official_source_url?: string;
};

export type DetailsRecord = {
  status: "suggested" | "draft" | "confirmed";
  revision: number;
  details: Details;
  absent: Partial<Record<keyof Details, Absence>>;
};

export type Provenance = {
  retrieved_from?: string;
  retrieved_at?: string;
  original_sha256?: string;
  bytes?: number;
  media_type?: string;
  pages: number;
  collection?: string;
  retrieval_note?: string;
  rights_basis?: string;
};

export type ReviewRecord = {
  readers: { primary: string; primary_version?: string; checks: string[] };
  pages: number;
  checked_pages: number;
  reviewers: string[];
  approval: {
    decision: string;
    by: string;
    at: string;
    current: boolean;
  } | null;
};

export const ABSENT: Record<Absence, string> = {
  not_in_source: "Not stated in this copy",
  not_applicable: "Not applicable",
  unknown: "Not yet known",
};

const LABELS: Record<string, string> = {
  act: "Act",
  bill: "Bill",
  regulation: "Regulation",
  order: "Order",
  judgment: "Judgment",
  gazette: "Gazette",
  treaty: "Treaty",
  other: "Document",
  gazette_supplement: "The Act as published in the gazette",
  authenticated_bill:
    "The Bill as passed and certified by the Clerk to the National Assembly, before gazetting",
  official_reprint: "Official reprint",
  not_consolidated:
    "Not consolidated: the text as enacted, without later amendments",
  consolidated: "Consolidated: later amendments incorporated",
  unknown: "Currentness not yet checked",
  current: "In force",
  historical: "Historical",
  repealed: "Repealed",
  superseded: "Superseded",
  original: "Original language",
  official_translation: "Official translation",
  unofficial_translation: "Unofficial translation",
  ordinary: "Ordinary issue",
  extraordinary: "Extraordinary issue",
  repeals: "Repeals",
  amends: "Amends",
  amended_by: "Amended by",
  repealed_by: "Repealed by",
  supersedes: "Supersedes",
  superseded_by: "Superseded by",
  cross_reference: "Refers to",
};

const LANGUAGES: Record<string, string> = {
  en: "English",
  fr: "French",
  pt: "Portuguese",
  ar: "Arabic",
  sw: "Swahili",
  ha: "Hausa",
  yo: "Yoruba",
  ig: "Igbo",
};

const COUNTRIES: Record<string, string> = { ng: "Nigeria" };

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

// "2023-06-08" → "8 June 2023", without time zones moving the day.
export function longDate(iso?: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? "");
  if (!match) return iso ?? "";
  return `${Number(match[3])} ${MONTHS[Number(match[2]) - 1]} ${match[1]}`;
}

export const label = (value?: string) =>
  value ? (LABELS[value] ?? value) : "";
export const languageName = (tag?: string) =>
  tag ? (LANGUAGES[tag.split("-")[0]] ?? tag) : "";
export const countryName = (code?: string) =>
  code ? (COUNTRIES[code] ?? code.toUpperCase()) : "";

function Missing({ why }: { why?: Absence }) {
  return <span className="doc-absent">{why ? ABSENT[why] : "Not given"}</span>;
}

// One detail: its value, or why it is not given.
function Row({
  term,
  value,
  why,
}: {
  term: string;
  value: ReactNode;
  why?: Absence;
}) {
  if (why === "not_applicable") return null;
  return (
    <>
      <dt>{term}</dt>
      <dd>{value ? value : <Missing why={why} />}</dd>
    </>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="info-panel doc-panel" aria-label={title}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

export function AboutPanel({
  record,
  source,
}: {
  record: DetailsRecord;
  source: Source;
}) {
  const d = record.details;
  const a = record.absent;
  return (
    <Panel title="About this document">
      <dl>
        <Row term="Short title" value={d.short_title} why={a.short_title} />
        <Row term="Citation" value={d.citation} why={a.citation} />
        <Row
          term="Type"
          value={label(d.document_type ?? source.document_type)}
        />
        <Row term="Number" value={d.number} why={a.number} />
        <Row term="Year" value={d.year} why={a.year} />
        <Row
          term="Jurisdiction"
          value={countryName(d.jurisdiction ?? source.jurisdiction_id)}
        />
        <Row term="Enacted by" value={d.enacting_body} why={a.enacting_body} />
        <Row
          term="Language"
          value={
            d.language
              ? `${languageName(d.language)}${d.language_status ? ` · ${label(d.language_status).toLowerCase()}` : ""}`
              : ""
          }
          why={a.language}
        />
        <Row term="Long title" value={d.long_title} why={a.long_title} />
        {d.subjects?.length ? (
          <Row term="Subjects" value={d.subjects.join(" · ")} />
        ) : null}
      </dl>
    </Panel>
  );
}

// The steps by which the Bill became law, in order, each dated or accounted for.
export function PassagePanel({ record }: { record: DetailsRecord }) {
  const d = record.details;
  const a = record.absent;
  const steps: [string, string | undefined, Absence | undefined, string?][] = [
    ["Passed by the Senate", d.passed_senate, a.passed_senate],
    ["Passed by the House of Representatives", d.passed_house, a.passed_house],
    [
      "Certified by the Clerk",
      d.authenticated_on,
      a.authenticated_on,
      d.authenticated_by,
    ],
    ["Assented to", d.assent_date, a.assent_date, d.assented_by],
    ["Commencement", d.commencement_date, a.commencement_date],
  ];
  if (steps.every(([, , why]) => why === "not_applicable")) return null;
  return (
    <Panel title="Passage and assent">
      {d.bill_title ? <p className="doc-panel-lead">{d.bill_title}</p> : null}
      <ol className="doc-steps">
        {steps
          .filter(([, , why]) => why !== "not_applicable")
          .map(([name, when, why, by]) => (
            <li key={name} className={when ? "" : "doc-step-missing"}>
              <span className="doc-step-name">{name}</span>
              <span className="doc-step-date">
                {when ? longDate(when) : <Missing why={why} />}
              </span>
              {by ? <span className="doc-step-by">{by}</span> : null}
            </li>
          ))}
      </ol>
    </Panel>
  );
}

export function PublicationPanel({ record }: { record: DetailsRecord }) {
  const d = record.details;
  const a = record.absent;
  if (!d.gazette_name && d.text_form === "authenticated_bill")
    return (
      <Panel title="Publication">
        <p className="doc-panel-note">
          This copy is the Bill as passed and certified, without the gazette
          front page: the gazette that published the Act, and the Act number it
          gave, are not stated in it.
        </p>
      </Panel>
    );
  const issue = [
    d.gazette_number ? `No. ${d.gazette_number}` : "",
    d.gazette_volume ? `Vol. ${d.gazette_volume}` : "",
  ]
    .filter(Boolean)
    .join(", ");
  return (
    <Panel title="Publication">
      <dl>
        <Row term="Gazette" value={d.gazette_name} why={a.gazette_name} />
        <Row term="Issue" value={issue} why={a.gazette_number} />
        <Row
          term="Date"
          value={longDate(d.gazette_date)}
          why={a.gazette_date}
        />
        <Row term="Place" value={d.gazette_place} why={a.gazette_place} />
        <Row
          term="Kind of issue"
          value={label(d.gazette_issue)}
          why={a.gazette_issue}
        />
        {d.gazette_notice ? (
          <Row term="Notice" value={d.gazette_notice} />
        ) : null}
        <Row term="Pages" value={d.gazette_pages} why={a.gazette_pages} />
        <Row
          term="Publisher"
          value={d.official_publisher}
          why={a.official_publisher}
        />
      </dl>
    </Panel>
  );
}

export function StatusPanel({ record }: { record: DetailsRecord }) {
  const d = record.details;
  const a = record.absent;
  return (
    <Panel title="Status and related laws">
      <dl>
        <Row
          term="Legal status"
          value={label(d.legal_status ?? "unknown")}
          why={a.legal_status}
        />
        <Row
          term="Status checked"
          value={longDate(d.status_checked_on)}
          why={a.status_checked_on}
        />
        {d.status_note ? <Row term="Note" value={d.status_note} /> : null}
        <Row
          term="Version"
          value={[d.version_label, label(d.consolidation)]
            .filter(Boolean)
            .join(". ")}
          why={a.version_label}
        />
      </dl>
      {d.relations?.length ? (
        <ul className="doc-relations">
          {d.relations.map((link, i) => (
            <li key={i}>
              <span className="doc-relation-kind">{label(link.kind)}</span>{" "}
              {link.target_id ? (
                <a href={"#/sources/" + link.target_id}>{link.target}</a>
              ) : (
                link.target
              )}
              {link.note ? (
                <span className="doc-relation-note"> · {link.note}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="doc-panel-note">No related laws are recorded.</p>
      )}
    </Panel>
  );
}

const short = (hash?: string) => (hash ? hash.slice(0, 16) + "…" : "");

function host(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

// How the text the reader sees was produced and checked.
export function ReviewPanel({
  review,
  record,
  contentHash,
  detailsHash,
}: {
  review: ReviewRecord;
  record: DetailsRecord;
  contentHash?: string;
  detailsHash?: string | null;
}) {
  const approval = review.approval;
  return (
    <Panel title="How this text was checked">
      <ol className="doc-checks">
        <li>
          Read by machine with {review.readers.primary}
          {review.readers.primary_version
            ? ` (${review.readers.primary_version})`
            : ""}
          {review.readers.checks.length
            ? `, and compared word by word with ${review.readers.checks.join(" and ")}; every difference was flagged.`
            : "."}
        </li>
        <li>
          {review.checked_pages === review.pages
            ? `Every page (${review.pages}) checked against the printed page image`
            : `${review.checked_pages} of ${review.pages} pages checked against the printed page image`}
          {review.reviewers.length
            ? ` by ${review.reviewers.join(", ")}.`
            : "."}
        </li>
        <li>
          {record.status === "confirmed"
            ? "The details on this page were confirmed against the document."
            : "The details on this page are not yet confirmed."}
        </li>
        <li>
          {approval && approval.decision === "approved" && approval.current
            ? `Approved for publication by ${approval.by} on ${longDate(approval.at)}.`
            : "Not yet approved for publication."}
        </li>
      </ol>
      <dl>
        {contentHash ? (
          <Row
            term="Text fingerprint (SHA-256)"
            value={<code title={contentHash}>{short(contentHash)}</code>}
          />
        ) : null}
        {detailsHash ? (
          <Row
            term="Details fingerprint (SHA-256)"
            value={<code title={detailsHash}>{short(detailsHash)}</code>}
          />
        ) : null}
      </dl>
    </Panel>
  );
}

export function SourcePanel({
  record,
  provenance,
}: {
  record: DetailsRecord;
  provenance: Provenance;
}) {
  const d = record.details;
  const a = record.absent;
  return (
    <Panel title="Source of this text">
      <dl>
        <Row
          term="Form of this text"
          value={label(d.text_form)}
          why={a.text_form}
        />
        <Row
          term="Official source online"
          value={
            d.official_source_url ? (
              <a href={d.official_source_url} target="_blank" rel="noreferrer">
                {host(d.official_source_url)}
              </a>
            ) : (
              ""
            )
          }
          why={a.official_source_url}
        />
        <Row
          term="Retrieved from"
          value={
            provenance.retrieved_from ? (
              <a
                href={provenance.retrieved_from}
                target="_blank"
                rel="noreferrer"
              >
                {host(provenance.retrieved_from)}
              </a>
            ) : (
              ""
            )
          }
        />
        <Row term="Retrieved on" value={longDate(provenance.retrieved_at)} />
        <Row
          term="Original file"
          value={[
            provenance.media_type === "application/pdf" ? "PDF" : "",
            `${provenance.pages} pages`,
            provenance.bytes ? `${Math.round(provenance.bytes / 1024)} KB` : "",
          ]
            .filter(Boolean)
            .join(", ")}
        />
        {provenance.original_sha256 ? (
          <Row
            term="Original file fingerprint (SHA-256)"
            value={
              <code title={provenance.original_sha256}>
                {short(provenance.original_sha256)}
              </code>
            }
          />
        ) : null}
      </dl>
      {provenance.retrieval_note ? (
        <p className="doc-panel-note">{provenance.retrieval_note}</p>
      ) : null}
      {provenance.collection ? (
        <p className="doc-panel-note">Collection: {provenance.collection}</p>
      ) : null}
    </Panel>
  );
}

// The facts a reader needs first, under the title.
export function KeyFacts({
  record,
  approved,
}: {
  record: DetailsRecord;
  approved: boolean;
}) {
  const d = record.details;
  const facts = [
    d.version_label,
    d.consolidation === "not_consolidated" ? "Not consolidated" : "",
    d.legal_status && d.legal_status !== "unknown"
      ? label(d.legal_status)
      : "Currentness not yet checked",
    d.assent_date ? `Assented to ${longDate(d.assent_date)}` : "",
    d.commencement_date
      ? `In effect from ${longDate(d.commencement_date)}`
      : "",
    d.language ? languageName(d.language) : "",
    approved ? "Checked page by page and approved" : "",
  ].filter(Boolean);
  return (
    <ul className="doc-facts" aria-label="Key facts">
      {facts.map((fact) => (
        <li key={fact}>{fact}</li>
      ))}
    </ul>
  );
}

// The citation a reader copies, for the whole document or one section.
export function citationFor(
  record: DetailsRecord,
  source: Source,
  section?: string,
) {
  const base =
    record.details.citation || record.details.short_title || source.title;
  return section ? `${base}, s. ${section}` : base;
}

// schema.org Legislation, for search engines: what the public page will carry in its head.
export function legislationJsonLd(
  record: DetailsRecord,
  source: Source,
  url: string,
  downloads: { format: string; href: string }[],
) {
  const d = record.details;
  const force =
    d.legal_status === "current"
      ? "InForce"
      : d.legal_status === "repealed" || d.legal_status === "superseded"
        ? "NotInForce"
        : undefined;
  const changes = (d.relations ?? []).filter((r) =>
    ["amends", "repeals"].includes(r.kind),
  );
  const data: Record<string, unknown> = {
    "@context": "https://schema.org",
    "@type": "Legislation",
    name: d.short_title || source.title,
    alternateName: d.citation,
    legislationIdentifier: d.citation,
    legislationType: label(d.document_type ?? source.document_type),
    legislationJurisdiction: {
      "@type": "Country",
      name: countryName(d.jurisdiction ?? source.jurisdiction_id),
    },
    legislationDate: d.assent_date,
    legislationDateVersion: d.commencement_date ?? d.assent_date,
    legislationLegalForce: force && `https://schema.org/${force}`,
    legislationPassedBy: d.enacting_body && {
      "@type": "Organization",
      name: d.enacting_body,
    },
    legislationChanges: changes.length
      ? changes.map((r) => ({ "@type": "Legislation", name: r.target }))
      : undefined,
    inLanguage: d.language,
    abstract: d.summary,
    description: d.long_title,
    datePublished: d.gazette_date,
    publisher: d.official_publisher && {
      "@type": "Organization",
      name: d.official_publisher,
    },
    keywords: d.subjects?.length ? d.subjects.join(", ") : undefined,
    url,
    encoding: downloads.map((download) => ({
      "@type": "MediaObject",
      encodingFormat: download.format,
      contentUrl: download.href,
    })),
  };
  return JSON.parse(JSON.stringify(data)); // drops what is not given
}
