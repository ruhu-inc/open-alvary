// A document's details as the public reads them beside the text (document-record-1). The panels
// follow the document's family and its jurisdiction: an Act shows how it was made step by step, a
// judgment shows its court, parties and bench, a treaty its parties and ratifications. A detail that
// is not given says why ("Not stated in this copy"), never blank.

import type { ReactNode } from "react";
import type { Source } from "./site";

export type Absence =
  "not_in_source" | "not_applicable" | "unknown" | "withheld";
type Choice = { value: string; label: string };

export type FieldInfo = {
  key: string;
  label: string;
  group: string;
  kind: string;
  choices: Choice[];
  columns: { key: string; label: string; kind: string; choices: Choice[] }[];
};

export type View = {
  family: string;
  kind: string;
  place: { code: string; name: string; level: string };
  groups: { key: string; label: string }[];
  fields: FieldInfo[];
  values: Record<string, unknown>;
  absent: Record<string, Absence>;
};

export type Step = {
  step: string;
  label: string;
  event: string;
  signed_by?: string | null;
};

export type DetailsStatus = {
  status: "suggested" | "draft" | "confirmed";
  revision: number;
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
  readers: {
    primary: string;
    primary_version?: string | null;
    checks: string[];
    notes?: string[];
  };
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
  withheld: "Withheld",
};

const LANGUAGES: Record<string, string> = {
  en: "English",
  fr: "French",
  rw: "Kinyarwanda",
  sw: "Swahili",
  pt: "Portuguese",
  ar: "Arabic",
  am: "Amharic",
  ha: "Hausa",
  yo: "Yoruba",
  ig: "Igbo",
  so: "Somali",
  zu: "Zulu",
  xh: "Xhosa",
  af: "Afrikaans",
};

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

export const languageName = (tag?: string) =>
  tag ? (LANGUAGES[tag.split("-")[0]] ?? tag) : "";
export const humanise = (value?: string) =>
  value ? value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "";

const text = (view: View, key: string) => {
  const value = view.values[key];
  return typeof value === "string" ? value : value == null ? "" : String(value);
};
const list = <T,>(view: View, key: string) =>
  (Array.isArray(view.values[key]) ? view.values[key] : []) as T[];
const given = (view: View, key: string) => {
  const value = view.values[key];
  return (
    value != null && value !== "" && !(Array.isArray(value) && !value.length)
  );
};

function choiceLabel(field: FieldInfo | undefined, value: string) {
  return (
    field?.choices.find((c) => c.value === value)?.label ?? humanise(value)
  );
}

function Missing({ why }: { why: Absence }) {
  return <span className="doc-absent">{ABSENT[why]}</span>;
}

