"""Legal effects in the change log: the last public form of each reviewed link between works.

changes.record compares each checked repeal, amendment or supersession between two public works with
this state and records a legal_effect_recorded event when one becomes public, or a correction of that
event when its details change. The first comparison after this migration records the links already
public without events (links_baselined), so existing links are never announced as news.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE {c}.link_state (
    link_id text PRIMARY KEY CHECK (char_length(link_id) BETWEEN 1 AND 300),
    work_id text NOT NULL CHECK (char_length(work_id) BETWEEN 1 AND 160),
    projection jsonb NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    projection_hash text NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{{64}}$'),
    event_id uuid,
    event_revision integer CHECK (event_revision >= 1),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX link_state_work ON {c}.link_state (work_id);
ALTER TABLE {c}.feed_state ADD COLUMN links_baselined boolean NOT NULL DEFAULT false;
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
            op.execute(f"GRANT {privileges} ON {changes}.link_state TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _, _, changes, _ = names()
    # The state is derived from the catalogue and the events, which stay; nothing public is lost.
    op.execute(f"ALTER TABLE {changes}.feed_state DROP COLUMN links_baselined")
    op.execute(f"DROP TABLE {changes}.link_state")
