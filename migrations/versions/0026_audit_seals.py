"""MAC-13.7: each UTC day's audit entries sealed with a hash chained to the day before (accounts.seals).

Seals hold no personal data (counts, sequence numbers and hashes) and outlive the entries they cover,
so the chain still verifies after the retention purge. They share the log's append-only trigger:
seals can be added, never changed, and the retention purge, the only exception it allows, leaves
them alone.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

HASH = "CHECK ({column} ~ '^[0-9a-f]{{64}}$')"
TABLES = """
CREATE TABLE {a}.audit_seal (
    day date PRIMARY KEY,
    entries integer NOT NULL CHECK (entries >= 0),
    first_seq bigint,
    last_seq bigint,
    entries_hash text NOT NULL {entries_hash},
    previous_hash text {previous_hash},
    seal_hash text NOT NULL UNIQUE {seal_hash},
    at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK ((entries = 0) = (first_seq IS NULL) AND (first_seq IS NULL) = (last_seq IS NULL))
);
CREATE TRIGGER audit_seal_append_only BEFORE UPDATE OR DELETE ON {a}.audit_seal
    FOR EACH ROW EXECUTE FUNCTION {p}.accounts_append_only();
"""


def schemas():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    return quote(ns.schema("accounts")), quote(ns.schema("policy"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    accounts, policy = schemas()
    checks = {c: HASH.format(column=c) for c in ("entries_hash", "previous_hash", "seal_hash")}
    op.execute(TABLES.format(a=accounts, p=policy, **checks))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    accounts, _ = schemas()
    if op.get_bind().scalar(text(f"SELECT EXISTS(SELECT 1 FROM {accounts}.audit_seal)")):
        raise RuntimeError("Audit seals exist; a downgrade would delete the record that the log is intact")
    op.execute(f"DROP TABLE {accounts}.audit_seal")