// A value as a reader should see it, by the kind of field it is.
function Value({ field, value }: { field: FieldInfo; value: unknown }) {
  if (field.kind === "date") return <>{longDate(String(value))}</>;
  if (field.kind === "choice") return <>{choiceLabel(field, String(value))}</>;
  if (field.kind === "list") return <>{(value as string[]).join(" · ")}</>;
  if (field.kind === "rows") {
    const rows = value as Record<string, string>[];
    return (
      <ul className="doc-rows">
        {rows.map((row, i) => (
          <li key={i}>
            {field.columns
              .filter((c) => row[c.key])
              .map((c) =>
                c.choices.length
                  ? (c.choices.find((x) => x.value === row[c.key])?.label ??
                    row[c.key])
                  : c.kind === "date"
                    ? longDate(row[c.key])
                    : row[c.key],
              )
              .join(" · ")}
          </li>
        ))}
      </ul>
    );
  }
  return <>{String(value)}</>;
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="info-panel doc-panel" aria-label={title}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

// The given or accounted-for details among these fields, as term and value.
function Details({ view, fields }: { view: View; fields: FieldInfo[] }) {
  const shown = fields.filter(
    (f) =>
      (given(view, f.key) || view.absent[f.key]) &&
      view.absent[f.key] !== "not_applicable",
  );
  if (!shown.length) return null;
  return (
    <dl>
      {shown.map((f) => (
        <div className="doc-term" key={f.key}>
          <dt>{f.label}</dt>
          <dd>
            {view.absent[f.key] ? (
              <Missing why={view.absent[f.key]} />
            ) : (
              <Value field={f} value={view.values[f.key]} />
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// Fields internal to the record, or shown elsewhere on the page.
const HIDDEN = new Set([
  "identity.family",
  "identity.local_subtype",
  "text.summary.text",
  "text.summary.kind",
  "text.summary.official",
  "publication.kind",
  "publication.url",
  "expressions",
  "relations",
]);

// How the document was made: the jurisdiction's steps in order, each dated or accounted for.
function Making({ view, making }: { view: View; making: Step[] }) {
  const events = view.fields.filter(
    (f) => f.group === "making" && f.key.startsWith("event:"),
  );
  const bill = text(view, "profile.bill");
  if (!making.length && !events.length) return null;
  const rows = [
    ...making.map((step) => ({
      key: step.step,
      label: step.label,
      when: text(view, `step:${step.step}.date`),
      why: view.absent[`step:${step.step}.date`],
      by: text(view, `step:${step.step}.person`),
    })),
    ...events.map((field) => ({
      key: field.key,
      label: field.label,
      when: text(view, field.key),
      why: view.absent[field.key],
      by: "",
    })),
  ].filter((row) => row.why !== "not_applicable");
  return (
    <Panel
      title={view.family === "judgment" ? "Proceedings" : "How it was made"}
    >
      {bill ? <p className="doc-panel-lead">{bill}</p> : null}
      <ol className="doc-steps">
        {rows.map((row) => (
          <li key={row.key} className={row.when ? "" : "doc-step-missing"}>
            <span className="doc-step-name">{row.label}</span>
            <span className="doc-step-date">
              {row.when ? (
                longDate(row.when)
              ) : (
                <Missing why={row.why ?? "unknown"} />
              )}
            </span>
            {row.by ? <span className="doc-step-by">{row.by}</span> : null}
          </li>
        ))}
      </ol>
    </Panel>
  );
}

const AUTHORITY: Record<string, string> = {
  authoritative: "authoritative",
  equally_authentic: "equally authentic",
  official_translation: "official translation",
  unofficial_translation: "unofficial translation",
  machine_translation: "machine translation",
};

function Languages({ view }: { view: View }) {
  const versions = list<Record<string, string>>(view, "expressions");
  if (!versions.length) return null;
  return (
    <Panel title="Language versions">
      <ul className="doc-relations">
        {versions.map((v, i) => (
          <li key={i}>
            <span className="doc-relation-kind">
              {languageName(v.language)}
            </span>{" "}
            {AUTHORITY[v.authority] ?? v.authority}
            {v.this_text === "yes" ? (
              <span className="doc-relation-note">
                {" "}
                · the text on this page
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function Status({ view }: { view: View }) {
  const fields = view.fields.filter(
    (f) =>
      (f.group === "status" && !HIDDEN.has(f.key)) ||
      f.key === "text.version.label" ||
      f.key === "text.version.consolidated",
  );
  const links = list<{
    kind: string;
    target: string;
    target_id?: string;
    note?: string;
  }>(view, "relations");
  return (
    <Panel title="Status and related documents">
      <Details view={view} fields={fields} />
      {links.length ? (
        <ul className="doc-relations">
          {links.map((link, i) => (
            <li key={i}>
              <span className="doc-relation-kind">{humanise(link.kind)}</span>{" "}
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
        <p className="doc-panel-note">No related documents are recorded.</p>
      )}
    </Panel>
  );
}

// Every detail panel for this document, in the order its family calls for.
export function DetailsPanels({
  view,
  making,
}: {
  view: View;
  making: Step[];
}) {
  const inGroup = (group: string) =>
    view.fields.filter((f) => f.group === group && !HIDDEN.has(f.key));
  const groupLabel = (group: string) =>
    view.groups.find((g) => g.key === group)?.label || humanise(group);
  const certified =
    text(view, "text.form") === "certified_copy" &&
    !text(view, "publication.name");
  const profile = inGroup("profile").filter((f) => f.key !== "profile.bill");
  return (
    <>
      <Panel title="About this document">
        <Details
          view={view}
          fields={[
            ...inGroup("identity"),
            ...view.fields.filter((f) => f.key === "text.form"),
          ]}
        />
      </Panel>
      <Making view={view} making={making} />
      {view.family !== "judgment" ? (
        <Panel title="Publication">
          {certified ? (
            <p className="doc-panel-note">
              This copy is the text as passed and certified, without the
              gazette's front page: the gazette that published it, and the
              number it gave, are not stated in it.
            </p>
          ) : (
            <Details view={view} fields={inGroup("publication")} />
          )}
        </Panel>
      ) : null}
      <Status view={view} />
      {profile.some(
        (f) =>
          given(view, f.key) ||
          (view.absent[f.key] && view.absent[f.key] !== "not_applicable"),
      ) ? (
        <Panel title={groupLabel("profile")}>
          <Details view={view} fields={profile} />
        </Panel>
      ) : null}
      <Languages view={view} />
      {inGroup("classification").some((f) => given(view, f.key)) ? (
        <Panel title="Classification">
          <Details view={view} fields={inGroup("classification")} />
        </Panel>
      ) : null}
    </>
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

function Term({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="doc-term">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

// How the text the reader sees was produced and checked.
export function ReviewPanel({
  review,
  details,
  contentHash,
  detailsHash,
}: {
  review: ReviewRecord;
  details: DetailsStatus;
  contentHash?: string;
  detailsHash?: string | null;
}) {
  const approval = review.approval;
  return (
    <Panel title="How this text was checked">
      <ol className="doc-checks">
        {review.readers.notes?.length ? (
          review.readers.notes.map((note) => <li key={note}>{note}</li>)
        ) : (
          <li>
            Read by machine with {review.readers.primary}
            {review.readers.primary_version
              ? ` (${review.readers.primary_version})`
              : ""}
            {review.readers.checks.length
              ? `, and compared word by word with ${review.readers.checks.join(" and ")}; every difference was flagged.`
              : "."}
          </li>
        )}
        <li>
          {review.checked_pages === review.pages
            ? `Every page (${review.pages}) checked against the printed page image`
            : `${review.checked_pages} of ${review.pages} pages checked against the printed page image`}
          {review.reviewers.length
            ? ` by ${review.reviewers.join(", ")}.`
            : "."}
        </li>
        <li>
          {details.status === "confirmed"
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
          <Term term="Text fingerprint (SHA-256)">
            <code title={contentHash}>{short(contentHash)}</code>
          </Term>
        ) : null}
        {detailsHash ? (
          <Term term="Details fingerprint (SHA-256)">
            <code title={detailsHash}>{short(detailsHash)}</code>
          </Term>
        ) : null}
      </dl>
    </Panel>
  );
}

export function SourcePanel({
  view,
  provenance,
}: {
  view: View;
  provenance: Provenance;
}) {
  const official = text(view, "publication.url");
  return (
    <Panel title="Source of this text">
      <dl>
        {official ? (
          <Term term="Official copy online">
            <a href={official} target="_blank" rel="noreferrer">
              {host(official)}
            </a>
          </Term>
        ) : null}
        {provenance.retrieved_from ? (
          <Term term="Retrieved from">
            <a
              href={provenance.retrieved_from}
              target="_blank"
              rel="noreferrer"
            >
              {host(provenance.retrieved_from)}
            </a>
          </Term>
        ) : null}
        {provenance.retrieved_at ? (
          <Term term="Retrieved on">{longDate(provenance.retrieved_at)}</Term>
        ) : null}
        <Term term="Original file">
          {[
            provenance.media_type === "application/pdf" ? "PDF" : "",
            `${provenance.pages} pages`,
            provenance.bytes ? `${Math.round(provenance.bytes / 1024)} KB` : "",
          ]
            .filter(Boolean)
            .join(", ")}
        </Term>
        {provenance.original_sha256 ? (
          <Term term="Original file fingerprint (SHA-256)">
            <code title={provenance.original_sha256}>
              {short(provenance.original_sha256)}
            </code>
          </Term>
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

// When the document was made (assent, promulgation, signature), commenced or decided.
function milestones(view: View, making: Step[]) {
  const date = (step?: Step) =>
    step ? text(view, `step:${step.step}.date`) : "";
  const made = making.find(
    (s) =>
      ["assented", "promulgated", "signed", "made"].includes(s.event) &&
      date(s),
  );
  const commenced = making.find((s) => s.event === "commenced" && date(s));
  return {
    made: made ? { label: made.label, date: date(made) } : null,
    commenced: date(commenced) || text(view, "event:commenced.date"),
    decided:
      text(view, "event:decided.date") || text(view, "event:delivered.date"),
  };
}

// The facts a reader needs first, under the title.
export function KeyFacts({
  view,
  making,
  approved,
}: {
  view: View;
  making: Step[];
  approved: boolean;
}) {
  const { made, commenced, decided } = milestones(view, making);
  const status = text(view, "status.legal");
  const consolidated = text(view, "text.version.consolidated");
  const language = list<Record<string, string>>(view, "expressions").find(
    (e) => e.this_text === "yes",
  );
  const facts = [
    text(view, "text.version.label"),
    consolidated === "no"
      ? "Not consolidated"
      : consolidated === "yes"
        ? "Consolidated"
        : "",
    status && status !== "unknown"
      ? humanise(status)
      : "Currentness not yet checked",
    made ? `${made.label.replace(/ by .*$/, "")} ${longDate(made.date)}` : "",
    decided ? `Decided ${longDate(decided)}` : "",
    commenced ? `In effect from ${longDate(commenced)}` : "",
    language ? languageName(language.language) : "",
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

export const titleOf = (view: View, source: Source) =>
  text(view, "identity.title") || source.title;
export const summaryOf = (view: View) => text(view, "text.summary.text");
export const kindOf = (view: View) => humanise(view.kind);

// The citation a reader copies, for the whole document or one section.
export function citationFor(view: View, source: Source, section?: string) {
  const base = text(view, "identity.citation") || titleOf(view, source);
  return section ? `${base}, s. ${section}` : base;
}

// schema.org for search engines: what the public page will carry in its head.
export function legislationJsonLd(
  view: View,
  making: Step[],
  source: Source,
  url: string,
  downloads: { format: string; href: string }[],
) {
  const { made, commenced } = milestones(view, making);
  const status = text(view, "status.legal");
  const force =
    status === "in_force"
      ? "InForce"
      : status === "partly_in_force"
        ? "PartiallyInForce"
        : ["repealed", "superseded", "expired", "struck_down"].includes(status)
          ? "NotInForce"
          : undefined;
  const changes = list<{ kind: string; target: string }>(
    view,
    "relations",
  ).filter((r) => ["amends", "repeals"].includes(r.kind));
  const language = list<Record<string, string>>(view, "expressions").find(
    (e) => e.this_text === "yes",
  );
  const judgment = view.family === "judgment";
  const data: Record<string, unknown> = {
    "@context": "https://schema.org",
    "@type": judgment ? "CreativeWork" : "Legislation",
    additionalType: judgment ? "Judgment" : undefined,
    name: titleOf(view, source),
    alternateName: text(view, "identity.citation") || undefined,
    legislationIdentifier: judgment
      ? undefined
      : text(view, "identity.citation") || undefined,
    legislationType: judgment ? undefined : kindOf(view),
    legislationJurisdiction: judgment
      ? undefined
      : { "@type": "Country", name: view.place.name },
    legislationDate: made?.date,
    legislationDateVersion: commenced || made?.date,
    legislationLegalForce: force && `https://schema.org/${force}`,
    legislationPassedBy: text(view, "issued_by")
      ? { "@type": "Organization", name: text(view, "issued_by") }
      : undefined,
    legislationChanges: changes.length
      ? changes.map((r) => ({ "@type": "Legislation", name: r.target }))
      : undefined,
    inLanguage: language?.language,
    abstract: summaryOf(view) || undefined,
    description: text(view, "identity.full_title") || undefined,
    datePublished: text(view, "publication.date") || undefined,
    publisher: text(view, "publication.publisher")
      ? { "@type": "Organization", name: text(view, "publication.publisher") }
      : undefined,
    keywords:
      list<string>(view, "classification.subjects").join(", ") || undefined,
    url,
    encoding: downloads.map((download) => ({
      "@type": "MediaObject",
      encodingFormat: download.format,
      contentUrl: download.href,
    })),
  };
  return JSON.parse(JSON.stringify(data)); // drops what is not given
}
