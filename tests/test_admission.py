"""ACS-05 admission middleware: identity grouping, queueing, fail-closed and headers."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.admission import (
    GLOBAL_KEY,
    AdmissionConfig,
    AdmissionMiddleware,
    AdmissionUnavailable,
    client_address,
    network_group,
    pseudonym,
)

SECRET = b"s" * 32


def config(**overrides):
    values = dict(
        policy_revision="synthetic-1",
        capacity=10,
        refill_per_minute=30,
        identity_concurrency=4,
        global_concurrency=8,
        lease_seconds=30,
        queue_size=2,
        queue_deadline_seconds=0.3,
        trusted_proxies=1,
        hmac_secret=SECRET,
        previous_hmac_secret=None,
        db_connections=20,
        operator_reserve=3,
        other_workers=2,
        public_pool_size=10,
        api_replicas=2,
    )
    values.update(overrides)
    return AdmissionConfig(**values)


class FakeStore:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests, self.released = [], []

    def admit(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome == "down":
            raise AdmissionUnavailable("down")
        return outcome

    def release(self, lease_id):
        self.released.append(lease_id)


def admitted(key=None):
    return {
        "admitted": True,
        "lease_id": "lease-1",
        "buckets": {key or pseudonym(SECRET, "203.0.113.7"): {"remaining": 9, "reset": 2}},
    }


def client(store, cfg=None):
    app = FastAPI()

    @app.get("/sources")
    def sources():
        return {"items": []}

    @app.get("/boom")
    def boom():
        raise RuntimeError("handler failure")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    app.add_middleware(AdmissionMiddleware, config=cfg or config(), store=store)
    return TestClient(app, raise_server_exceptions=False)


HEADERS = {"X-Forwarded-For": "198.51.100.1, 203.0.113.7"}


def test_trusted_proxy_chain_selects_the_outermost_appended_address():
    raw = [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.7")]
    assert client_address(raw, "10.0.0.2", 1) == "203.0.113.7"
    assert client_address(raw, "10.0.0.2", 2) == "6.6.6.6"
    assert client_address(raw, "10.0.0.2", 0) == "10.0.0.2"
    assert client_address([], "10.0.0.2", 1) is None


def test_network_grouping_and_pseudonyms():
    assert network_group("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"
    assert network_group("::ffff:203.0.113.7") == "203.0.113.7"
    assert pseudonym(SECRET, "203.0.113.7") != pseudonym(b"t" * 32, "203.0.113.7")
    assert "203.0.113.7" not in pseudonym(SECRET, "203.0.113.7")


@pytest.mark.parametrize(
    "overrides",
    [
        dict(public_pool_size=16),
        dict(hmac_secret=b"short"),
        dict(capacity=0),
        dict(lease_seconds=601),
        dict(api_replicas=11),
    ],
)
def test_configuration_rejects_unsafe_values(overrides):
    with pytest.raises(ValueError):
        config(**overrides)


def test_admitted_request_carries_quota_headers_and_releases_its_lease():
    store = FakeStore([admitted()])
    response = client(store).get("/sources", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["RateLimit-Policy"] == '"anonymous";q=10;w=20'
    assert response.headers["RateLimit"] == '"anonymous";r=9;t=2'
    assert response.headers["Open-Alvary-Quota-Tier"] == "anonymous"
    assert store.released == ["lease-1"]
    keys = {c["key"] for c in store.requests[0]["concurrency"]}
    assert GLOBAL_KEY in keys and "203.0.113.7" not in str(store.requests)


def test_lease_is_released_even_when_the_handler_fails():
    store = FakeStore([admitted()])
    assert client(store).get("/boom", headers=HEADERS).status_code == 500
    assert store.released == ["lease-1"]


def test_identity_limit_returns_429_with_retry_after():
    store = FakeStore([{"admitted": False, "blocked": ["ip:x"], "retry_after": 7}])
    response = client(store).get("/sources", headers=HEADERS)
    assert response.status_code == 429 and response.headers["Retry-After"] == "7"
    assert response.json()["retry_after_seconds"] == 7 and store.released == []


def test_global_capacity_waits_in_queue_then_proceeds_uncharged():
    full = {"admitted": False, "blocked": [GLOBAL_KEY], "retry_after": 1}
    store = FakeStore([full, full, admitted()])
    assert client(store).get("/sources", headers=HEADERS).status_code == 200
    assert len(store.requests) == 3


def test_global_capacity_past_queue_deadline_is_503_capacity_exhausted():
    store = FakeStore([{"admitted": False, "blocked": [GLOBAL_KEY], "retry_after": 2}])
    response = client(store).get("/sources", headers=HEADERS)
    assert response.status_code == 503 and response.json()["detail"] == "capacity_exhausted"


def test_full_queue_rejects_without_waiting():
    store = FakeStore([{"admitted": False, "blocked": [GLOBAL_KEY], "retry_after": 2}])
    response = client(store, config(queue_size=0)).get("/sources", headers=HEADERS)
    assert response.status_code == 503 and len(store.requests) == 1


def test_quota_store_outage_fails_closed_without_body():
    response = client(FakeStore(["down"])).get("/sources", headers=HEADERS)
    assert response.status_code == 503 and response.json() == {"detail": "Quota store unavailable"}


def test_health_is_exempt_and_missing_client_address_is_rejected():
    store = FakeStore(["down"])
    assert client(store).get("/healthz").status_code == 200
    assert client(FakeStore([admitted()])).get("/sources").status_code == 400


def test_rotation_debits_both_current_and_previous_buckets():
    store = FakeStore([admitted()])
    client(store, config(previous_hmac_secret=b"p" * 32)).get("/sources", headers=HEADERS)
    assert len(store.requests[0]["buckets"]) == 2


def test_each_replica_gets_an_equal_share_of_the_public_pool():
    assert config(public_pool_size=10, api_replicas=3).instance_pool_size == 3


def test_published_quota_policy_states_limits_without_secrets():
    from api.admission import ByteLimits, published_policy

    assert published_policy(None)["enforcement"] == "none"
    mib = 10 * 1024 * 1024
    document = published_policy(
        config(byte_limits=ByteLimits(mib, 60, 4 * mib, 600, 60), previous_hmac_secret=b"p" * 32)
    )
    assert document["request_units"] == {"burst": 10, "refill_per_minute": 30, "window_seconds": 20}
    assert document["response_bytes"]["burst"] == mib
    text = str(document)
    assert "sss" not in text and "ppp" not in text and "public_pool" not in text


def test_quota_policy_route_reports_unconfigured_pilot(store, tmp_path):
    from api.main import create_app

    response = TestClient(create_app(store, tmp_path)).get("/quota-policy")
    assert response.status_code == 200 and response.json()["enforcement"] == "none"
