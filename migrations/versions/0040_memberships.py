"""Membership of the continental and regional bodies (JUR-01, JUR-04): reviewed, dated facts, announced.

corpus.membership holds the reviewed memberships that operations.memberships publishes from the
membership register (schema/memberships.json): the member, the body, the inclusive dates, the evidence,
the note and who reviewed it when. Only reviewed memberships are stored; a proposal never is (every
reviewed column is required). Member and body are catalogue jurisdictions.

changes.membership_state is the last announced form of each, so changes.record.publish announces a newly
published membership as membership_recorded, its end (valid_to set) as another membership_recorded with
the reason ``ended``, and any other change, or its retraction, as a correction of the latest. The change
log's checks gain that type and that reason.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE {corpus}.membership (
    membership_id text PRIMARY KEY
        CHECK (membership_id ~ '^[a-z]{{2}}(-[a-z0-9]{{1,12}})?\\:aa-[a-z0-9]{{1,12}}:[1-9][0-9]{{0,2}}$'),
    member_id varchar(160) NOT NULL REFERENCES {base}.jurisdictions (id),
    body_id varchar(160) NOT NULL REFERENCES {base}.jurisdictions (id) CHECK (body_id LIKE 'aa-%'),
    valid_from date NOT NULL,
    valid_to date,
    evidence_url text NOT NULL CHECK (evidence_url ~ '^https://' AND char_length(evidence_url) <= 2000),
    note text NOT NULL CHECK (char_length(note) BETWEEN 1 AND 2000),
    reviewed_by text NOT NULL CHECK (char_length(reviewed_by) BETWEEN 1 AND 300),
    reviewed_at timestamptz NOT NULL,
    published_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (member_id <> body_id),
    CHECK (valid_to IS NULL OR valid_from <= valid_to)
);
CREATE INDEX membership_member ON {corpus}.membership (member_id);
CREATE INDEX membership_body ON {corpus}.membership (body_id);
CREATE TABLE {changes}.membership_state (
    membership_id text PRIMARY KEY CHECK (char_length(membership_id) BETWEEN 1 AND 160),
    member_id text NOT NULL CHECK (char_length(member_id) BETWEEN 1 AND 160),
    body_id text NOT NULL CHECK (char_length(body_id) BETWEEN 1 AND 160),
    projection jsonb NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    projection_hash text NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{{64}}$'),
    event_id uuid,
    event_revision integer CHECK (event_revision >= 1),
    event_key text CHECK (char_length(event_key) BETWEEN 1 AND 300),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
ALTER TABLE {changes}.change_event DROP CONSTRAINT change_event_event_type_check;
ALTER TABLE {changes}.change_event ADD CONSTRAINT change_event_event_type_check CHECK (event_type IN ({types}));
ALTER TABLE {changes}.change_event DROP CONSTRAINT change_event_change_reason_check;
ALTER TABLE {changes}.change_event ADD CONSTRAINT change_event_change_reason_check
    CHECK (change_reason IN ({reasons}));
"""
TYPES = (
    "catalogue_added", "source_publication_observed", "text_version_published", "metadata_corrected",
    "legal_effect_recorded", "proposal_recorded", "treatment_recorded", "withdrawal", "coverage_changed",
)  # fmt: skip
REASONS = (
    "first_text", "source_revision", "extraction_correction", "redaction", "unknown", "restored", "corrected",
)  # fmt: skip
GRANTS = (
    ("release", "SELECT, INSERT, UPDATE, DELETE", "corpus", "membership"),
    ("release", "SELECT, INSERT, UPDATE, DELETE", "changes", "membership_state"),
    ("serving", "SELECT", "corpus", "membership"),
    ("serving", "SELECT", "changes", "membership_state"),
    # The guard reads every corpus table (bundle v0003); granted here too, whatever the provisioning order.
    ("guard", "SELECT", "corpus", "membership"),
)


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    schemas = {key: quote(ns.schema(key)) for key in ("corpus", "changes")} | {"base": quote(ns.base)}
    return conn, ns, schemas, quote


def _listed(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, schemas, quote = names()
    types, reasons = _listed((*TYPES, "membership_recorded")), _listed((*REASONS, "ended"))
    op.execute(TABLES.format(types=types, reasons=reasons, **schemas))
    for capability, privileges, schema, table in GRANTS:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"GRANT {privileges} ON {schemas[schema]}.{table} TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, schemas, _ = names()
    events = ns.qualified(conn, "changes", "change_event")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {events} WHERE event_type = 'membership_recorded')")):
        raise RuntimeError("Membership events exist; a downgrade would break the public change log")
    changes = schemas["changes"]
    op.execute(
        f"ALTER TABLE {changes}.change_event DROP CONSTRAINT change_event_event_type_check;"
        f" ALTER TABLE {changes}.change_event ADD CONSTRAINT change_event_event_type_check"
        f" CHECK (event_type IN ({_listed(TYPES)}));"
        f" ALTER TABLE {changes}.change_event DROP CONSTRAINT change_event_change_reason_check;"
        f" ALTER TABLE {changes}.change_event ADD CONSTRAINT change_event_change_reason_check"
        f" CHECK (change_reason IN ({_listed(REASONS)}))"
    )
    # The state is derived from the catalogue and the events; the memberships are in the register file.
    op.execute(f"DROP TABLE {changes}.membership_state")
    op.execute(f"DROP TABLE {schemas['corpus']}.membership")
