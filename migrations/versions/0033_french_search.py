"""French analysis for the section index: a unit's config may also be 'french'.

api.text_index analyses French texts with PostgreSQL's french configuration (stemming and French
common words) from analyzer oa-search-2; the index is rebuilt at the next publication or worker run.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def table():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    return f"{quote(ns.schema('changes'))}.text_unit"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    units = table()
    op.execute(f"ALTER TABLE {units} DROP CONSTRAINT text_unit_config_check")
    op.execute(
        f"ALTER TABLE {units} ADD CONSTRAINT text_unit_config_check CHECK (config IN ('english', 'french', 'simple'))"
    )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    units = table()
    # French units are derived; the next publication indexes them again as simple.
    op.execute(f"DELETE FROM {units} WHERE config = 'french'")
    op.execute(f"ALTER TABLE {units} DROP CONSTRAINT text_unit_config_check")
    op.execute(
        f"ALTER TABLE {units} ADD CONSTRAINT text_unit_config_check CHECK (config IN ('english', 'simple'))"
    )
