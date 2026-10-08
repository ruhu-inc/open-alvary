"""Review hardening: two-phase epoch close, broader denial journaling, ordered reconciliation."""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install, install_new, remove_new

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None
RESOURCE_HASH = "53d7c33f58901eb87f08311aa2d1f5b9866bbe2a953db5ea82ccdb0b3b036963"


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    install_new(conn, ns, "v0020", RESOURCE_HASH, ())
    install(conn, ns, "v0020", RESOURCE_HASH)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    epochs = ns.qualified(conn, "policy", "serving_epoch")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {epochs} WHERE state='closing')")):
        raise RuntimeError("A closing serving epoch requires forward repair")
    install(conn, ns, "v0020", RESOURCE_HASH, downgrade=True)
    remove_new(conn, ns, "v0020", RESOURCE_HASH)
