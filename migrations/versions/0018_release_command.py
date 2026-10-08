"""Audited metadata publication command; revoke direct release-role projection writes."""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install_new, remove_new

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None
RESOURCE_HASH = "33779f3798d50610ec0939c3be22a189510f7af89d97828874e0a8bb0d1a6d52"
GUARD_GRANTS = (
    "GRANT INSERT,UPDATE ON {{corpus}}.metadata_projection TO {{role_guard}}",
    "GRANT SELECT,INSERT ON {{policy}}.release_receipt TO {{role_guard}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.apply_release_command(jsonb) TO {{role_release}}",
    "REVOKE INSERT,UPDATE ON {{corpus}}.metadata_projection FROM {{role_release}}",
)


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    install_new(op.get_bind(), namespace(), "v0018", RESOURCE_HASH, ("release_receipt",), GUARD_GRANTS)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    table = ns.qualified(conn, "policy", "release_receipt")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table})")):
        raise RuntimeError("Release history requires forward repair")
    tokens = remove_new(conn, ns, "v0018", RESOURCE_HASH)
    if conn.scalar(text("SELECT to_regrole(:role)"), {"role": ns.role("release")}) is not None:
        corpus, release = tokens["corpus"], tokens["role_release"]
        conn.exec_driver_sql(f"GRANT INSERT,UPDATE ON {corpus}.metadata_projection TO {release}")
