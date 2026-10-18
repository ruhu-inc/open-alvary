"""The section index (RET-01, MIN-02, D15): each published text's provisions as search units.

api.text_index keeps it in the transaction that changes the catalogue (changes.record.publish): a text
anyone may read now has its units here, and a withdrawn one has none. A unit is a provision, never a
whole text, with its span as UTF-8 byte offsets and two weighted search vectors (its headings and
words; its work's title and identifiers). No words are stored: snippets are read from the canonical
text after the rights recheck. The first publication after this migration builds every unit (built).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE {c}.text_unit (
    version_id text NOT NULL CHECK (char_length(version_id) BETWEEN 1 AND 160),
    start_byte integer NOT NULL CHECK (start_byte >= 0),
    end_byte integer NOT NULL CHECK (end_byte > start_byte),
    work_id text NOT NULL CHECK (char_length(work_id) BETWEEN 1 AND 160),
    node_id text NOT NULL CHECK (char_length(node_id) BETWEEN 1 AND 160),
    kind text NOT NULL CHECK (char_length(kind) BETWEEN 1 AND 40),
    locator text NOT NULL CHECK (char_length(locator) BETWEEN 1 AND 1000),
    pinpoint text CHECK (char_length(pinpoint) <= 1000),
    heading text CHECK (char_length(heading) <= 2000),
    language text NOT NULL CHECK (char_length(language) BETWEEN 1 AND 40),
    config text NOT NULL CHECK (config IN ('english', 'simple')),
    jurisdiction_id text NOT NULL CHECK (char_length(jurisdiction_id) BETWEEN 1 AND 160),
    document_type text NOT NULL CHECK (char_length(document_type) BETWEEN 1 AND 40),
    legal_status text NOT NULL CHECK (char_length(legal_status) BETWEEN 1 AND 40),
    year integer,
    effective_from date,
    effective_until date,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{{64}}$'),
    prior_version_id text,
    replaced boolean NOT NULL DEFAULT false,
    work_hash text NOT NULL CHECK (work_hash ~ '^[0-9a-f]{{64}}$'),
    document tsvector NOT NULL,
    context tsvector NOT NULL,
    PRIMARY KEY (version_id, start_byte)
);
CREATE INDEX text_unit_match ON {c}.text_unit USING gin ((document || context));
CREATE INDEX text_unit_work ON {c}.text_unit (work_id);
CREATE TABLE {c}.text_index_state (
    singleton integer PRIMARY KEY CHECK (singleton = 1),
    built boolean NOT NULL DEFAULT false,
    analyzer text,
    built_at timestamptz
);
INSERT INTO {c}.text_index_state (singleton) VALUES (1);
"""
GRANTS = (
    ("release", "SELECT, INSERT, UPDATE, DELETE", "text_unit"),
    ("release", "SELECT, UPDATE", "text_index_state"),
    ("serving", "SELECT", "text_unit"),
    ("serving", "SELECT", "text_index_state"),
)


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    return conn, ns, quote(ns.schema("changes")), quote


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, changes, quote = names()
    op.execute(TABLES.format(c=changes))
    for capability, privileges, table in GRANTS:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"GRANT {privileges} ON {changes}.{table} TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _, _, changes, _ = names()
    # The index is derived from the catalogue, which stays; nothing public is lost.
    op.execute(f"DROP TABLE {changes}.text_index_state")
    op.execute(f"DROP TABLE {changes}.text_unit")
