"""A confidential withdrawal can hold a record that is approved but not yet public (findings item 35).

python -m changes.admin withdraw-confidentially blocks a record by its rights row, which exists only once
the record is public: for one approved but not yet published it refused, and the next publishing run
published it. corpus.privacy_hold names the works an operator holds back for a confidential reason
before they were ever public; publishing passes them over until lift-privacy-block removes the hold.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def corpus():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("corpus"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"""CREATE TABLE {corpus()}.privacy_hold (
    work_id text PRIMARY KEY CHECK (char_length(work_id) BETWEEN 1 AND 160),
    held_at timestamptz NOT NULL DEFAULT statement_timestamp()
)"""
    )
    # The guard reads every corpus table (bundle v0003): granted here too, whatever the provisioning order.
    conn = op.get_bind()
    guard = PolicyNamespace(conn.scalar(text("SELECT current_schema()"))).role("guard")
    if conn.scalar(text("SELECT to_regrole(:role)"), {"role": guard}) is not None:
        quoted = conn.dialect.identifier_preparer.quote_identifier(guard)
        op.execute(f"GRANT SELECT ON {corpus()}.privacy_hold TO {quoted}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP TABLE {corpus()}.privacy_hold")
