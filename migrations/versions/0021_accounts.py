"""MAC-03–05, MAC-13: the accounts schema for people, organisations, access and credentials.

Nothing is seeded here: tiers come from accounts/tiers.json through scripts.seed, and people only
through sign-in. The serving role may execute only the credential check and the usage counter.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install_new, remove_new

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None
RESOURCE_HASH = "bc77fa85656707f62dd35e4e150fcbbaf8f3f762c97141531fc7c601bcafeae8"
GUARD_GRANTS = (
    "GRANT USAGE ON SCHEMA {{accounts}} TO {{role_guard}}",
    "GRANT SELECT ON {{accounts}}.api_key, {{accounts}}.client_grant, {{accounts}}.membership, "
    "{{accounts}}.person, {{accounts}}.identity, {{accounts}}.organisation, {{accounts}}.entitlement, "
    "{{accounts}}.tier TO {{role_guard}}",
    "GRANT UPDATE (last_used_at) ON {{accounts}}.api_key, {{accounts}}.client_grant TO {{role_guard}}",
    "GRANT SELECT,INSERT,UPDATE ON {{accounts}}.usage_day TO {{role_guard}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.check_credential(jsonb) TO {{role_serving}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.record_usage(jsonb) TO {{role_serving}}",
)


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    install_new(op.get_bind(), namespace(), "v0021", RESOURCE_HASH, (), GUARD_GRANTS)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    people = ns.qualified(conn, "accounts", "person")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {people})")):
        raise RuntimeError("Accounts exist; a downgrade would delete them")
    remove_new(conn, ns, "v0021", RESOURCE_HASH)
