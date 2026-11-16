"""The expiry step remembers which expiry it compared (findings 2026-11-16, item 12).

Every five minutes the worker compares the works whose rights review has expired. Each comparison
rewrites the work's published_state row (updated_at), so once a jurisdiction's rights decision lapsed,
every run rewrote the whole country. published_state.expiry_compared holds the expires_at value the step
last compared the work after; the step then skips works whose expiry it has already seen, and a renewed
review with a new expiry is compared again when that one passes.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def published_state():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    return f"{quote(ns.schema('changes'))}.published_state"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE {published_state()} ADD COLUMN expiry_compared text")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE {published_state()} DROP COLUMN expiry_compared")
