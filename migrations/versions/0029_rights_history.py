"""The public rights history: every revision of each work's rights record, never edited.

Written by the transaction that changes the catalogue (changes.record), beside the change log: a
new revision whenever a work's rights record differs from its last one, with the publication
revision it committed at (0 for the first baseline). Text and withdrawal events name the rights
revision in force, so a copy of a text can say which decision it was cleared under. Nothing is
recorded here: the next publication records the current records as each work's first revision.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE {c}.rights_revision (
    work_id text NOT NULL CHECK (char_length(work_id) BETWEEN 1 AND 160),
    revision integer NOT NULL CHECK (revision >= 1),
    publication_revision bigint NOT NULL CHECK (publication_revision >= 0),
    committed_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    record_hash text NOT NULL CHECK (record_hash ~ '^[0-9a-f]{{64}}$'),
    PRIMARY KEY (work_id, revision)
);
CREATE TRIGGER rights_revision_append_only BEFORE UPDATE OR DELETE ON {c}.rights_revision
    FOR EACH ROW EXECUTE FUNCTION {p}.changes_append_only();
"""
# The same capabilities as the change log (v0023): the release role appends, serving reads.
GRANTS = (("release", "SELECT, INSERT"), ("serving", "SELECT"))


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    return conn, ns, quote(ns.schema("changes")), quote(ns.schema("policy")), quote


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, changes, policy, quote = names()
    op.execute(TABLE.format(c=changes, p=policy))
    for capability, privileges in GRANTS:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"GRANT {privileges} ON {changes}.rights_revision TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, _, changes, _, _ = names()
    # The baseline (publication revision 0) is today's records again; anything later is history.
    if conn.scalar(
        text(f"SELECT EXISTS(SELECT 1 FROM {changes}.rights_revision WHERE publication_revision > 0)")
    ):
        raise RuntimeError("Rights revisions exist; a downgrade would delete the public rights history")
    op.execute(f"DROP TABLE {changes}.rights_revision")
