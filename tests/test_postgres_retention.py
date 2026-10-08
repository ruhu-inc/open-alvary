"""Retention purges transient enforcement state and never recovery tombstones or history."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.admission import AdmissionStore
from rights.bytes import ByteLedger, Scope
from rights.database import PolicyNamespace
from rights.retention import purge

pytestmark = pytest.mark.postgres


@pytest.fixture
def ns(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        return PolicyNamespace(conn.scalar(text("SELECT current_schema()")))


def age(engine, ns, table, column, interval, where="true"):
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {ns.qualified(conn, 'policy', table)} SET {column}={column}-interval '{interval}' WHERE {where}"
            )
        )


def count(engine, ns, table):
    with engine.connect() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {ns.qualified(conn, 'policy', table)}"))


def run(engine, ns):
    with engine.begin() as conn:
        return purge(conn, ns, idle_bucket_hours=24, reservation_days=90)


def test_purges_expired_leases_and_idle_refilled_admission_buckets(pg_engine, ns):
    store = AdmissionStore(pg_engine, ns)
    request = {
        "buckets": [{"key": "ip:a", "capacity": 2, "refill_per_minute": 60}],
        "concurrency": [{"key": "ip:a", "limit": 5}],
        "lease_seconds": 30,
    }
    store.admit(request)
    assert run(pg_engine, ns)["expired_leases"] == 0
    age(pg_engine, ns, "admission_lease", "expires_at", "1 hour")
    age(pg_engine, ns, "admission_bucket", "last_refill_at", "2 days")
    assert run(pg_engine, ns) == {
        "expired_leases": 1,
        "idle_admission_buckets": 1,
        "terminal_reservations": 0,
        "idle_byte_buckets": 0,
    }


def test_byte_history_is_kept_until_terminal_and_past_horizon(pg_engine, ns):
    ledger = ByteLedger(pg_engine, ns)
    scopes = [Scope("ip:a", 100, 600)]
    live = ledger.reserve(uuid4(), 10, scopes, 60)
    done = ledger.reserve(uuid4(), 10, scopes, 60)
    ledger.begin_emission(done.reservation_id)
    ledger.settle(done.reservation_id, 10, "receipt")
    age(pg_engine, ns, "byte_reservation", "settled_at", "100 days", "settled_at IS NOT NULL")
    age(pg_engine, ns, "byte_bucket", "last_refill_at", "2 days")
    result = run(pg_engine, ns)
    assert result["terminal_reservations"] == 1 and result["idle_byte_buckets"] == 0  # live one still holds
    ledger.release(live.reservation_id)
    age(
        pg_engine,
        ns,
        "byte_reservation",
        "settled_at",
        "100 days",
        "settled_at IS NOT NULL AND state='released'",
    )
    assert run(pg_engine, ns)["idle_byte_buckets"] == 1
    assert count(pg_engine, ns, "byte_reservation") == 0


def test_recent_terminal_reservations_and_tombstones_are_never_purged(pg_engine, ns):
    ledger = ByteLedger(pg_engine, ns)
    r = ledger.reserve(uuid4(), 10, [Scope("ip:b", 100, 600)], 60)
    ledger.release(r.reservation_id)
    with pg_engine.begin() as conn:
        fn = ns.qualified(conn, "policy", "record_denial")
        conn.execute(text(f"SELECT {fn}('erasure', ARRAY['synthetic'], true)"))
    # Delivery records cannot even be backdated (append-only); retention never touches them.
    run(pg_engine, ns)
    assert count(pg_engine, ns, "byte_reservation") == 1 and count(pg_engine, ns, "denial_delivery") == 1


def test_horizons_are_required_and_bounded(pg_engine, ns):
    with pytest.raises(ValueError), pg_engine.begin() as conn:
        purge(conn, ns, idle_bucket_hours=0, reservation_days=90)
