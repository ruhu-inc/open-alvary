"""ACS-05 admission primitive: debits, refill, rollback, leases and concurrency races."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

from api.admission import AdmissionStore, AdmissionUnavailable
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres


def request(capacity=3, refill=60, concurrency=10, global_limit=100, key="ip:a"):
    return {
        "buckets": [{"key": key, "capacity": capacity, "refill_per_minute": refill}],
        "concurrency": [{"key": key, "limit": concurrency}, {"key": "global:all", "limit": global_limit}],
        "lease_seconds": 30,
    }


@pytest.fixture
def store(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return AdmissionStore(pg_engine, ns)


def tokens(store, key="ip:a"):
    with store.engine.connect() as conn:
        table = store.namespace.qualified(conn, "policy", "admission_bucket")
        return conn.scalar(text(f"SELECT tokens FROM {table} WHERE scope_key=:k"), {"k": key})


def test_bucket_debits_one_per_admission_and_rejection_debits_nothing(store):
    results = [store.admit(request(refill=0.001)) for _ in range(4)]
    assert [r["admitted"] for r in results] == [True, True, True, False]
    assert results[3]["blocked"] == ["ip:a"] and results[3]["retry_after"] >= 1
    before = tokens(store)
    store.admit(request(refill=0.001))
    assert tokens(store) - before < 0.01  # only refill, never a debit


def test_refill_uses_database_time_and_clock_rollback_removes_nothing(store):
    store.admit(request(capacity=5))
    with store.engine.begin() as conn:
        table = store.namespace.qualified(conn, "policy", "admission_bucket")
        conn.execute(
            text(f"UPDATE {table} SET tokens=0, last_refill_at=statement_timestamp()+interval '1 hour'")
        )
    result = store.admit(request(capacity=5))
    assert not result["admitted"] and tokens(store) == 0  # future refill time: nothing added or removed
    with store.engine.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET last_refill_at=statement_timestamp()-interval '2 seconds'"))
    assert store.admit(request(capacity=5))["admitted"]  # 60/min refill restores ~2 tokens


def test_concurrency_and_global_leases_block_until_release_or_expiry(store):
    first = store.admit(request(concurrency=1))
    blocked = store.admit(request(concurrency=1))
    assert blocked["blocked"] == ["ip:a"] and 1 <= blocked["retry_after"] <= 30
    store.release(first["lease_id"])
    second = store.admit(request(concurrency=1))
    assert second["admitted"]
    other = store.admit(request(key="ip:b", global_limit=1))
    assert other["blocked"] == ["global:all"]
    with store.engine.begin() as conn:
        table = store.namespace.qualified(conn, "policy", "admission_lease")
        conn.execute(text(f"UPDATE {table} SET expires_at=statement_timestamp()-interval '1 second'"))
    assert store.admit(request(key="ip:b", global_limit=1))["admitted"]


def test_parallel_arrivals_never_exceed_capacity_or_global_ceiling(store):
    with ThreadPoolExecutor(12) as pool:
        results = list(
            pool.map(
                lambda i: store.admit(request(capacity=5, refill=0.001, key=f"ip:{i % 2}", global_limit=6)),
                range(24),
            )
        )
    admitted = [r for r in results if r["admitted"]]
    assert len(admitted) == 6  # global ceiling, though each identity could admit 5
    assert all(set(r["blocked"]) <= {"ip:0", "ip:1", "global:all"} for r in results if not r["admitted"])


def test_invalid_requests_are_rejected(store):
    bad = request()
    bad["lease_seconds"] = 0
    with pytest.raises(AdmissionUnavailable):
        store.admit(bad)


def test_serving_role_may_admit_but_cannot_read_or_edit_admission_state(foundation):
    f = foundation
    with f.as_role("serving") as conn:
        fn = f.name(conn, "policy", "admit")
        result = conn.scalar(text(f"SELECT {fn}(CAST(:r AS jsonb))"), {"r": json.dumps(request())})
        assert result["admitted"]
    with pytest.raises(DBAPIError) as denied, f.as_role("serving") as conn:
        conn.execute(text(f"UPDATE {f.name(conn, 'policy', 'admission_bucket')} SET tokens=1000"))
    assert denied.value.orig.sqlstate == "42501"


def test_real_app_enforces_admission_from_configuration(pg_engine, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import create_app
    from api.store import Store
    from scripts.seed import seed

    migrate(pg_engine)
    seed(Store(pg_engine))
    settings = dict(
        policy_revision="synthetic-1",
        capacity=2,
        refill_per_minute=0.001,
        identity_concurrency=2,
        global_concurrency=4,
        lease_seconds=30,
        queue_size=1,
        queue_deadline_seconds=0.2,
        trusted_proxies=1,
        hmac_secret="ab" * 32,
        db_connections=10,
        operator_reserve=2,
        other_workers=1,
        public_pool_size=5,
        api_replicas=1,
    )
    path = tmp_path / "admission.json"
    path.write_text(json.dumps(settings))
    monkeypatch.setenv("ALVARY_ADMISSION_CONFIG", str(path))
    with pg_engine.connect() as conn:
        monkeypatch.setenv("ALVARY_POLICY_SCHEMA", conn.scalar(text("SELECT current_schema()")))
    client = TestClient(create_app(Store(pg_engine), tmp_path), headers={"X-Forwarded-For": "203.0.113.9"})
    statuses = [client.get("/sources").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    assert client.get("/healthz").status_code == 200
    limited = client.get("/sources")
    assert limited.headers["Retry-After"] and limited.headers["Cache-Control"] == "no-store"
    assert "items" not in limited.json()
