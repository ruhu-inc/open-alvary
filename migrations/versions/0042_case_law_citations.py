"""Case law: a judgment's reviewed citations and judicial treatment (EDT-01, EDT-02, DAT-06).

corpus.citation holds the citations a reviewer confirmed in a judgment's published paragraphs: the citing
judgment and paragraph (its locator and printed pinpoint, and its place in document order), the cited work
and, when the pinpoint resolved, the cited part, the words as printed, the paragraph's words, the
evidence, how it was found (the rule set that proposed it, or by hand) and the structural review (who, in
what role, when). corpus.judicial_treatment holds how a confirmed citation's paragraph treats the judgment
it cites (follows, applies, distinguishes, doubts, overrules), only from a doctrinal review that states
the reviewer's legal qualification, with the passage that does it and the opinion it is in; at most one
reviewed treatment per citation. A proposal is never stored: every reviewed column is required. Published
rows are never edited or deleted, only withdrawn (operations.citations).

changes.treatment_state is the last announced form of each treatment, so changes.record.publish announces
a newly public treatment as treatment_recorded on the treating judgment (key: the treatment id) and an
overruling also on the overruled judgment (key: the id + ":treated"); a treatment no longer public is a
correction of each, marked withdrawn. The change log's checks list treatment_recorded already (0040).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

UUID4 = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
# operations.citations, as of this revision: how a treatment is named, the opinion it is in, and how a
# citation was found.
TREATMENTS = ("follows", "applies", "distinguishes", "doubts", "overrules")
OPINIONS = ("majority", "concurrence", "dissent", "plurality", "order", "unknown")
EXTRACTIONS = ("citations-1", "manual")
TABLES = """
CREATE TABLE {corpus}.citation (
    citation_id text PRIMARY KEY CHECK (citation_id ~ '^cit_{uuid}$'),
    citing_work text NOT NULL CHECK (char_length(citing_work) BETWEEN 1 AND 160),
    citing_locator text NOT NULL CHECK (char_length(citing_locator) BETWEEN 1 AND 300),
    citing_pinpoint text NOT NULL CHECK (char_length(citing_pinpoint) BETWEEN 1 AND 100),
    citing_order integer NOT NULL CHECK (citing_order >= 0),
    cited_work text NOT NULL CHECK (char_length(cited_work) BETWEEN 1 AND 160),
    cited_locator text CHECK (char_length(cited_locator) BETWEEN 1 AND 300),
    cited_pinpoint text CHECK (char_length(cited_pinpoint) BETWEEN 1 AND 300),
    cited_order integer CHECK (cited_order >= 0),
    printed text NOT NULL CHECK (char_length(printed) BETWEEN 1 AND 500),
    quote text NOT NULL CHECK (char_length(quote) BETWEEN 1 AND 2000),
    evidence_url text NOT NULL CHECK (evidence_url ~ '^https://' AND char_length(evidence_url) <= 2000),
    extraction text NOT NULL CHECK (extraction IN ({extractions})),
    review_state text NOT NULL CHECK (review_state IN ('reviewed', 'withdrawn')),
    reviewed_by text NOT NULL CHECK (char_length(reviewed_by) BETWEEN 1 AND 200),
    reviewer_role text NOT NULL CHECK (char_length(reviewer_role) BETWEEN 1 AND 300),
    reviewed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (citing_work <> cited_work),
    CHECK ((cited_locator IS NULL) = (cited_pinpoint IS NULL) AND (cited_locator IS NULL) = (cited_order IS NULL))
);
CREATE INDEX citation_citing ON {corpus}.citation (citing_work);
CREATE INDEX citation_cited ON {corpus}.citation (cited_work);
CREATE TABLE {corpus}.judicial_treatment (
    treatment_id text PRIMARY KEY CHECK (treatment_id ~ '^trt_{uuid}$'),
    citation_id text NOT NULL REFERENCES {corpus}.citation (citation_id),
    treatment text NOT NULL CHECK (treatment IN ({treatments})),
    opinion text NOT NULL CHECK (opinion IN ({opinions})),
    quote text NOT NULL CHECK (char_length(quote) BETWEEN 1 AND 2000),
    review_state text NOT NULL CHECK (review_state IN ('reviewed', 'withdrawn')),
    reviewed_by text NOT NULL CHECK (char_length(reviewed_by) BETWEEN 1 AND 200),
    reviewer_role text NOT NULL CHECK (char_length(reviewer_role) BETWEEN 1 AND 300),
    reviewer_qualification text NOT NULL CHECK (char_length(reviewer_qualification) BETWEEN 1 AND 300),
    reviewed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE UNIQUE INDEX judicial_treatment_reviewed ON {corpus}.judicial_treatment (citation_id)
    WHERE review_state = 'reviewed';
CREATE TABLE {changes}.treatment_state (
    state_key text PRIMARY KEY CHECK (char_length(state_key) BETWEEN 1 AND 200),
    work_id text NOT NULL CHECK (char_length(work_id) BETWEEN 1 AND 160),
    projection jsonb NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    projection_hash text NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{{64}}$'),
    event_id uuid,
    event_revision integer CHECK (event_revision >= 1),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX treatment_state_work ON {changes}.treatment_state (work_id);
"""
GRANTS = (
    ("release", "SELECT, INSERT, UPDATE", "corpus", "citation"),
    ("release", "SELECT, INSERT, UPDATE", "corpus", "judicial_treatment"),
    ("release", "SELECT, INSERT, UPDATE, DELETE", "changes", "treatment_state"),
    ("serving", "SELECT", "corpus", "citation"),
    ("serving", "SELECT", "corpus", "judicial_treatment"),
    ("serving", "SELECT", "changes", "treatment_state"),
    # The guard reads every corpus table (bundle v0003); granted here too, whatever the provisioning order.
    ("guard", "SELECT", "corpus", "citation"),
    ("guard", "SELECT", "corpus", "judicial_treatment"),
)


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    schemas = {key: quote(ns.schema(key)) for key in ("corpus", "changes")}
    return conn, ns, schemas, quote


def _listed(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, schemas, quote = names()
    op.execute(
        TABLES.format(
            uuid=UUID4,
            treatments=_listed(TREATMENTS),
            opinions=_listed(OPINIONS),
            extractions=_listed(EXTRACTIONS),
            **schemas,
        )
    )
    for capability, privileges, schema, table in GRANTS:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"GRANT {privileges} ON {schemas[schema]}.{table} TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, schemas, _ = names()
    events = ns.qualified(conn, "changes", "change_event")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {events} WHERE event_type = 'treatment_recorded')")):
        raise RuntimeError("Treatment events exist; a downgrade would break the public change log")
    # The state is derived from the catalogue and the events; the citations are in the review workbench.
    op.execute(f"DROP TABLE {schemas['changes']}.treatment_state")
    op.execute(f"DROP TABLE {schemas['corpus']}.judicial_treatment")
    op.execute(f"DROP TABLE {schemas['corpus']}.citation")
