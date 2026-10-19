"""Document units (RSV-02): one search unit per published text for whole-law questions.

api.text_index keeps them beside the section index, in the same transaction: a text anyone may read now
has one document unit, a withdrawn one has none. A document unit's words are its work's title and the
text's long title or preamble (its quotable span, by byte offsets), weighted with the outline of its
part, chapter and section headings; its names are the work's title, citation and aliases, normalised,
so a query that names the instrument is recognised (RSV-03 instrument route). No words are stored.
The first publication after this migration rebuilds the index (analyzer oa-search-3).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE {c}.document_unit (
    version_id text PRIMARY KEY CHECK (char_length(version_id) BETWEEN 1 AND 160),
    work_id text NOT NULL CHECK (char_length(work_id) BETWEEN 1 AND 160),
    start_byte integer NOT NULL CHECK (start_byte >= 0),
    end_byte integer NOT NULL CHECK (end_byte >= start_byte),
    node_id text CHECK (char_length(node_id) BETWEEN 1 AND 160),
    language text NOT NULL CHECK (char_length(language) BETWEEN 1 AND 40),
    config text NOT NULL CHECK (config IN ('english', 'french', 'simple')),
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
    names text[] NOT NULL,
    outline_truncated boolean NOT NULL DEFAULT false,
    document tsvector NOT NULL,
    context tsvector NOT NULL
);
CREATE INDEX document_unit_match ON {c}.document_unit USING gin ((document || context));
CREATE INDEX document_unit_work ON {c}.document_unit (work_id);
"""
GRANTS = (("release", "SELECT, INSERT, UPDATE, DELETE"), ("serving", "SELECT"))


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    return conn, ns, quote(ns.schema("changes")), quote


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, changes, quote = names()
    op.execute(TABLE.format(c=changes))
    for capability, privileges in GRANTS:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"GRANT {privileges} ON {changes}.document_unit TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _, _, changes, _ = names()
    # Derived from the catalogue, which stays; nothing public is lost.
    op.execute(f"DROP TABLE {changes}.document_unit")
