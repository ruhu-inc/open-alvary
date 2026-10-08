"""Production denial-journal backend: an append-only table in a separate PostgreSQL database.

Deploy it on a different host/provider from the corpus database (an independent failure
domain, SYS-07). The table is append-only even for its owner (a trigger rejects UPDATE,
DELETE and TRUNCATE); the runtime writer role gets only SELECT and INSERT. Sequences are
assigned under a transaction advisory lock, so the journal, not the client, decides order.
It passes the same conformance drills as the reference FileJournal.
"""

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from operations.journal import GENESIS, Event, JournalConflict, JournalCorrupt, JournalError, Receipt, chain

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS journal_event (
    sequence bigint PRIMARY KEY CHECK (sequence > 0),
    event_id uuid NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    chain_sha256 text NOT NULL CHECK (chain_sha256 ~ '^[a-f0-9]{64}$'),
    appended_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE OR REPLACE FUNCTION journal_event_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path TO 'pg_catalog' AS $$
BEGIN
    RAISE EXCEPTION 'The denial journal is append-only' USING ERRCODE = '42501';
END $$;
DROP TRIGGER IF EXISTS journal_event_append_only ON journal_event;
CREATE TRIGGER journal_event_append_only BEFORE UPDATE OR DELETE ON journal_event
    FOR EACH ROW EXECUTE FUNCTION journal_event_append_only();
DROP TRIGGER IF EXISTS journal_event_no_truncate ON journal_event;
CREATE TRIGGER journal_event_no_truncate BEFORE TRUNCATE ON journal_event
    FOR EACH STATEMENT EXECUTE FUNCTION journal_event_append_only();
ALTER TABLE journal_event ENABLE ALWAYS TRIGGER journal_event_append_only;
ALTER TABLE journal_event ENABLE ALWAYS TRIGGER journal_event_no_truncate;
REVOKE ALL ON journal_event FROM PUBLIC;
"""


def install(engine: Engine, writer_role: str | None = None) -> None:
    """Administrator setup; optionally grant the least-privilege writer role."""
    with engine.begin() as conn:
        conn.exec_driver_sql(SCHEMA_SQL)
        if writer_role:
            quote = conn.dialect.identifier_preparer.quote_identifier
            conn.exec_driver_sql(f"GRANT SELECT, INSERT ON journal_event TO {quote(writer_role)}")


def _record(row) -> tuple[Receipt, Event]:
    event = Event.from_json(row.payload)
    return Receipt(row.sequence, event.event_id, row.payload_sha256, row.chain_sha256), event


class PostgresJournal:
    def __init__(self, engine: Engine):
        self.engine = engine

    def _rows(self, conn, after=0, limit=None):
        sql = "SELECT sequence,payload,payload_sha256,chain_sha256 FROM journal_event WHERE sequence > :after ORDER BY sequence"
        if limit is not None:
            sql += " LIMIT :limit"
        return conn.execute(text(sql), {"after": after, "limit": limit}).all()

    def _verified(self, conn):
        """Verify the entire chain; any gap, edit or reorder fails closed."""
        previous, records = GENESIS, []
        for expected, row in enumerate(self._rows(conn), start=1):
            receipt, event = _record(row)
            payload = hashlib.sha256(event.canonical()).hexdigest()
            if (
                row.sequence != expected
                or payload != row.payload_sha256
                or row.chain_sha256 != chain(previous, row.sequence, payload)
            ):
                raise JournalCorrupt("Journal chain broken")
            previous = row.chain_sha256
            records.append((receipt, event))
        return records, previous

    def append(self, event: Event) -> Receipt:
        payload = hashlib.sha256(event.canonical()).hexdigest()
        try:
            with self.engine.begin() as conn:
                # An EXCLUSIVE table lock requires UPDATE/DELETE/TRUNCATE privileges,
                # which the append-only writer deliberately lacks. The database-scoped
                # relation OID gives all backend instances the same transaction mutex
                # without granting mutation privileges; commit/rollback releases it.
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock('journal_event'::regclass::oid::bigint)")
                existing = conn.execute(
                    text(
                        "SELECT sequence,payload,payload_sha256,chain_sha256 FROM journal_event WHERE event_id=:id"
                    ),
                    {"id": event.event_id},
                ).one_or_none()
                if existing is not None:
                    if existing.payload_sha256 != payload:
                        raise JournalConflict("Event ID reused with a different payload")
                    return _record(existing)[0]
                last = conn.execute(
                    text("SELECT sequence,chain_sha256 FROM journal_event ORDER BY sequence DESC LIMIT 1")
                ).one_or_none()
                sequence = (last.sequence if last else 0) + 1
                previous = last.chain_sha256 if last else GENESIS
                receipt = Receipt(sequence, event.event_id, payload, chain(previous, sequence, payload))
                conn.execute(
                    text(
                        "INSERT INTO journal_event(sequence,event_id,payload,payload_sha256,chain_sha256)"
                        " VALUES (:s,:id,CAST(:p AS jsonb),:h,:c)"
                    ),
                    {
                        "s": sequence,
                        "id": event.event_id,
                        "p": json.dumps(event.as_json()),
                        "h": payload,
                        "c": receipt.chain_sha256,
                    },
                )
                return receipt
        except SQLAlchemyError:
            raise JournalError("Journal unavailable") from None

    def read(self, after: int = 0, limit: int = 1000) -> list[tuple[Receipt, Event]]:
        if after < 0 or not 1 <= limit <= 10000:
            raise JournalError("Invalid journal range")
        try:
            with self.engine.connect() as conn:
                records, _ = self._verified(conn)
        except SQLAlchemyError:
            raise JournalError("Journal unavailable") from None
        return records[after : after + limit]

    def receipt(self, event_id) -> Receipt | None:
        try:
            with self.engine.connect() as conn:
                records, _ = self._verified(conn)
        except SQLAlchemyError:
            raise JournalError("Journal unavailable") from None
        return next((r for r, _ in records if r.event_id == event_id), None)

    def high_watermark(self) -> tuple[int, str]:
        try:
            with self.engine.connect() as conn:
                records, previous = self._verified(conn)
        except SQLAlchemyError:
            raise JournalError("Journal unavailable") from None
        return len(records), previous


def open_journal(path=None):
    """ALVARY_JOURNAL_DATABASE_URL selects the production backend; otherwise the file reference."""
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from operations.journal import FileJournal

    raw = os.environ.get("ALVARY_JOURNAL_DATABASE_URL")
    if raw:
        url = make_url(raw)
        if url.drivername != "postgresql+psycopg":
            raise JournalError("Journal database URL must use postgresql+psycopg")
        return PostgresJournal(create_engine(url, poolclass=NullPool))
    if path is None:
        raise JournalError("Configure ALVARY_JOURNAL_DATABASE_URL or a reference journal directory")
    return FileJournal(path)
