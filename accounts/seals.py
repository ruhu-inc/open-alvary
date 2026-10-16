"""Daily seals of the audit log (MAC-13.7).

Each UTC day's entries are sealed once the day has been over for an hour (so a transaction begun
before midnight has committed): their count, first and last sequence numbers, and a SHA-256 over each
entry's canonical form in sequence order. A seal's own hash also covers the previous day's, so
changing, removing or back-dating an entry, or dropping a whole day, breaks that day's seal and the
chain after it. Days without entries are sealed too, so the chain has no gaps.

Seals are kept in accounts.audit_seal, which can be added to but never changed, and each new seal is
also written to the job's log, the copy held outside the database (runbook: an audit-seals log
bucket). Verification recomputes every day still held. Days past the two-year retention, whose
entries the purge removed, are checked through the chain alone.

  python -m accounts.seals seal      seal every day that has ended and is not yet sealed (run daily)
  python -m accounts.seals verify    check the chain and every day still held; exits 1 on a problem
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import text

from accounts.store import Accounts

GRACE = timedelta(hours=1)
VERSION = "audit-seal-1"
COLUMNS = (
    "seq, id, at, actor_type, actor_ref, organisation_id, action, target_type, target_id, summary,"
    " network_prefix, client_family, request_id"
)


def canonical(entry) -> bytes:
    """One entry as stable bytes: every column, keys sorted, time in UTC to the microsecond."""
    fields = dict(entry._mapping)
    fields["at"] = fields["at"].astimezone(UTC).isoformat(timespec="microseconds")
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def seal_hash(
    previous: str | None, day: date, entries: int, first: int | None, last: int | None, digest: str
) -> str:
    material = "|".join(
        (VERSION, previous or "", day.isoformat(), str(entries), str(first or ""), str(last or ""), digest)
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


def _digest(conn, table: str, day: date) -> dict:
    start, end = _bounds(day)
    rows = conn.execute(
        text(f"SELECT {COLUMNS} FROM {table} WHERE at >= :start AND at < :end ORDER BY seq"),
        {"start": start, "end": end},
    ).all()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical(row) + b"\n")
    return {
        "entries": len(rows),
        "first_seq": rows[0].seq if rows else None,
        "last_seq": rows[-1].seq if rows else None,
        "entries_hash": digest.hexdigest(),
    }


def _now(conn, now: datetime | None) -> datetime:
    return now or conn.scalar(text("SELECT statement_timestamp()"))


def last_sealable(now: datetime) -> date:
    """The latest day that ended at least GRACE ago."""
    return (now.astimezone(UTC) - GRACE).date() - timedelta(days=1)


def seal(accounts: Accounts, now: datetime | None = None, out=sys.stdout) -> list[dict]:
    """Seal every ended day after the last seal (or from the first entry's day), in order."""
    sealed = []
    with accounts.engine.begin() as conn:
        seals, log = accounts._t(conn, "audit_seal"), accounts._t(conn, "audit_event")
        conn.execute(text(f"LOCK TABLE {seals} IN SHARE ROW EXCLUSIVE MODE"))  # one sealer at a time
        now = _now(conn, now)
        last = conn.execute(text(f"SELECT day, seal_hash FROM {seals} ORDER BY day DESC LIMIT 1")).first()
        if last is not None:
            day, previous = last.day + timedelta(days=1), last.seal_hash
        else:
            first = conn.scalar(text(f"SELECT min(at) FROM {log}"))
            if first is None:
                return []
            day, previous = first.astimezone(UTC).date(), None
        while day <= last_sealable(now):
            row = {"day": day, **_digest(conn, log, day), "previous_hash": previous}
            row["seal_hash"] = seal_hash(
                previous, day, row["entries"], row["first_seq"], row["last_seq"], row["entries_hash"]
            )
            conn.execute(
                text(
                    f"INSERT INTO {seals} (day, entries, first_seq, last_seq, entries_hash, previous_hash,"
                    " seal_hash) VALUES (:day, :entries, :first_seq, :last_seq, :entries_hash,"
                    " :previous_hash, :seal_hash)"
                ),
                row,
            )
            sealed.append(row | {"day": day.isoformat()})
            previous, day = row["seal_hash"], day + timedelta(days=1)
    for row in sealed:
        # The copy outside the database: one structured line per seal, kept by the log bucket.
        print(json.dumps({"severity": "NOTICE", "message": "audit seal", "audit_seal": row}), file=out)
    return sealed


def verify(accounts: Accounts, now: datetime | None = None) -> dict:
    """Recompute the chain and every day still held. Problems are listed by day, never by entry
    content, so the report itself holds no personal data."""
    problems: list[dict] = []
    report = {"days": 0, "past_retention": 0, "unsealed_days": 0}
    with accounts.engine.connect() as conn:
        seals, log = accounts._t(conn, "audit_seal"), accounts._t(conn, "audit_event")
        now = _now(conn, now)
        # The purge removes whole UTC days older than two years (accounts.privacy.purge).
        horizon = conn.scalar(
            text("SELECT date_trunc('day', CAST(:now AS timestamptz) - interval '2 years', 'UTC')"),
            {"now": now},
        )
        previous, expected, first_day = None, None, None
        for stored in conn.execute(text(f"SELECT * FROM {seals} ORDER BY day")).all():
            report["days"] += 1
            day, first_day = stored.day, first_day or stored.day
            if expected is not None and day != expected:
                problems.append({"day": expected.isoformat(), "problem": "missing_seal"})
            if stored.previous_hash != previous:
                problems.append({"day": day.isoformat(), "problem": "chain_broken"})
            recomputed = seal_hash(
                stored.previous_hash,
                day,
                stored.entries,
                stored.first_seq,
                stored.last_seq,
                stored.entries_hash,
            )
            if recomputed != stored.seal_hash:
                problems.append({"day": day.isoformat(), "problem": "seal_altered"})
            held = _digest(conn, log, day)
            if _bounds(day)[1] <= horizon and held["entries"] == 0:
                report["past_retention"] += 1  # purged as the schedule says; the chain still holds
            elif any(
                held[k] != getattr(stored, k) for k in ("entries", "first_seq", "last_seq", "entries_hash")
            ):
                problems.append({"day": day.isoformat(), "problem": "entries_changed"})
            previous, expected = stored.seal_hash, day + timedelta(days=1)
        if first_day is None:
            first = conn.scalar(text(f"SELECT min(at) FROM {log}"))
            expected = first.astimezone(UTC).date() if first is not None else None
        elif conn.scalar(
            text(f"SELECT EXISTS(SELECT 1 FROM {log} WHERE at < :start)"), {"start": _bounds(first_day)[0]}
        ):
            problems.append({"day": first_day.isoformat(), "problem": "entries_before_first_seal"})
        if expected is not None and expected <= last_sealable(now):
            report["unsealed_days"] = (last_sealable(now) - expected).days + 1
        if report["unsealed_days"] > 1:  # the daily job seals before it verifies; one day may wait for it
            problems.append({"day": expected.isoformat(), "problem": "sealing_behind"})
    return report | {"ok": not problems, "problems": problems}


def main(argv=None) -> int:
    from api.store import engine_for
    from changes.worker import accounts_for

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["seal", "verify"])
    command = parser.parse_args(argv).command
    accounts = accounts_for(engine_for())
    if command == "seal":
        print(json.dumps({"message": "audit seals", "sealed": len(seal(accounts))}))
        return 0
    report = verify(accounts)
    # A failed check is an ERROR line for the alert, and a failed job.
    print(
        json.dumps({"severity": "INFO" if report["ok"] else "ERROR", "message": "audit seal check", **report})
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
