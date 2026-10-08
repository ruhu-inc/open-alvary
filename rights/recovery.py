"""SYS-07 serving epochs, restore recovery and conservative reset (maintenance authority).

Public serving requires an open epoch bound to this exact database instance. Restores and
clones change the instance, so they serve nothing until ``recover`` has verified the journal,
evaluated the continuity predicate, replayed denials since the backup checkpoint and rebound
the instance. If continuity cannot be proven, every collection stays denied until a reviewer
records a fresh decision revision; no operator command can reopen it.

Each function owns its transactions (taking an Engine) because the journal is outside the
database: markers are appended between committed database steps, never inside one.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from operations.journal import Event
from rights.database import PolicyNamespace

EPOCH_LOCK = 0x4F41_4550
CLOSE_NAMESPACE = uuid5(NAMESPACE_URL, "https://open.alvary.ai/journal/epoch-close")


class RecoveryRequired(RuntimeError):
    """This database instance has not been recovered or rebound; serving stays closed."""


class EpochRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryResult:
    recovery_id: UUID
    decision: str
    predicate: dict


def close_event_id(epoch_id: UUID) -> UUID:
    return uuid5(CLOSE_NAMESPACE, str(epoch_id))


def all_events(journal):
    events, after = [], 0
    while batch := journal.read(after=after, limit=1000):
        events.extend(batch)
        after = batch[-1][0].sequence
    return events


class Tables:
    def __init__(self, conn: Connection, ns: PolicyNamespace):
        q = lambda name, kind="policy": ns.qualified(conn, kind, name)  # noqa: E731
        self.epoch, self.binding, self.record = (
            q("serving_epoch"),
            q("instance_binding"),
            q("recovery_record"),
        )
        self.floor, self.delivery = q("recovery_floor"), q("denial_delivery")
        self.revision, self.head = q("publication_revision"), q("revision_head")
        self.metadata, self.staged = q("metadata_projection", "corpus"), q("staged_artifact", "staging")
        self.binding_table = q("version_binding")
        base = conn.dialect.identifier_preparer.quote_identifier(ns.base)
        self.collections = f"{base}.identity_collections"


def instance(conn) -> tuple:
    return tuple(
        conn.execute(
            text(
                "SELECT (SELECT system_identifier FROM pg_catalog.pg_control_system()),"
                " (SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())"
            )
        ).one()
    )


def bound(conn, t: Tables) -> bool:
    row = conn.execute(text(f"SELECT system_identifier,database_oid FROM {t.binding}")).one_or_none()
    return row is not None and tuple(row) == instance(conn)


def publication_revision(conn, t: Tables, lock=False) -> int:
    suffix = " FOR UPDATE" if lock else ""
    return conn.scalar(text(f"SELECT revision FROM {t.revision} WHERE singleton{suffix}"))


def marker(epoch_id: UUID, kind: str, revision: int, event_id: UUID) -> Event:
    return Event(
        event_id=event_id,
        kind=kind,
        affected=(str(epoch_id),),
        revision=revision,
        revision_domain="publication",
        occurred_at=datetime.now(UTC),
    )


def open_epoch(engine: Engine, ns: PolicyNamespace, journal) -> UUID:
    """Commit 'opening', persist the journal open marker, then commit 'open'."""
    with engine.begin() as conn:
        t = Tables(conn, ns)
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": EPOCH_LOCK})
        if not bound(conn, t):
            raise RecoveryRequired("Database instance requires recovery before serving")
        if conn.scalar(text(f"SELECT count(*) FROM {t.delivery} WHERE durability='pending'")):
            raise EpochRefused("Deliver pending denials before opening a serving epoch")
        active = conn.execute(
            text(f"SELECT epoch_id,state FROM {t.epoch} WHERE state IN ('opening','open')")
        ).one_or_none()
        if active is not None and active.state == "open":
            raise EpochRefused("A serving epoch is already open")
        epoch_id = active.epoch_id if active is not None else uuid4()
        if active is None:
            system_identifier, oid = instance(conn)
            conn.execute(
                text(
                    f"INSERT INTO {t.epoch}(epoch_id,system_identifier,database_oid,state)"
                    " VALUES (:id,:sid,:oid,'opening')"
                ),
                {"id": epoch_id, "sid": system_identifier, "oid": oid},
            )
        revision = publication_revision(conn, t)
    # Retry-safe: the open marker's event ID is the epoch ID.
    receipt = journal.append(marker(epoch_id, "epoch_open", revision, epoch_id))
    with engine.begin() as conn:
        t = Tables(conn, ns)
        conn.execute(
            text(
                f"UPDATE {t.epoch} SET state='open',open_sequence=:seq WHERE epoch_id=:id AND state='opening'"
            ),
            {"seq": receipt.sequence, "id": epoch_id},
        )
    return epoch_id


def close_epoch(engine: Engine, ns: PolicyNamespace, journal) -> UUID:
    """Clean close: fence denials, require none pending, journal the final checkpoint."""
    with engine.begin() as conn:
        t = Tables(conn, ns)
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": EPOCH_LOCK})
        # Holding the allocator row fences new denials until this close commits.
        revision = publication_revision(conn, t, lock=True)
        if conn.scalar(text(f"SELECT count(*) FROM {t.delivery} WHERE durability='pending'")):
            raise EpochRefused("Deliver pending denials before closing the serving epoch")
        epoch_id = conn.scalar(text(f"SELECT epoch_id FROM {t.epoch} WHERE state='open'"))
        if epoch_id is None:
            raise EpochRefused("No open serving epoch")
        receipt = journal.append(marker(epoch_id, "epoch_close", revision, close_event_id(epoch_id)))
        conn.execute(
            text(
                f"UPDATE {t.epoch} SET state='closed',closed_at=statement_timestamp(),"
                " close_sequence=:seq,close_revision=:rev WHERE epoch_id=:id"
            ),
            {"seq": receipt.sequence, "rev": revision, "id": epoch_id},
        )
    return epoch_id


def recover(
    engine: Engine, ns: PolicyNamespace, journal, checkpoint: dict | None, reason: str
) -> RecoveryResult:
    """Reconcile a restored or cloned database with the journal before any serving epoch."""
    reason = reason.strip()
    if not 1 <= len(reason) <= 2048:
        raise ValueError("Bounded recovery reason required")
    watermark, head_chain = journal.high_watermark()  # verifies the whole chain
    events = all_events(journal)
    start = (checkpoint or {}).get("journal_sequence") or 0
    with engine.begin() as conn:
        t = Tables(conn, ns)
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": EPOCH_LOCK})
        conn.execute(text(f"LOCK TABLE {t.revision} IN EXCLUSIVE MODE"))
        predicate = continuity(conn, t, journal, events, checkpoint, start, watermark)
        decision = "continuity_verified" if all(predicate["checks"].values()) else "conservative_reset"
        recovery_id = uuid4()
        system_identifier, oid = instance(conn)
        conn.execute(
            text(
                f"INSERT INTO {t.record}(id,system_identifier,database_oid,decision,journal_sequence,"
                "journal_chain,predicate,reason) VALUES (:id,:sid,:oid,:decision,:seq,:chain,"
                "CAST(:predicate AS jsonb),:reason)"
            ),
            {
                "id": recovery_id,
                "sid": system_identifier,
                "oid": oid,
                "decision": decision,
                "seq": watermark,
                "chain": head_chain,
                "predicate": json.dumps(predicate, sort_keys=True),
                "reason": reason,
            },
        )
        replay(conn, t, [e for r, e in events if r.sequence > start], recovery_id)
        reconcile_pending(conn, t, journal)
        if decision == "conservative_reset":
            conn.execute(
                text(
                    f"INSERT INTO {t.floor}(collection_id,recovery_id,revision)"
                    f" SELECT c.id,:rid,coalesce(h.revision,0) FROM {t.collections} c"
                    f" LEFT JOIN {t.head} h ON h.kind='collection' AND h.scope_id=c.id"
                    " ON CONFLICT DO NOTHING"
                ),
                {"rid": recovery_id},
            )
        # Allocator floor above every revision the journal has seen; new feed epoch follows.
        floor = max([e.revision for _, e in events if e.revision_domain == "publication"], default=0)
        conn.execute(text(f"UPDATE {t.revision} SET revision=greatest(revision,:floor)"), {"floor": floor})
        conn.execute(
            text(
                f"UPDATE {t.epoch} SET state='abandoned',closed_at=statement_timestamp()"
                " WHERE state IN ('opening','open')"
            )
        )
        conn.execute(
            text(
                f"INSERT INTO {t.binding}(singleton,system_identifier,database_oid) VALUES (true,:sid,:oid)"
                " ON CONFLICT (singleton) DO UPDATE SET system_identifier=excluded.system_identifier,"
                " database_oid=excluded.database_oid,bound_at=statement_timestamp()"
            ),
            {"sid": system_identifier, "oid": oid},
        )
    return RecoveryResult(recovery_id, decision, predicate)


def continuity(conn, t, journal, events, checkpoint, start, watermark) -> dict:
    """SYS-07 continuity predicate; every input is recorded in the recovery record."""
    by_id = {e.event_id: (r, e) for r, e in events}
    checks = {}
    chain_at = next((r.chain_sha256 for r, _ in events if r.sequence == start), None)
    expected_chain = (checkpoint or {}).get("journal_chain")
    checks["checkpoint_in_journal"] = start <= watermark and (
        start == 0 or expected_chain is None or chain_at == expected_chain
    )
    restored = conn.execute(
        text(f"SELECT epoch_id,state FROM {t.epoch} WHERE state IN ('opening','open')")
    ).all()
    later = [e.event_id for r, e in events if e.kind == "epoch_open" and r.sequence > start]
    # An 'opening' epoch without a journal marker never served. Every epoch that was open in the
    # backup, or opened after it, needs a durable clean-close marker; otherwise it was unclean.
    must_close = [r.epoch_id for r in restored if r.state == "open" or r.epoch_id in by_id] + later
    unclean = [str(epoch) for epoch in dict.fromkeys(must_close) if close_event_id(epoch) not in by_id]
    checks["epochs_cleanly_closed"] = not unclean
    pending = conn.execute(
        text(
            f"SELECT event_id,kind,affected,revision,revision_domain,occurred_at,erasure"
            f" FROM {t.delivery} WHERE durability='pending'"
        )
    ).all()
    unmatched = [
        str(row.event_id)
        for row in pending
        if (receipt := journal.receipt(row.event_id)) is None or receipt.payload_sha256 != payload_hash(row)
    ]
    checks["pending_denials_journaled"] = not unmatched
    return {
        "checks": checks,
        "backup_checkpoint": start,
        "journal_watermark": watermark,
        "unclean_epochs": unclean,
        "unmatched_pending": unmatched,
    }


def as_event(row) -> Event:
    return Event(
        event_id=row.event_id,
        kind=row.kind,
        affected=tuple(row.affected),
        revision=row.revision,
        revision_domain=row.revision_domain,
        occurred_at=row.occurred_at,
        erasure=row.erasure,
    )


def payload_hash(row) -> str:
    return hashlib.sha256(as_event(row).canonical()).hexdigest()


def replay(conn, t, events, recovery_id):
    """Apply every journal denial idempotently; restoring access needs a fresh decision."""
    for event in events:
        if event.kind not in {"publication_deny", "erasure"}:
            continue
        for scope in event.affected:
            collection = conn.scalar(
                text(
                    f"SELECT id FROM {t.collections} WHERE id=:s UNION ALL"
                    f" SELECT collection_id FROM {t.binding_table} WHERE version_id=:s LIMIT 1"
                ),
                {"s": scope},
            )
            if collection is not None:
                conn.execute(
                    text(
                        f"INSERT INTO {t.floor}(collection_id,recovery_id,revision)"
                        f" SELECT :c,:rid,coalesce((SELECT revision FROM {t.head}"
                        " WHERE kind='collection' AND scope_id=:c),0) ON CONFLICT DO NOTHING"
                    ),
                    {"c": collection, "rid": recovery_id},
                )
            conn.execute(
                text(f"UPDATE {t.metadata} SET suspended=true WHERE work_id=:s AND NOT suspended"),
                {"s": scope},
            )
            if event.erasure:
                conn.execute(
                    text(
                        f"UPDATE {t.staged} SET state='erased',private_bytes=NULL"
                        " WHERE id::text=:s AND private_bytes IS NOT NULL"
                    ),
                    {"s": scope},
                )


def reconcile_pending(conn, t, journal):
    """Pending records already in the journal become durable with their original receipt."""
    rows = conn.execute(
        text(
            f"SELECT event_id,kind,affected,revision,revision_domain,occurred_at,erasure"
            f" FROM {t.delivery} WHERE durability='pending'"
        )
    ).all()
    for row in rows:
        receipt = journal.receipt(row.event_id)
        if receipt is not None and receipt.payload_sha256 == payload_hash(row):
            conn.execute(
                text(
                    f"UPDATE {t.delivery} SET durability='durable',journal_sequence=:seq,"
                    " journal_chain=:chain,durable_at=statement_timestamp() WHERE event_id=:id"
                ),
                {"seq": receipt.sequence, "chain": receipt.chain_sha256, "id": row.event_id},
            )


def main(argv=None) -> int:
    import argparse
    import os
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from operations.journal_postgres import open_journal

    parser = argparse.ArgumentParser(description="Serving epochs and restore recovery (maintenance)")
    parser.add_argument("action", choices=["restore-grants", "recover", "open", "close"])
    parser.add_argument(
        "--journal", type=Path, help="Reference file journal; ALVARY_JOURNAL_DATABASE_URL selects production"
    )
    parser.add_argument("--schema", default="public")
    parser.add_argument("--backup-metadata", type=Path, help="recover: the backup's .json sidecar")
    parser.add_argument("--reason", help="recover: incident reference and reason")
    args = parser.parse_args(argv)
    raw = os.environ.get("DATABASE_URL")
    url = make_url(raw) if raw else None
    if url is None or url.drivername != "postgresql+psycopg":
        print(json.dumps({"status": "unavailable"}))
        return 2
    engine = create_engine(url, poolclass=NullPool)
    ns = PolicyNamespace(args.schema)
    try:
        journal = open_journal(args.journal)
        if args.action == "restore-grants":
            from rights.database import restore_grants

            with engine.begin() as conn:
                restore_grants(conn, ns)
            result = {"grants": "restored"}
        elif args.action == "open":
            result = {"epoch_id": str(open_epoch(engine, ns, journal))}
        elif args.action == "close":
            result = {"epoch_id": str(close_epoch(engine, ns, journal))}
        else:
            if args.backup_metadata is None or not args.reason:
                raise ValueError("recover needs --backup-metadata and --reason")
            checkpoint = json.loads(args.backup_metadata.read_text())["journal_checkpoint"]
            outcome = recover(engine, ns, journal, checkpoint, args.reason)
            result = {
                "recovery_id": str(outcome.recovery_id),
                "decision": outcome.decision,
                **outcome.predicate,
            }
    except (RecoveryRequired, EpochRefused, ValueError) as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 1
    except Exception:
        print(json.dumps({"status": "unavailable"}))
        return 2
    finally:
        engine.dispose()
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
