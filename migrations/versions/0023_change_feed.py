"""SUB C1: the public change log (changes-contracts.md).

Events are appended by the transaction that changes the public catalogue (changes.record), in
LIF-02 publication-revision order. Nothing is recorded here: the first publication after this
migration records a baseline without events, so existing records are never announced as new.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install_new, remove_new

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None
RESOURCE_HASH = "c8a80c370ad2756aa971fd004f51e3f774aca17f0c2d76a6dcd7e50e4c1aa404"
GUARD_GRANTS = (
    "GRANT USAGE ON SCHEMA {{changes}} TO {{role_release}}, {{role_serving}}",
    "GRANT SELECT,INSERT ON {{changes}}.change_event TO {{role_release}}",
    "GRANT SELECT,INSERT,UPDATE ON {{changes}}.published_state, {{changes}}.coverage_state, "
    "{{changes}}.feed_state TO {{role_release}}",
    "GRANT SELECT ON {{changes}}.change_event, {{changes}}.feed_state, {{changes}}.coverage_state "
    "TO {{role_serving}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.allocate_publication_revision() TO {{role_release}}",
)


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    install_new(op.get_bind(), namespace(), "v0023", RESOURCE_HASH, (), GUARD_GRANTS)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    events = ns.qualified(conn, "changes", "change_event")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {events})")):
        raise RuntimeError("Change events exist; a downgrade would delete the public change log")
    remove_new(conn, ns, "v0023", RESOURCE_HASH)
