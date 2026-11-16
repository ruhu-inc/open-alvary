"""Each citation's and treatment's words are tied to the text they were taken from (findings item 33).

A citation's printed words and quoted paragraph, and a treatment's passage, are the citing judgment's
own words, served only while its text is public. They were tied to the work, not a text, so after the
judgment was republished with names withheld its old words, naming the party, were served again.
source_sha256 records the content hash of the citing text the words were taken from (for a citation,
its printed words and quote; for a treatment, its passage), and the words are served only while that
very text is public. Rows recorded before this have none, and their words are not served (no citation
was public when this was written).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def corpus():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("corpus"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = corpus()
    for table in ("citation", "judicial_treatment"):
        op.execute(
            f"ALTER TABLE {schema}.{table} ADD COLUMN source_sha256 text CHECK (source_sha256 ~ '^[0-9a-f]{{64}}$')"
        )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = corpus()
    for table in ("citation", "judicial_treatment"):
        op.execute(f"ALTER TABLE {schema}.{table} DROP COLUMN source_sha256")
