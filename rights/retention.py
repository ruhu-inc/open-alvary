"""Retention purge for transient enforcement state (owner-delegated D20 policy, 2026-10-07).

Purged: expired admission leases, idle admission buckets that have fully refilled, and
terminal (released/settled) byte reservations older than the horizon, then byte buckets
that are idle, unreferenced and fully refilled. Never purged: denial-delivery records,
journal events, receipts and audit events, which are recovery tombstones or history.
Runs under maintenance authority in a caller-owned transaction; horizons are explicit.
"""

import argparse
import json
import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.pool import NullPool

from rights.database import PolicyNamespace


def purge(
    conn: Connection, ns: PolicyNamespace, *, idle_bucket_hours: float, reservation_days: float
) -> dict:
    if idle_bucket_hours < 1 or reservation_days < 1:
        raise ValueError("Retention horizons must be at least one hour and one day")
    q = lambda name: ns.qualified(conn, "policy", name)  # noqa: E731
    leases = conn.execute(
        text(f"DELETE FROM {q('admission_lease')} WHERE expires_at <= statement_timestamp()")
    )
    buckets = conn.execute(
        text(f"""
        DELETE FROM {q("admission_bucket")} b
        WHERE b.last_refill_at <= statement_timestamp() - make_interval(hours => :h)
            AND b.tokens + extract(epoch FROM statement_timestamp() - b.last_refill_at)
                * b.refill_per_minute / 60 >= b.capacity
            AND NOT EXISTS (SELECT 1 FROM {q("admission_lease")} l
                WHERE l.scope_key = b.scope_key AND l.expires_at > statement_timestamp())
        """),
        {"h": idle_bucket_hours},
    )
    old = text(f"""
        SELECT reservation_id FROM {q("byte_reservation")}
        WHERE state IN ('released','settled') AND settled_at <= statement_timestamp() - make_interval(days => :d)
    """)
    expired = [row[0] for row in conn.execute(old, {"d": reservation_days})]
    if expired:
        conn.execute(
            text(f"DELETE FROM {q('byte_reservation_scope')} WHERE reservation_id = ANY(:ids)"),
            {"ids": expired},
        )
        conn.execute(
            text(f"DELETE FROM {q('byte_reservation')} WHERE reservation_id = ANY(:ids)"), {"ids": expired}
        )
    byte_buckets = conn.execute(
        text(f"""
        DELETE FROM {q("byte_bucket")} b
        WHERE b.held = 0
            AND b.spent - extract(epoch FROM statement_timestamp() - b.last_refill_at) * b.refill_per_minute / 60 <= 0
            AND b.last_refill_at <= statement_timestamp() - make_interval(hours => :h)
            AND NOT EXISTS (SELECT 1 FROM {q("byte_reservation_scope")} s WHERE s.scope_key = b.scope_key)
        """),
        {"h": idle_bucket_hours},
    )
    return {
        "expired_leases": leases.rowcount,
        "idle_admission_buckets": buckets.rowcount,
        "terminal_reservations": len(expired),
        "idle_byte_buckets": byte_buckets.rowcount,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Purge transient enforcement state (maintenance)")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--idle-bucket-hours", type=float, required=True)
    parser.add_argument("--reservation-days", type=float, required=True)
    args = parser.parse_args(argv)
    raw = os.environ.get("DATABASE_URL")
    url = make_url(raw) if raw else None
    if url is None or url.drivername != "postgresql+psycopg":
        print(json.dumps({"status": "unavailable"}))
        return 2
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            result = purge(
                conn,
                PolicyNamespace(args.schema),
                idle_bucket_hours=args.idle_bucket_hours,
                reservation_days=args.reservation_days,
            )
    except ValueError as error:
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
