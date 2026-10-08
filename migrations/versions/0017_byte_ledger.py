"""BUD absolute-byte ledger: reservations, emission state machine and reconciliation."""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install_new, remove_new

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None
RESOURCE_HASH = "147572c5eaf7d37d3d022f8c70dbe8338dc98f14ea401be06d415092ef36cc77"
TABLES = ("byte_bucket", "byte_reservation", "byte_reservation_scope")
GUARD_GRANTS = (
    "GRANT SELECT,INSERT,UPDATE ON {{policy}}.byte_bucket,{{policy}}.byte_reservation TO {{role_guard}}",
    "GRANT SELECT,INSERT ON {{policy}}.byte_reservation_scope TO {{role_guard}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.reserve_bytes(jsonb),{{policy}}.begin_emission(uuid),"
    "{{policy}}.release_bytes(uuid),{{policy}}.settle_bytes(uuid, bigint, text) TO {{role_serving}}",
)


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    install_new(op.get_bind(), namespace(), "v0017", RESOURCE_HASH, TABLES, GUARD_GRANTS)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    table = ns.qualified(conn, "policy", "byte_reservation")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table})")):
        raise RuntimeError("Byte accounting history requires forward repair")
    remove_new(conn, ns, "v0017", RESOURCE_HASH)
