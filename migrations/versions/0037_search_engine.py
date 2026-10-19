"""The dedicated search engine's state (RSV-04, RSV-10): where the engine stands against the system of
record, and index generations whose vectors live in the engine.

changes.engine_state records the change-log position the engine has caught up to (publication revision
within the feed's epoch) and the application deployed (its hash), so retrieval.projector resumes from
there and a feed reset or a new application forces a full rebuild. index_generation.backend may now be
``vespa``: the generation's vectors are a field of the engine's units, not a PostgreSQL table.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE {c}.engine_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    engine text NOT NULL CHECK (engine IN ('vespa')),
    application_hash text CHECK (application_hash ~ '^[0-9a-f]{{64}}$'),
    feed_epoch uuid,
    publication_revision bigint NOT NULL DEFAULT 0 CHECK (publication_revision >= 0),
    synced_at timestamptz,
    rebuilt_at timestamptz
);
ALTER TABLE {c}.index_generation DROP CONSTRAINT index_generation_backend_check;
ALTER TABLE {c}.index_generation ADD CONSTRAINT index_generation_backend_check
    CHECK (backend IN ('pgvector', 'exact', 'vespa'));
ALTER TABLE {c}.index_generation DROP CONSTRAINT index_generation_vectors_table_check;
ALTER TABLE {c}.index_generation ADD CONSTRAINT index_generation_vectors_table_check
    CHECK (vectors_table ~ '^(vectors|emb)_[0-9a-z_]{{1,64}}$');
"""
GRANTS = (("release", "SELECT, INSERT, UPDATE"), ("serving", "SELECT"))


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
            op.execute(f"GRANT {privileges} ON {changes}.engine_state TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _, _, changes, _ = names()
    op.execute(f"DELETE FROM {changes}.index_generation WHERE backend = 'vespa'")
    op.execute(f"ALTER TABLE {changes}.index_generation DROP CONSTRAINT index_generation_backend_check")
    op.execute(
        f"ALTER TABLE {changes}.index_generation ADD CONSTRAINT index_generation_backend_check"
        " CHECK (backend IN ('pgvector', 'exact'))"
    )
    op.execute(f"ALTER TABLE {changes}.index_generation DROP CONSTRAINT index_generation_vectors_table_check")
    op.execute(
        f"ALTER TABLE {changes}.index_generation ADD CONSTRAINT index_generation_vectors_table_check"
        " CHECK (vectors_table ~ '^vectors_[0-9a-f]{32}$')"
    )
    op.execute(f"DROP TABLE {changes}.engine_state")
