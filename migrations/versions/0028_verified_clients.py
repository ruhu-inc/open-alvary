"""MAC-07, MAC-11: tools shown as verified publishers, pinned by an operator to their client metadata
document's address, with who pinned them and why. Seeded with the hosts certified so far (MAC-13);
operators pin and unpin in the workbench's Access section. Unpinning keeps the row.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

TABLE = """
CREATE TABLE {a}.verified_client (
    client_id text PRIMARY KEY CHECK (client_id ~ '^https://[^[:space:]]+$' AND char_length(client_id) <= 2008),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    publisher text NOT NULL CHECK (char_length(publisher) BETWEEN 1 AND 100),
    pinned_by text NOT NULL CHECK (char_length(pinned_by) BETWEEN 1 AND 320),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    pinned_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    unpinned_at timestamptz,
    unpinned_by text
);
"""
CERTIFIED = (
    ("https://claude.ai/oauth/mcp-oauth-client-metadata", "Claude", "Anthropic"),
    ("https://claude.ai/oauth/claude-code-client-metadata", "Claude Code", "Anthropic"),
    ("https://chatgpt.com/oauth/client.json", "ChatGPT", "OpenAI"),
    ("https://chatgpt.com/oauth/codex/client.json", "Codex", "OpenAI"),
)


def schema():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("accounts"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    accounts = schema()
    op.execute(TABLE.format(a=accounts))
    for client_id, name, publisher in CERTIFIED:
        op.get_bind().execute(
            text(
                f"INSERT INTO {accounts}.verified_client (client_id, name, publisher, pinned_by, reason)"
                " VALUES (:c, :n, :p, 'built-in', 'Host certified by the MAC-13 client matrix')"
            ),
            {"c": client_id, "n": name, "p": publisher},
        )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP TABLE {schema()}.verified_client")
