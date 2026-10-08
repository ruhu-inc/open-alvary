"""Allow one accountable person to record both the rights and content approvals.

Both decisions remain required, each with its own assignment, evidence, revision and audit;
only the requirement that two different actors record them is removed (owner decision).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None
RESOURCE_HASH = "e118cf5811d0819677906e1bbd75187daf98915b76117006664010a9ab70c77a"


def apply(downgrade=False):
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    install(conn, ns, "v0019", RESOURCE_HASH, downgrade=downgrade)


def upgrade():
    apply()


def downgrade():
    # Restoring the stricter two-actor rule needs no data repair.
    apply(downgrade=True)
