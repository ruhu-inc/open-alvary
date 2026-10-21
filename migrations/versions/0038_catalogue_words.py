"""The catalogue's word search (G11): a GIN index on the words of each source's title, citation and
summary, so a catalogue search reads its matches from an index and never scans for a substring.

The expression is exactly the one api.repositories.LegacyRepository.matching queries (api.search):
PostgreSQL's 'simple' analysis of the three fields together, in NFKC with punctuation read as spaces.
The index is derived from the catalogue; dropping it loses nothing. The 0012 trigram index stays until
its own removal is decided.
"""

from alembic import op
from sqlalchemy import text

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

INDEX = """
CREATE INDEX sources_catalogue_words ON {base}.sources USING gin (
    to_tsvector('simple', regexp_replace(normalize(
        coalesce(payload ->> 'title', '') || ' ' || coalesce(payload ->> 'citation', '') || ' '
            || coalesce(payload ->> 'summary', ''),
        NFKC), '[[:punct:]]+', ' ', 'g'))
)
"""


def base():
    conn = op.get_bind()
    return conn.dialect.identifier_preparer.quote_identifier(conn.scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(INDEX.format(base=base()))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX {base()}.sources_catalogue_words")
