"""Pin reviewed SQL resources without rewriting applied migrations or identities."""

from alembic import op

from rights.database import PolicyNamespace
from rights.sql_resources import install

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None
RESOURCE_HASH = "ab6830ff490899f0f22ac524e60fdcd7d82d38002cdeea871adbb761a27a7c0b"


def apply(downgrade=False):
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
        install(conn, PolicyNamespace(base), "v0011", RESOURCE_HASH, downgrade=downgrade)


def upgrade():
    apply()


def downgrade():
    apply(downgrade=True)
