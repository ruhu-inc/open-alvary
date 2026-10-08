"""BUD absolute-byte ledger: reservation states, conservation and reconciliation drills."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

from rights.bytes import ByteLedger, LedgerConflict, Scope
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
SLOW = 0.000001  # refill so slow that tests observe the ledger without refill noise


@pytest.fixture
def ledger(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return ByteLedger(pg_engine, ns)


def bucket(ledger, key):
    with ledger.engine.connect() as conn:
        table = ledger.namespace.qualified(conn, "policy", "byte_bucket")
        row = conn.execute(
            text(f"SELECT capacity,held,spent FROM {table} WHERE scope_key=:k"), {"k": key}
        ).one()
        return row.capacity, row.held, round(float(row.spent))


def expire(ledger, reservation_id, seconds=60):
    with ledger.engine.begin() as conn:
        table = ledger.namespace.qualified(conn, "policy", "byte_reservation")
        conn.execute(
            text(
                f"UPDATE {table} SET lease_expires_at=statement_timestamp()-make_interval(secs=>:s),"
                " ambiguous_at=CASE WHEN ambiguous_at IS NULL THEN NULL"
                " ELSE statement_timestamp()-make_interval(secs=>:s) END WHERE reservation_id=:r"
            ),
            {"s": seconds, "r": reservation_id},
        )


def state(ledger, reservation_id):
    with ledger.engine.connect() as conn:
        table = ledger.namespace.qualified(conn, "policy", "byte_reservation")
        return conn.execute(
            text(
                f"SELECT state,settled_bytes,refunded_bytes,settlement_mode FROM {table} WHERE reservation_id=:r"
            ),
            {"r": reservation_id},
        ).one()


def scopes(capacity=100, global_capacity=1000):
    return [Scope("ip:a", capacity, SLOW), Scope("global:bytes", global_capacity, SLOW)]


def test_receipt_settlement_conserves_bytes_across_every_scope(ledger):
    r = ledger.reserve(uuid4(), 100, scopes(), 30)
    assert bucket(ledger, "ip:a") == (100, 100, 0) and bucket(ledger, "global:bytes") == (1000, 100, 0)
    assert ledger.begin_emission(r.reservation_id)
    ledger.settle(r.reservation_id, 40, "receipt-1")
    assert bucket(ledger, "ip:a") == (100, 0, 40) and bucket(ledger, "global:bytes") == (1000, 0, 40)
    assert tuple(state(ledger, r.reservation_id)) == ("settled", 40, 60, "receipt")
    ledger.settle(r.reservation_id, 40, "receipt-1")  # identical finalization is a no-op
    with pytest.raises(LedgerConflict):
        ledger.settle(r.reservation_id, 41, "receipt-1")
    with pytest.raises(LedgerConflict):
        ledger.release(r.reservation_id)
    assert bucket(ledger, "ip:a") == (100, 0, 40)


def test_insufficient_bytes_are_refused_without_charge(ledger):
    ledger.reserve(uuid4(), 80, scopes(), 30)
    refused = ledger.reserve(uuid4(), 30, scopes(), 30)
    assert not refused.reserved and refused.blocked == ("ip:a",) and refused.retry_after >= 1
    assert bucket(ledger, "ip:a") == (100, 80, 0)


def test_one_admission_shares_one_reservation(ledger):
    admission = uuid4()
    first = ledger.reserve(admission, 50, scopes(), 30)
    again = ledger.reserve(admission, 50, scopes(), 30)
    assert first.reservation_id == again.reservation_id and bucket(ledger, "ip:a")[1] == 50


def test_capacity_changes_preserve_held_and_spent(ledger):
    # Specification example: C=100, H=90; cut C to 50, settle a 60-byte reservation unsent.
    a = ledger.reserve(uuid4(), 60, scopes(), 30)
    ledger.reserve(uuid4(), 30, scopes(), 30)
    assert not ledger.reserve(uuid4(), 1, scopes(capacity=50), 30).reserved
    ledger.release(a.reservation_id)
    assert bucket(ledger, "ip:a") == (50, 30, 0)
    assert ledger.reserve(uuid4(), 20, scopes(capacity=50), 30).reserved
    assert not ledger.reserve(uuid4(), 1, scopes(capacity=50), 30).reserved


def test_crash_before_emit_releases_on_reconciliation(ledger):
    r = ledger.reserve(uuid4(), 70, scopes(), 30)
    expire(ledger, r.reservation_id)
    assert not ledger.begin_emission(r.reservation_id)  # expired: a late worker cannot emit
    assert ledger.reconcile(fence_seconds=5, conservative_seconds=3600)["released"] == 1
    assert bucket(ledger, "ip:a") == (100, 0, 0)
    assert state(ledger, r.reservation_id).state == "released"


def test_crash_mid_emit_becomes_ambiguous_then_settles_conservatively(ledger):
    r = ledger.reserve(uuid4(), 70, scopes(), 30)
    assert ledger.begin_emission(r.reservation_id)
    expire(ledger, r.reservation_id, seconds=2)
    assert ledger.reconcile(fence_seconds=10, conservative_seconds=3600)["ambiguous"] == 0  # not yet fenced
    expire(ledger, r.reservation_id, seconds=60)
    assert ledger.reconcile(fence_seconds=10, conservative_seconds=3600)["ambiguous"] == 1
    with pytest.raises(LedgerConflict):
        ledger.release(r.reservation_id)  # ambiguous bytes are never refunded
    with pytest.raises(LedgerConflict):
        ledger.settle(r.reservation_id, 0, "late-worker")
    expire(ledger, r.reservation_id, seconds=7200)
    assert ledger.reconcile(fence_seconds=10, conservative_seconds=3600)["conservative"] == 1
    assert tuple(state(ledger, r.reservation_id)) == ("settled", 70, 0, "conservative")
    assert bucket(ledger, "ip:a") == (100, 0, 70)


def test_ambiguous_reservation_accepts_durable_evidence_once(ledger):
    r = ledger.reserve(uuid4(), 70, scopes(), 30)
    ledger.begin_emission(r.reservation_id)
    expire(ledger, r.reservation_id)
    ledger.reconcile(fence_seconds=10, conservative_seconds=3600)
    ledger.reconcile_reservation(r.reservation_id, 25, "transport-log-9")
    ledger.reconcile_reservation(r.reservation_id, 25, "transport-log-9")
    assert tuple(state(ledger, r.reservation_id)) == ("settled", 25, 45, "reconciled")
    assert bucket(ledger, "ip:a") == (100, 0, 25)


def test_parallel_reservations_never_exceed_capacity(ledger):
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda _: ledger.reserve(uuid4(), 30, scopes(), 30), range(20)))
    assert sum(r.reserved for r in results) == 3
    assert bucket(ledger, "ip:a") == (100, 90, 0) and bucket(ledger, "global:bytes")[1] == 90


def test_reconciliation_bounds_are_required(ledger):
    from rights.bytes import LedgerUnavailable

    with pytest.raises(LedgerUnavailable):
        ledger.reconcile(fence_seconds=-1, conservative_seconds=0)


def test_serving_role_may_reserve_but_not_reconcile(foundation):
    f = foundation
    with f.as_role("serving") as conn:
        fn = f.name(conn, "policy", "reserve_bytes")
        request = json.dumps(
            {
                "admission_id": str(uuid4()),
                "bytes": 5,
                "lease_seconds": 30,
                "scopes": [{"key": "ip:z", "capacity": 10, "refill_per_minute": 1}],
            }
        )
        assert conn.scalar(text(f"SELECT {fn}(CAST(:r AS jsonb))"), {"r": request})["reserved"]
    with pytest.raises(DBAPIError) as denied, f.as_role("serving") as conn:
        conn.execute(text(f"SELECT {f.name(conn, 'policy', 'reconcile_bytes')}(1,1)"))
    assert denied.value.orig.sqlstate == "42501"
