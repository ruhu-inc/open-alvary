"""The catalogue's gates in columns of their own, so its counts and the sitemap are read without parsing a
record: /coverage cost 700 seconds at 50,000 records with texts, and a plain database count still 50,
since a version's payload holds its text and every field read from a json value parses all of it.

The columns are generated from the payload, so they cannot drift from it, and they are not new facts:
- gate_identity (sources) and gate_public (rights) are the parts of LegacyRepository._visible;
- gate_text (rights, versions) is what rights.policy.decide asks of them that does not depend on the time
  of asking; the rest of what it asks, and whether a version's hash is approved, is left to each answer,
  so a review that expires takes effect when it expires. A malformed value is simply not counted.
Nothing here is cached: each answer is worked out afresh, so a withdrawal takes effect on the next one.
tests/test_postgres_coverage.py holds these to the same answers as decide; a change to the one needs a
migration here too.

Partial indexes on the payload expressions look cheaper, with no rewrite, but were measured and refused:
PostgreSQL has no statistics for those expressions and takes the indexed rows for one, so it joins them
with a nested loop over a filter, which ran for minutes at 50,000 records. They would match _visible, so
every other catalogue query could have been planned that way too. Plain columns have statistics.

Adding a generated column rewrites the table under an exclusive lock, every text included: 66 seconds for
versions at 50,000 records and 3 GB of text. Reads of the catalogue wait for it (deployment/runbook.md).
"""

from alembic import op
from sqlalchemy import text

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

CONDITIONAL = "payload ->> 'status' = 'OPEN_WITH_CONDITIONS' OR payload ->> 'redistribution_allowed' = 'conditional'"
SOURCES = """
ALTER TABLE {base}.sources
    ADD COLUMN gate_identity boolean GENERATED ALWAYS AS (
        payload ->> 'id' = id
        AND payload ->> 'jurisdiction_id' = jurisdiction_id
        AND payload ->> 'authority_id' = authority_id
        AND payload ->> 'language' = language
    ) STORED
"""
RIGHTS = f"""
ALTER TABLE {{base}}.rights
    ADD COLUMN gate_public boolean GENERATED ALWAYS AS (
        payload ->> 'id' = id
        AND payload ->> 'source_id' = source_id
        AND payload ->> 'metadata_public' = 'true'
        AND payload ->> 'metadata_basis' <> ''
        AND payload ->> 'status' <> 'RESTRICTED'
    ) STORED,
    ADD COLUMN gate_text boolean GENERATED ALWAYS AS (
        payload ->> 'id' = id
        AND payload ->> 'source_id' = source_id
        AND payload ->> 'metadata_public' = 'true'
        AND payload ->> 'metadata_basis' <> ''
        AND payload ->> 'status' IN ('OPEN', 'OPEN_WITH_CONDITIONS')
        AND payload ->> 'redistribution_allowed' IN ('yes', 'conditional')
        AND payload ->> 'commercial_reuse_allowed' = 'yes'
        AND payload ->> 'derivatives_allowed' = 'yes'
        AND payload ->> 'reviewer' <> ''
        AND payload ->> 'licence' <> ''
        AND payload ->> 'rights_holder' <> ''
        AND payload ->> 'legal_note' <> ''
        AND payload ->> 'database_rights_notes' <> ''
        AND payload ->> 'evidence' LIKE '["%'
        AND payload ->> 'privacy_status' IN ('cleared', 'redacted')
        AND (
            payload ->> 'attribution_required' = 'false'
            OR (payload ->> 'attribution_required' = 'true' AND payload ->> 'attribution_text' <> '')
        )
        AND (
            (payload ->> 'conditions' LIKE '["%' AND payload ->> 'conditions_satisfied' = 'true')
            OR (coalesce(payload ->> 'conditions', '[]') = '[]' AND NOT ({CONDITIONAL}))
        )
    ) STORED,
    ADD COLUMN gate_reviewed_at text GENERATED ALWAYS AS (payload ->> 'reviewed_at') STORED,
    ADD COLUMN gate_expires_at text GENERATED ALWAYS AS (payload ->> 'expires_at') STORED,
    ADD COLUMN gate_approved text GENERATED ALWAYS AS (payload ->> 'approved_content_hashes') STORED
"""
VERSIONS = """
ALTER TABLE {base}.versions
    ADD COLUMN gate_text boolean GENERATED ALWAYS AS (
        payload ->> 'id' = id
        AND payload ->> 'source_id' = source_id
        AND payload ->> 'language' = language
        AND payload ->> 'verification_status' = 'verified'
        AND payload ->> 'content_verifier' <> ''
    ) STORED,
    ADD COLUMN gate_content_hash text GENERATED ALWAYS AS (payload ->> 'content_hash') STORED,
    ADD COLUMN gate_verified_at text GENERATED ALWAYS AS (payload ->> 'content_verified_at') STORED
"""
# The public records in ID order from the indexes alone: the sitemap's walk and the counts by jurisdiction.
INDEXES = """
CREATE INDEX sources_public_gate ON {base}.sources (id) INCLUDE (jurisdiction_id) WHERE gate_identity;
CREATE INDEX rights_public_gate ON {base}.rights (source_id) WHERE gate_public;
"""
TABLES = {
    "sources": ("gate_identity",),
    "rights": ("gate_public", "gate_text", "gate_reviewed_at", "gate_expires_at", "gate_approved"),
    "versions": ("gate_text", "gate_content_hash", "gate_verified_at"),
}


def base():
    conn = op.get_bind()
    return conn.dialect.identifier_preparer.quote_identifier(conn.scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = base()
    for statement in (SOURCES, RIGHTS, VERSIONS, INDEXES):
        op.execute(statement.format(base=schema))
    for table in TABLES:  # the planner knows at once that nearly every row passes
        op.execute(f"ANALYZE {schema}.{table}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    schema = base()
    for table, columns in TABLES.items():
        # Dropping a column rewrites nothing; its index goes with it.
        drops = ", ".join(f"DROP COLUMN {column}" for column in columns)
        op.execute(f"ALTER TABLE {schema}.{table} {drops}")
