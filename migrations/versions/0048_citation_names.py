"""A citation's exact lookup from an index of the values that name a record, so that a text search, which tries it
first, need not read every record's payload: 430 ms at 50,000 records, and 170 ms more for as many aliases.

catalogue_name_key is exactly api.search.normalize_citation: NFKC, then str.casefold (catalogue_title_key, migration
0046), then str.split's whitespace, each run of it one space and none at the ends. The whitespace is spelled out,
since [[:space:]] depends on the locale and leaves out U+001C to U+001F. The caller still confirms each value in
Python. tests/test_postgres_citation_names.py holds the key to Python for every code point there is, so an
interpreter or a PostgreSQL with a newer Unicode fails it rather than drifting.

Before this, legacy_metadata_normalize folded only ASCII, so every value that was not ASCII came back on every
search for Python to compare. That cost more with each accented title, and past MAX_UNPAGED_ROWS of them every
citation lookup, so every text search, was refused. Now the only values that come back are those that read as the
term. legacy_metadata_normalize stays for the code before this and for the trigram index (deployment/runbook.md).

Hash indexes, as these are only ever compared whole and a title has no limit: a btree entry may not pass about 2,700
bytes. Expression indexes rewrite nothing, and PostgreSQL keeps statistics for them, so no generated column is
needed; browse_title and words_title cannot answer this, as they read a value differently. They are kept in the
statement that changes a record, so they cannot drift, and they hold no record: nothing is cached, and a withdrawal
takes effect on the next answer (F01), since each candidate is still read through the gate.

Building them reads each payload once, under a lock that lets searches run but holds publications back: 10 seconds
at 50,000 records and as many aliases on a scratch cluster, under load (deployment/runbook.md).
"""

from alembic import op
from sqlalchemy import text

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

# str.split's whitespace, as PostgreSQL's E'' strings write it. A later Unicode changing it fails the test above.
SPACES = (
    "\\u0009\\u000a\\u000b\\u000c\\u000d\\u001c\\u001d\\u001e\\u001f\\u0020\\u0085\\u00a0\\u1680\\u2000\\u2001\\u2002"
    "\\u2003\\u2004\\u2005\\u2006\\u2007\\u2008\\u2009\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000"
)
BLANKS = " " * (len(SPACES) // 6)
# ASCII needs only its letters lowered, which is most of the cost of building the indexes.
FUNCTION = f"""
CREATE FUNCTION {{base}}.catalogue_name_key(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE RETURNS NULL ON NULL INPUT AS $name$
    SELECT btrim(regexp_replace(translate(
        CASE WHEN octet_length(value) = char_length(value) THEN lower(value COLLATE "C")
        ELSE {{base}}.catalogue_title_key(normalize(value, NFKC)) END,
        E'{SPACES}', '{BLANKS}'), ' +', ' ', 'g'))
$name$
"""
INDEXES = {
    "sources_name_title": ("sources", "title"),
    "sources_name_citation": ("sources", "citation"),
    "citations_name_value": ("citations", "value"),
}


def base():
    conn = op.get_bind()
    return conn.dialect.identifier_preparer.quote_identifier(conn.scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = base()
    op.execute(FUNCTION.format(base=schema))
    for index, (table, field) in INDEXES.items():
        op.execute(
            f"CREATE INDEX {index} ON {schema}.{table} USING hash ({schema}.catalogue_name_key(payload ->> '{field}'))"
        )
    for table in ("sources", "citations"):  # the planner knows at once that a value names few records
        op.execute(f"ANALYZE {schema}.{table}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = base()
    for index in INDEXES:
        op.execute(f"DROP INDEX {schema}.{index}")
    op.execute(f"DROP FUNCTION {schema}.catalogue_name_key(text)")
