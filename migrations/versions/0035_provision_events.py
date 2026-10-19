"""Provision-level legal time (CPR-02, IDT-03): reviewed legal events and currentness rule profiles.

corpus.legal_event holds reviewed events (commenced, repealed, suspended, …) affecting a whole work or
named provisions (by locator), each with its date and precision, conditions, evidence and review
state; a correction supersedes an event, never edits its history. corpus.currentness_profile holds a
jurisdiction's reviewed rules for a document class (what commencement is when an instrument is
silent). changes.event_state is the last announced form of each event, so changes.record.publish
announces a newly public event as legal_effect_recorded and a changed one as its correction.
operations.events publishes them from a reviewed file.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

EVENT_TYPES = (
    "adopted", "assented", "published", "commenced", "amended", "repealed", "suspended", "revived", "expired",
    "corrected", "consolidated", "declared_invalid", "disapplied", "entered_into_force",
)  # fmt: skip

TABLES = """
CREATE TABLE {corpus}.legal_event (
    event_id text PRIMARY KEY CHECK (event_id ~ '^evt_[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'),
    event_type text NOT NULL CHECK (event_type IN ({types})),
    affected_work text NOT NULL CHECK (char_length(affected_work) BETWEEN 1 AND 160),
    asserting_work text CHECK (char_length(asserting_work) BETWEEN 1 AND 160),
    scope text NOT NULL CHECK (scope IN ('whole', 'explicit_nodes')),
    locators text[] NOT NULL DEFAULT '{{}}',
    date_from date,
    date_to date,
    precision text NOT NULL CHECK (precision IN ('day', 'month', 'year', 'unknown')),
    conditions text CHECK (char_length(conditions) <= 2000),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    review_state text NOT NULL CHECK (review_state IN ('reviewed', 'withdrawn')),
    reviewed_by text NOT NULL CHECK (char_length(reviewed_by) BETWEEN 1 AND 200),
    reviewed_at timestamptz NOT NULL,
    supersedes_event_id text REFERENCES {corpus}.legal_event (event_id),
    recorded_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (scope = 'whole' OR cardinality(locators) > 0),
    CHECK (date_to IS NULL OR date_from IS NULL OR date_from <= date_to)
);
CREATE INDEX legal_event_work ON {corpus}.legal_event (affected_work);
CREATE TABLE {corpus}.currentness_profile (
    profile_id text PRIMARY KEY CHECK (char_length(profile_id) BETWEEN 1 AND 160),
    jurisdiction_id text NOT NULL CHECK (char_length(jurisdiction_id) BETWEEN 1 AND 160),
    document_type text NOT NULL CHECK (char_length(document_type) BETWEEN 1 AND 40),
    silent_commencement text NOT NULL CHECK (silent_commencement IN ('assent', 'unknown')),
    basis text NOT NULL CHECK (char_length(basis) BETWEEN 1 AND 2000),
    reviewed_by text NOT NULL CHECK (char_length(reviewed_by) BETWEEN 1 AND 200),
    reviewed_at timestamptz NOT NULL,
    UNIQUE (jurisdiction_id, document_type)
);
CREATE TABLE {changes}.event_state (
    event_id text PRIMARY KEY CHECK (char_length(event_id) BETWEEN 1 AND 160),
    work_id text NOT NULL CHECK (char_length(work_id) BETWEEN 1 AND 160),
    projection jsonb NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    projection_hash text NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{{64}}$'),
    change_event_id uuid,
    change_event_revision integer CHECK (change_event_revision >= 1),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX event_state_work ON {changes}.event_state (work_id);
"""
GRANTS = (
    ("release", "SELECT, INSERT, UPDATE", "corpus", "legal_event"),
    ("release", "SELECT, INSERT, UPDATE", "corpus", "currentness_profile"),
    ("release", "SELECT, INSERT, UPDATE, DELETE", "changes", "event_state"),
    ("serving", "SELECT", "corpus", "legal_event"),
    ("serving", "SELECT", "corpus", "currentness_profile"),
    ("serving", "SELECT", "changes", "event_state"),
    # The guard reads every corpus table (bundle v0003); granted here too, whatever the provisioning order.
    ("guard", "SELECT", "corpus", "legal_event"),
    ("guard", "SELECT", "corpus", "currentness_profile"),
)


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    schemas = {key: quote(ns.schema(key)) for key in ("corpus", "changes")}
    return conn, ns, schemas, quote


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, schemas, quote = names()
    types = ", ".join(f"'{t}'" for t in EVENT_TYPES)
    op.execute(TABLES.format(types=types, **schemas))
    for capability, privileges, schema, table in GRANTS:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"GRANT {privileges} ON {schemas[schema]}.{table} TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _, _, schemas, _ = names()
    op.execute(f"DROP TABLE {schemas['changes']}.event_state")
    op.execute(f"DROP TABLE {schemas['corpus']}.currentness_profile")
    op.execute(f"DROP TABLE {schemas['corpus']}.legal_event")
