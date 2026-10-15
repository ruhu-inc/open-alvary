"""REST v1 through the public service against PostgreSQL: keys, access, scope, limits, usage."""

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import policies
from accounts.store import Accounts
from api.service import create_service
from api.store import Store, seed
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
PEPPER = b"v" * 32
TIER = {
    "id": "pilot",
    "name": "Pilot",
    "description": "Test",
    "includes": ["rest", "mcp", "bulk"],
    "allowance": {"requests_per_minute": 600, "burst": 100, "concurrency": 8},
    "published": True,
}


@pytest.fixture
def api(pg_engine, monkeypatch):
    monkeypatch.setenv("ALVARY_KEY_PEPPER", base64.b64encode(PEPPER).decode())
    for name in ("ALVARY_CONSOLE_CLIENT_ID", "ALVARY_ADMISSION_CONFIG", "ALVARY_SITE_ADMISSION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)
    with pg_engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    accounts = Accounts(pg_engine, namespace, key_pepper=PEPPER)
    accounts.set_tiers(
        [TIER, {**TIER, "id": "tiny", "allowance": {"requests_per_minute": 1, "burst": 2, "concurrency": 2}}]
    )
    signed = accounts.sign_in(
        issuer="https://login.example.test/",
        subject="email|a",
        method="email",
        email="a@example.org",
        label="a@example.org",
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    organisation = accounts.welcome(signed["id"], "Amina", "Amina")
    return TestClient(create_service(store=store)), accounts, signed["id"], organisation


def grant(accounts, organisation, **kwargs):
    accounts.grant_access(
        "op",
        organisation,
        tier=kwargs.pop("tier", "pilot"),
        scope=kwargs.pop("scope", {"all": True}),
        source="manual",
        ends_at=None,
        reason="Test",
    )


def key(accounts, person, organisation):
    return {
        "Authorization": "Bearer "
        + accounts.create_key(person, organisation, kind="personal", name="k")["secret"]
    }


def test_a_key_reads_the_corpus_and_its_use_is_counted(api):
    client, accounts, person, organisation = api
    grant(accounts, organisation)
    headers = key(accounts, person, organisation)
    page = client.get("/api/v1/sources", headers=headers)
    assert page.status_code == 200 and page.json()["total"] == 4
    assert "access-control-allow-origin" not in page.headers and page.headers["cache-control"] == "no-store"
    record = client.get("/api/v1/sources/ng-foi-act-2011", headers=headers)
    assert record.json()["title"] == "Freedom of Information Act 2011"
    assert client.get("/api/v1/search", params={"q": "act"}, headers=headers).json()["total"] == 3
    [day] = accounts.usage(person, organisation)
    assert day["requests"] == 3 and day["bytes"] > 0 and day["class"] == "rest"
    assert accounts.list_keys(person, organisation)[0]["last_used_at"] is not None


def test_public_documents_need_no_credential_and_allow_any_origin(api):
    client, *_ = api
    for path in ("/api/v1/openapi.json", "/api/v1/quota-policy", "/api/v1/tiers"):
        response = client.get(path)
        assert response.status_code == 200 and response.headers["access-control-allow-origin"] == "*"
    assert [t["id"] for t in client.get("/api/v1/tiers").json()["tiers"]] == ["pilot", "tiny"]
    metadata = client.get("/.well-known/oauth-protected-resource/api/v1").json()
    assert metadata["resource"] == "https://api.open.alvary.ai/api/v1" and metadata["scopes_supported"] == [
        "corpus:read"
    ]
    refused = client.get("/api/v1/sources")
    assert refused.status_code == 401 and "resource_metadata=" in refused.headers["www-authenticate"]


def test_no_access_lapse_and_revocation_answer_403_without_a_challenge(api):
    client, accounts, person, organisation = api
    grant(accounts, organisation)
    headers = key(accounts, person, organisation)
    accounts.revoke_access("op", organisation, "Test")
    lapsed = client.get("/api/v1/sources", headers=headers)
    assert lapsed.status_code == 403 and lapsed.json()["code"] == "entitlement_lapsed"
    assert "www-authenticate" not in lapsed.headers
    grant(accounts, organisation)
    assert client.get("/api/v1/sources", headers=headers).status_code == 200
    accounts.revoke_key(person, organisation, "key_" + headers["Authorization"].split("_")[2])
    assert client.get("/api/v1/sources", headers=headers).json()["code"] == "invalid_credentials"


def test_a_jurisdiction_scope_limits_lists_and_refuses_other_records(api):
    client, accounts, person, organisation = api
    grant(accounts, organisation, scope={"jurisdictions": ["rw"]})
    headers = key(accounts, person, organisation)
    assert client.get("/api/v1/sources", headers=headers).json()["total"] == 0
    outside = client.get("/api/v1/sources/ng-foi-act-2011", headers=headers)
    assert outside.status_code == 403 and outside.json()["code"] == "jurisdiction_not_included"
    assert (
        client.get("/api/v1/search", params={"q": "act", "jurisdiction": "ng"}, headers=headers).status_code
        == 403
    )
    assert client.get("/api/v1/jurisdictions", headers=headers).json() == []
    [day] = accounts.usage(person, organisation)
    assert day["refusals"] == {"jurisdiction_not_included": 2}


def test_the_organisation_allowance_answers_429_with_retry_after(api):
    client, accounts, person, organisation = api
    grant(accounts, organisation, tier="tiny")
    headers = key(accounts, person, organisation)
    codes = [client.get("/api/v1/coverage", headers=headers).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    limited = client.get("/api/v1/coverage", headers=headers)
    assert limited.json()["code"] == "rate_limited" and int(limited.headers["retry-after"]) >= 1
    [day] = accounts.usage(person, organisation)
    assert day["requests"] == 2 and day["refusals"]["rate_limited"] == 2
