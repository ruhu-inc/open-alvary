"""Portuguese, Spanish and Arabic analysis for the section index: a unit's config, and a document
unit's, may also be 'portuguese', 'spanish' or 'arabic'.

api.text_index analyses Portuguese and Spanish texts with PostgreSQL's configurations of those names
(stemming and common words) and Arabic texts with its arabic configuration (stemming only: PostgreSQL
has no Arabic list of common words) from analyzer oa-search-5; the index is rebuilt at the next
publication or worker run. Other languages, Kiswahili among them, stay simple.

The any-word search asks, for each config, whether any unit has it; with six configs, most absent
from a deployment, an index on config answers that without reading the table.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

BEFORE = ("english", "french", "simple")
AFTER = ("english", "french", "portuguese", "spanish", "arabic", "simple")


def schema():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("changes"))


def allow(changes: str, table: str, configs: tuple[str, ...]) -> None:
    values = ", ".join(f"'{c}'" for c in configs)
    op.execute(f"ALTER TABLE {changes}.{table} DROP CONSTRAINT {table}_config_check")
    op.execute(
        f"ALTER TABLE {changes}.{table} ADD CONSTRAINT {table}_config_check CHECK (config IN ({values}))"
    )


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    changes = schema()
    allow(changes, "text_unit", AFTER)
    allow(changes, "document_unit", AFTER)
    op.execute(f"CREATE INDEX text_unit_config ON {changes}.text_unit (config)")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    changes = schema()
    op.execute(f"DROP INDEX {changes}.text_unit_config")
    # Derived units; the next publication indexes these texts again as simple.
    for table in ("text_unit", "document_unit"):
        op.execute(f"DELETE FROM {changes}.{table} WHERE config IN ('portuguese', 'spanish', 'arabic')")
        allow(changes, table, BEFORE)
