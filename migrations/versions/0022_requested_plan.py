"""An access request names the plan it asks for (published plans, owner decision 2026-10-15)."""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return ns.qualified(conn, "accounts", "access_request"), ns.qualified(conn, "accounts", "tier")


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    requests, tiers = names()
    op.execute(f"ALTER TABLE {requests} ADD COLUMN tier_requested text REFERENCES {tiers} (id)")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    requests, _ = names()
    op.execute(f"ALTER TABLE {requests} DROP COLUMN tier_requested")
