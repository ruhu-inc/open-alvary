"""Each section unit's heaviest words, so a search for one common word need not score every unit that has it.

A search for one word scores a unit by that word's weight there: the sum over the places it is found of 1.0 in a
heading, 0.4 in the words, 0.2 in the work's title and 0.1 in the identifiers (oa-rank-1). This index names the
words of weight 1.2 or more in each unit, each with the band its weight falls in, which is about 10 of a unit's
130. api.text_index reads it to find the first hits, and shows from the bands that no other unit could come before
them. It changes no score and no order; tests/test_postgres_search.py holds the search to scoring every match.

An expression index, so it cannot drift from the vectors: it is kept in the statement that changes a unit, and
nothing here is a copy of any record. A band's name is part of what is indexed, and api.text_index.BANDS names
them.

Building it reads each unit's vectors once: 130 seconds and 57 MB at 2 million units on a scratch cluster, under a
lock that lets searches run but holds publications back (deployment/runbook.md).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE FUNCTION {c}.unit_heavy_words(words tsvector) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $heavy$
    SELECT coalesce(array_agg(w.lexeme || ' ' || band.name) FILTER (WHERE band.name IS NOT NULL), '{{}}')
    FROM unnest(words) AS w(lexeme, positions, weights)
    CROSS JOIN LATERAL (
        SELECT CASE
            WHEN weight >= 12.8 THEN '12.8' WHEN weight >= 6.4 THEN '6.4' WHEN weight >= 3.2 THEN '3.2'
            WHEN weight >= 2.4 THEN '2.4' WHEN weight >= 1.6 THEN '1.6' WHEN weight >= 1.2 THEN '1.2'
        END AS name
        FROM (
            SELECT sum(CASE place WHEN 'A' THEN 1.0 WHEN 'B' THEN 0.4 WHEN 'C' THEN 0.2 ELSE 0.1 END) AS weight
            FROM unnest(w.weights) AS place
        ) AS total
    ) AS band
    WHERE cardinality(w.positions) > 1
$heavy$
"""


def schema():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("changes"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    changes = schema()
    op.execute(FUNCTION.format(c=changes))
    op.execute(
        f"CREATE INDEX text_unit_heavy_words ON {changes}.text_unit"
        f" USING gin ({changes}.unit_heavy_words(document || context))"
    )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    changes = schema()
    op.execute(f"DROP INDEX {changes}.text_unit_heavy_words")
    op.execute(f"DROP FUNCTION {changes}.unit_heavy_words(tsvector)")
