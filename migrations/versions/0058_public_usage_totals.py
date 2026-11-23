"""The home page's live totals (owner, 2026-11-23): one read-only function the website's login may run.

policy.public_usage_totals() sums the last 30 days of the day counts the website (traffic_day) and the
credential gate (usage_day) already keep: texts read, searches and those that found something, API and MCP
requests, and the organisations that called this week. It returns totals only, and a total under 10 as null,
so it answers about no visitor, organisation or credential and holds nothing the privacy notice does not
already describe. It runs as the guard, which already reads both tables (migrations 0021 and 0044), looks only
in pg_catalog and pg_temp as every function since migration 0050 does, and is granted to the serving role;
the website's own login is given it by rights/logins.py.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install_new, remove_new

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

RESOURCE_HASH = "692df31a73da3a462347b622684b2fb8cbaa2728f48a0dcb58269abf1c280df6"
# Fresh provisioning gets the same grant from rights/sql/provision/v0058.sql.
GUARD_GRANTS = ("GRANT EXECUTE ON FUNCTION {{policy}}.public_usage_totals() TO {{role_serving}}",)


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    install_new(op.get_bind(), namespace(), "v0058", RESOURCE_HASH, (), GUARD_GRANTS)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    remove_new(op.get_bind(), namespace(), "v0058", RESOURCE_HASH)
