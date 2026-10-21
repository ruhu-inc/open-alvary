"""Memberships: dates with their precision, a basis, and suspensions from participation (JUR-04).

corpus.membership's valid_from and valid_to become dates of the schema.dates subset (a day, a month, a
year, or a window [FIRST..LAST] holding the day), each stored as its EDTF string with its inclusive
earliest and latest bounds, which only schema.dates computes; ``basis`` records why the membership starts
when it does (its kind, the instrument's article and the evidence). corpus.membership_suspension holds
the reviewed suspensions from participation, each overlaid on one published membership, which it never
ends, with its legal basis: each article with its role, the one its decision cites (decision_basis) among
them. changes.suspension_state is the last announced form of each, so changes.record.publish announces a
newly published suspension as suspension_recorded, its lifting (valid_to set) as another with the reason
``lifted``, and any other change, or its retraction, as a correction of the latest. The change log's
checks gain that type and that reason, added to whatever they list.

Only reviewed memberships are stored, and a reviewed membership now states its basis, which none
published before this revision does: the upgrade refuses a catalogue that holds any (none has been
published; publish them again from the register after migrating).
"""

import re

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

# schema.dates.PATTERN, as of this revision: YYYY-MM-DD, YYYY-MM, YYYY, or a window [FIRST..LAST] of them
# (never FIRST/LAST, an EDTF interval). Only the shape: schema.dates computes the bounds stored beside it.
_PART = "[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?"
EDTF = rf"^({_PART}|\[{_PART}\.\.{_PART}\])$"
DAY = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
BASES = (
    "entry_into_force",
    "deposit",
    "admission_decision",
    "provisional_application_on_signature",
    "signature",
)
# schema.memberships.ROLES, as of this revision: why a suspension's legal basis cites an article.
ROLES = ("decision_basis", "treaty_rule")
TABLES = """
ALTER TABLE {corpus}.membership
    ALTER COLUMN valid_from TYPE text USING valid_from::text,
    ALTER COLUMN valid_to TYPE text USING valid_to::text,
    ADD COLUMN valid_from_earliest date NOT NULL,
    ADD COLUMN valid_from_latest date NOT NULL,
    ADD COLUMN valid_to_earliest date,
    ADD COLUMN valid_to_latest date,
    ADD COLUMN basis jsonb NOT NULL,
    ADD CONSTRAINT membership_valid_from CHECK (valid_from ~ '{edtf}' AND valid_from_earliest <= valid_from_latest),
    ADD CONSTRAINT membership_valid_to CHECK (
        (valid_to IS NULL) = (valid_to_earliest IS NULL) AND (valid_to IS NULL) = (valid_to_latest IS NULL)
        AND (valid_to IS NULL OR (valid_to ~ '{edtf}' AND valid_to_earliest <= valid_to_latest
                                  AND valid_from_earliest <= valid_to_latest))),
    ADD CONSTRAINT membership_basis CHECK (
        jsonb_typeof(basis) = 'object' AND basis->>'kind' IN ({bases})
        AND char_length(basis->>'pinpoint') BETWEEN 1 AND 300 AND basis->>'evidence_url' ~ '^https://');
CREATE TABLE {corpus}.membership_suspension (
    suspension_id text PRIMARY KEY CHECK (suspension_id
        ~ '^[a-z]{{2}}(-[a-z0-9]{{1,12}})?\\:aa-[a-z0-9]{{1,12}}\\:[1-9][0-9]{{0,2}}\\:s[1-9][0-9]{{0,2}}$'),
    membership_id text NOT NULL REFERENCES {corpus}.membership (membership_id),
    valid_from text NOT NULL CHECK (valid_from ~ '{edtf}'),
    valid_from_earliest date NOT NULL,
    valid_from_latest date NOT NULL,
    valid_to text CHECK (valid_to ~ '{edtf}'),
    valid_to_earliest date,
    valid_to_latest date,
    legal_basis jsonb NOT NULL
        CHECK (jsonb_typeof(legal_basis) = 'array' AND jsonb_array_length(legal_basis) BETWEEN 1 AND 5
               AND jsonb_array_length(jsonb_path_query_array(legal_basis, '$[*] ? ({roles})'))
                   = jsonb_array_length(legal_basis)
               AND jsonb_path_exists(legal_basis, '$[*] ? (@.role == "decision_basis")')),
    evidence_url text NOT NULL CHECK (evidence_url ~ '^https://' AND char_length(evidence_url) <= 2000),
    lifted_evidence_url text
        CHECK (lifted_evidence_url ~ '^https://' AND char_length(lifted_evidence_url) <= 2000),
    note text NOT NULL CHECK (char_length(note) BETWEEN 1 AND 2000),
    reviewed_by text NOT NULL CHECK (char_length(reviewed_by) BETWEEN 1 AND 300),
    reviewed_at timestamptz NOT NULL,
    published_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (left(suspension_id, char_length(membership_id) + 2) = membership_id || '\\:s'),
    CHECK (valid_from_earliest <= valid_from_latest),
    CHECK ((valid_to IS NULL) = (valid_to_earliest IS NULL) AND (valid_to IS NULL) = (valid_to_latest IS NULL)),
    CHECK (valid_to IS NULL OR (valid_to_earliest <= valid_to_latest AND valid_from_earliest <= valid_to_latest
                                AND lifted_evidence_url IS NOT NULL))
);
CREATE INDEX membership_suspension_membership ON {corpus}.membership_suspension (membership_id);
CREATE TABLE {changes}.suspension_state (
    suspension_id text PRIMARY KEY CHECK (char_length(suspension_id) BETWEEN 1 AND 160),
    membership_id text NOT NULL CHECK (char_length(membership_id) BETWEEN 1 AND 160),
    projection jsonb NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    projection_hash text NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{{64}}$'),
    event_id uuid,
    event_revision integer CHECK (event_revision >= 1),
    event_key text CHECK (char_length(event_key) BETWEEN 1 AND 300),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
"""
GRANTS = (
    ("release", "SELECT, INSERT, UPDATE, DELETE", "corpus", "membership_suspension"),
    ("release", "SELECT, INSERT, UPDATE, DELETE", "changes", "suspension_state"),
    ("serving", "SELECT", "corpus", "membership_suspension"),
    ("serving", "SELECT", "changes", "suspension_state"),
    # The guard reads every corpus table (bundle v0003); granted here too, whatever the provisioning order.
    ("guard", "SELECT", "corpus", "membership_suspension"),
)
# What the change log's checks gain: listed after whatever they hold (another revision may add others).
CHECKS = (("event_type", "suspension_recorded"), ("change_reason", "lifted"))


