"""What the website's catalogue filters, counts and sorts by, in columns of their own (as migration 0045 keeps
the gates), so a request reads no record's payload but the page's.

The columns are generated from the payload, so they cannot drift from it:
- browse_type, browse_status and browse_year are the facets the payload holds (a record's jurisdiction and
  language are already columns, which gate_identity holds to the payload);
- browse_title is the title's sort key: exactly Python's str.casefold, compared in the order of code points;
- words_all, words_title and words_citation are api.search.VECTOR of those fields, the search's match and rank.
Nothing here is cached: each answer is worked out afresh, so a withdrawal takes effect on the next one.
tests/test_postgres_catalogue.py holds these to the same answers as reading each record through decide.

PostgreSQL 16, which production runs, has no case folding of its own (casefold comes in 18, and pg_c_utf8 in
17), so catalogue_title_key spells it out, from the interpreter's table when the migration runs. The test holds
it to str.casefold for every code point, so an interpreter with a newer Unicode fails it rather than drifting.

The title's index holds the first 200 characters, since a btree entry may not pass about 2,700 bytes and a title
has no limit: the rare titles alike that far are sorted among themselves.

The words index reads what 0038's indexes, but a word most records have is rightly scanned for, and over 0038's
expression that scan parsed every payload and analysed its words again. 0038's stays until the release before
this one has gone (deployment/runbook.md); a later migration drops it.

Adding the columns rewrites sources under an exclusive lock: 17 seconds at 120,000 records on a scratch cluster,
and 100 MB more (deployment/runbook.md).
"""

import sys

from alembic import op
from sqlalchemy import text

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

VECTOR = "to_tsvector('simple', regexp_replace(normalize({}, NFKC), '[[:punct:]]+', ' ', 'g'))"
ALL_FIELDS = (
    "coalesce(payload ->> 'title', '') || ' ' || coalesce(payload ->> 'citation', '') || ' '"
    " || coalesce(payload ->> 'summary', '')"
)
SOURCES = f"""
ALTER TABLE {{base}}.sources
    ADD COLUMN browse_type text GENERATED ALWAYS AS (payload ->> 'document_type') STORED,
    ADD COLUMN browse_status text GENERATED ALWAYS AS (payload ->> 'legal_status') STORED,
    ADD COLUMN browse_year integer GENERATED ALWAYS AS (
        CASE WHEN payload ->> 'year' ~ '^[1-9][0-9]{{{{0,8}}}}$' THEN (payload ->> 'year')::integer END
    ) STORED,
    ADD COLUMN browse_title text GENERATED ALWAYS AS (
        {{base}}.catalogue_title_key(payload ->> 'title')
    ) STORED,
    ADD COLUMN words_all tsvector GENERATED ALWAYS AS ({VECTOR.format(ALL_FIELDS)}) STORED,
    ADD COLUMN words_title tsvector GENERATED ALWAYS AS (
        {VECTOR.format("coalesce(payload ->> 'title', '')")}
    ) STORED,
    ADD COLUMN words_citation tsvector GENERATED ALWAYS AS (
        {VECTOR.format("coalesce(payload ->> 'citation', '')")}
    ) STORED
"""
# The record's facets beside its ID, from the index alone: a seventh of the reading. It does what 0045's did too.
INDEXES = """
DROP INDEX {base}.sources_public_gate;
CREATE INDEX sources_browse_facets ON {base}.sources (id)
    INCLUDE (jurisdiction_id, language, browse_type, browse_status, browse_year) WHERE gate_identity;
CREATE INDEX sources_browse_words ON {base}.sources USING gin (words_all);
CREATE INDEX sources_browse_title ON {base}.sources (left(browse_title, 200) COLLATE "C") WHERE gate_identity;
"""
PUBLIC_GATE = """
CREATE INDEX sources_public_gate ON {base}.sources (id) INCLUDE (jurisdiction_id) WHERE gate_identity
"""


def base():
    conn = op.get_bind()
    return conn.dialect.identifier_preparer.quote_identifier(conn.scalar(text("SELECT current_schema()")))


def fold_function(schema: str) -> str:
    """str.casefold in SQL: lower case for ASCII, and for the rest one translation of the letters that fold to
    one letter and a replacement for each that folds to several (ß to ss). Folding twice changes nothing, so
    the order of these cannot matter."""
    one, several = {}, {}
    for code in range(128, sys.maxunicode + 1):
        if 0xD800 <= code <= 0xDFFF:
            continue
        letter = chr(code)
        folded = letter.casefold()
        if folded != letter:
            (one if len(folded) == 1 else several)[letter] = folded
    rest = f"""translate(lower(value COLLATE "C"), '{"".join(one)}', '{"".join(one.values())}')"""
    for letter, folded in several.items():
        rest = f"replace({rest}, '{letter}', '{folded}')"
    return f"""
CREATE FUNCTION {schema}.catalogue_title_key(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE RETURNS NULL ON NULL INPUT AS $fold$
    SELECT CASE WHEN octet_length(value) = char_length(value) THEN lower(value COLLATE "C") ELSE {rest} END
$fold$
"""


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = base()
    op.execute(fold_function(schema))
    op.execute(SOURCES.format(base=schema))
    op.execute(INDEXES.format(base=schema))
    op.execute(f"ANALYZE {schema}.sources")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = base()
    op.execute(
        f"ALTER TABLE {schema}.sources DROP COLUMN browse_type, DROP COLUMN browse_status, DROP COLUMN browse_year,"
        " DROP COLUMN browse_title, DROP COLUMN words_all, DROP COLUMN words_title, DROP COLUMN words_citation"
    )
    op.execute(f"DROP FUNCTION {schema}.catalogue_title_key(text)")
    op.execute(PUBLIC_GATE.format(base=schema))
