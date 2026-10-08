"""SYS-07 steps 2–3: deliver committed pending denials to the independent journal, in order.

Denials take effect in PostgreSQL at step 1, before any delivery; this module only makes
them durable outside the database. Delivery runs under maintenance authority inside a
caller-owned transaction. A crash after the journal append but before commit leaves the
record pending; the retry appends the same ``event_id`` and receives the original receipt,
so no duplicate or reordered event is created.
"""

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.pool import NullPool

from operations.journal import Event, JournalError
from operations.journal_postgres import open_journal
from rights.database import PolicyNamespace

# One dispatcher at a time keeps journal order equal to committed revision order.
DISPATCH_LOCK = 0x4F41_4A4E


@dataclass(frozen=True)
class DispatchResult:
    delivered: int
    pending: int


def pending_count(conn: Connection, ns: PolicyNamespace) -> int:
    table = ns.qualified(conn, "policy", "denial_delivery")
    return conn.scalar(text(f"SELECT count(*) FROM {table} WHERE durability='pending'"))


def dispatch(conn: Connection, ns: PolicyNamespace, journal, limit: int = 100) -> DispatchResult:
    if not 1 <= limit <= 1000:
        raise ValueError("Bounded dispatch batch required")
    table = ns.qualified(conn, "policy", "denial_delivery")
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": DISPATCH_LOCK})
    # Detect a database that lost history the journal still holds (fences serving; caller commits).
    from rights.recovery import RecoveryRequired, Tables, fence_if_behind_journal

    try:
        fence_if_behind_journal(conn, Tables(conn, ns), journal)
    except (RecoveryRequired, JournalError):
        # Fenced, or the journal is unreachable: denials stay pending and effective.
        return DispatchResult(delivered=0, pending=pending_count(conn, ns))
    rows = conn.execute(
        text(f"""
        SELECT event_id,kind,affected,revision,revision_domain,occurred_at,erasure
        FROM {table} WHERE durability='pending' ORDER BY revision LIMIT :limit
        """),
        {"limit": limit},
    ).all()
    delivered = 0
    for row in rows:
        event = Event(
            event_id=row.event_id,
            kind=row.kind,
            affected=tuple(row.affected),
            revision=row.revision,
            revision_domain=row.revision_domain,
            occurred_at=row.occurred_at,
            erasure=row.erasure,
        )
        try:
            receipt = journal.append(event)
        except JournalError:
            # Stop in order; later events must not overtake an undelivered earlier one.
            break
        conn.execute(
            text(f"""
            UPDATE {table} SET durability='durable',journal_sequence=:sequence,
                journal_chain=:chain,durable_at=statement_timestamp()
            WHERE event_id=:id AND durability='pending'
            """),
            {"sequence": receipt.sequence, "chain": receipt.chain_sha256, "id": row.event_id},
        )
        delivered += 1
    return DispatchResult(delivered=delivered, pending=pending_count(conn, ns))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deliver pending denials to the independent journal")
    parser.add_argument(
        "--journal", type=Path, help="Reference file journal; ALVARY_JOURNAL_DATABASE_URL selects production"
    )
    parser.add_argument("--schema", default="public")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    raw = os.environ.get("DATABASE_URL")
    url = make_url(raw) if raw else None
    if url is None or url.drivername != "postgresql+psycopg":
        print(json.dumps({"status": "unavailable"}))
        return 2
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            result = dispatch(conn, PolicyNamespace(args.schema), open_journal(args.journal), args.limit)
    except Exception:
        print(json.dumps({"status": "unavailable"}))
        return 2
    finally:
        engine.dispose()
    print(json.dumps({"status": "ok", "delivered": result.delivered, "pending": result.pending}))
    return 0 if result.pending == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