def names():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    schemas = {key: quote(ns.schema(key)) for key in ("corpus", "changes")}
    return conn, ns, schemas, quote


def _listed(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _constraints(conn, table: str, mentioning: str) -> list[tuple[str, str]]:
    """The check constraints of a table whose definition mentions these words: (name, definition)."""
    return [
        (row.conname, row.definition)
        for row in conn.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint"
                " WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
            ),
            {"t": table},
        )
        if mentioning in row.definition
    ]


def _recheck(conn, ns, changes: str, *, add: bool) -> None:
    """Add this revision's event type and change reason to the change log's checks, or remove them."""
    events = ns.qualified(conn, "changes", "change_event")
    for column, value in CHECKS:
        [(name, definition)] = _constraints(conn, events, f"(({column} = ANY")
        values = [v for v in re.findall(r"'([a-z_]+)'::text", definition) if v != value]
        values += [value] if add else []
        op.execute(
            f"ALTER TABLE {changes}.change_event DROP CONSTRAINT {name};"
            f" ALTER TABLE {changes}.change_event ADD CONSTRAINT {name} CHECK ({column} IN ({_listed(values)}))"
        )


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, schemas, quote = names()
    membership = ns.qualified(conn, "corpus", "membership")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {membership})")):
        raise RuntimeError(
            "Published memberships have no basis: publish a register without reviewed memberships with the"
            " previous release, migrate, then review their bases and publish again"
        )
    # The check that compared the two dates as dates; the new ones compare their bounds.
    for name, _ in _constraints(conn, membership, "valid_from"):
        op.execute(f"ALTER TABLE {schemas['corpus']}.membership DROP CONSTRAINT {quote(name)}")
    roles = " || ".join(f'@.role == "{role}"' for role in ROLES)
    op.execute(TABLES.format(edtf=EDTF, bases=_listed(BASES), roles=roles, **schemas))
    _recheck(conn, ns, schemas["changes"], add=True)
    for capability, privileges, schema, table in GRANTS:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"GRANT {privileges} ON {schemas[schema]}.{table} TO {quote(role)}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, schemas, _ = names()
    events = ns.qualified(conn, "changes", "change_event")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {events} WHERE event_type = 'suspension_recorded')")):
        raise RuntimeError("Suspension events exist; a downgrade would break the public change log")
    membership = ns.qualified(conn, "corpus", "membership")
    if conn.scalar(
        text(f"SELECT EXISTS(SELECT 1 FROM {membership} WHERE valid_from !~ :day OR valid_to !~ :day)"),
        {"day": DAY},
    ):
        raise RuntimeError(
            "A published membership's date is not a day; the previous revision stores days only"
        )
    corpus, changes = schemas["corpus"], schemas["changes"]
    _recheck(conn, ns, changes, add=False)
    # The state is derived from the catalogue and the events; the suspensions are in the register file.
    op.execute(f"DROP TABLE {changes}.suspension_state")
    op.execute(f"DROP TABLE {corpus}.membership_suspension")
    op.execute(
        f"ALTER TABLE {corpus}.membership DROP CONSTRAINT membership_valid_from,"
        " DROP CONSTRAINT membership_valid_to, DROP CONSTRAINT membership_basis,"
        " DROP COLUMN valid_from_earliest, DROP COLUMN valid_from_latest, DROP COLUMN valid_to_earliest,"
        " DROP COLUMN valid_to_latest, DROP COLUMN basis,"
        " ALTER COLUMN valid_from TYPE date USING valid_from::date,"
        " ALTER COLUMN valid_to TYPE date USING valid_to::date,"
        " ADD CHECK (valid_to IS NULL OR valid_from <= valid_to)"
    )
