"""Semantic index generations and their jobs (RSV-10).

An index profile (model and revision, dimensions, metric, precision, how units are cut into windows) is
built as generations: building, validating, active, retired, at most one active per profile. A
generation's vectors live in a table of their own, created by the operator's `retrieval.indexer prepare`
(the vector type and its index need the pgvector extension and an administrator), so this migration
needs no extension. Publication queues one job per new text for every building or active generation, in
its own transaction; workers embed outside any transaction and write the text's vectors at once.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE {c}.index_generation (
    generation_id text PRIMARY KEY CHECK (generation_id ~ '^gen_[0-9a-f]{{32}}$'),
    profile_id text NOT NULL CHECK (char_length(profile_id) BETWEEN 1 AND 160),
    profile jsonb NOT NULL CHECK (jsonb_typeof(profile) = 'object'),
    backend text NOT NULL CHECK (backend IN ('pgvector', 'exact')),
    vectors_table text NOT NULL CHECK (vectors_table ~ '^vectors_[0-9a-f]{{32}}$'),
    state text NOT NULL CHECK (state IN ('building', 'validating', 'active', 'retired')),
    manifest jsonb NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(manifest) = 'object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    activated_at timestamptz,
    retired_at timestamptz
);
CREATE UNIQUE INDEX index_generation_one_active ON {c}.index_generation (profile_id) WHERE state = 'active';
CREATE TABLE {c}.index_job (
    generation_id text NOT NULL REFERENCES {c}.index_generation (generation_id) ON DELETE CASCADE,
    version_id text NOT NULL CHECK (char_length(version_id) BETWEEN 1 AND 160),
    state text NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_until timestamptz,
    error text CHECK (char_length(error) <= 1000),
    vectors integer CHECK (vectors >= 0),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (generation_id, version_id)
);
CREATE INDEX index_job_queue ON {c}.index_job (state, updated_at);
"""
GRANTS = (
    ("release", "SELECT, INSERT, UPDATE", "index_generation"),
    ("release", "SELECT, INSERT, UPDATE, DELETE", "index_job"),
    ("serving", "SELECT", "index_generation"),
    ("serving", "SELECT", "index_job"),
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
    conn, _, changes, quote = names()
    # A generation's vectors are derived from published text; dropping them loses nothing public.
    for (table,) in conn.execute(text(f"SELECT vectors_table FROM {changes}.index_generation")).all():
        op.execute(f"DROP TABLE IF EXISTS {changes}.{quote(table)}")
    op.execute(f"DROP TABLE {changes}.index_job")
    op.execute(f"DROP TABLE {changes}.index_generation")
