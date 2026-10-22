"""The change log announces every new revision of a public record's rights record (G15): rights_changed.

changes.record.publish already writes each change to a work's rights record as a revision in
changes.rights_revision, in the transaction that makes it; it now also announces the revision, when the
record is public after it, as a rights_changed event naming the revision, the rights record's fields
that changed and the decision taken from it. The change log's event type check gains that type, added
to whatever it lists. No table or grant changes.
"""

import re

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

TYPE = "rights_changed"


def _listed(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _recheck(*, add: bool) -> None:
    """Add this revision's event type to the change log's event type check, or remove it."""
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    events = ns.qualified(conn, "changes", "change_event")
    changes = conn.dialect.identifier_preparer.quote_identifier(ns.schema("changes"))
    [(name, definition)] = [
        (row.conname, row.definition)
        for row in conn.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint"
                " WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
            ),
            {"t": events},
        )
        if "((event_type = ANY" in row.definition
    ]
    values = [v for v in re.findall(r"'([a-z_]+)'::text", definition) if v != TYPE]
    values += [TYPE] if add else []
    op.execute(
        f"ALTER TABLE {changes}.change_event DROP CONSTRAINT {name};"
        f" ALTER TABLE {changes}.change_event ADD CONSTRAINT {name} CHECK (event_type IN ({_listed(values)}))"
    )


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _recheck(add=True)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    events = ns.qualified(conn, "changes", "change_event")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {events} WHERE event_type = :t)"), {"t": TYPE}):
        raise RuntimeError("Rights change events exist; a downgrade would break the public change log")
    _recheck(add=False)
