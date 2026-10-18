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
    "includes": ["rest", "mcp"],
    "allowance": {"requests_per_minute": 600, "burst": 100, "concurrency": 8},
    "published": True,
}


@pytest.fixture
def api(pg_engine, monkeypatch):
    monkeypatch.setenv("ALVARY_KEY_PEPPER", base64.b64encode(PEPPER).decode())
    monkeypatch.setenv("ALVARY_ACCOUNTS", "1")
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
    assert page.status_code == 200 and page.json()["total"] == 5
    assert "access-control-allow-origin" not in page.headers and page.headers["cache-control"] == "no-store"
    record = client.get("/api/v1/sources/ng-foi-act-2011", headers=headers)
    assert record.json()["title"] == "Freedom of Information Act 2011"
    assert client.get("/api/v1/search", params={"q": "act"}, headers=headers).json()["total"] == 4
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
    canonical = [
        line for line in client.get("/.well-known/security.txt").text.splitlines() if "Canonical" in line
    ]
    assert canonical[-1] == "Canonical: https://api.open.alvary.ai/.well-known/security.txt"


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


def test_outside_the_plans_countries_reads_at_the_starter_rate(api, pg_engine, monkeypatch, tmp_path):
    client, accounts, person, organisation = api
    grant(accounts, organisation, scope={"jurisdictions": ["rw"]})
    headers = key(accounts, person, organisation)
    # Without a jurisdiction, lists and searches cover the plan's countries (no Nigerian law here).
    assert client.get("/api/v1/sources", headers=headers).json()["total"] == 0
    assert client.get("/api/v1/search", params={"q": "act"}, headers=headers).json()["total"] == 0
    # Named, or by ID, every other country still answers.
    assert client.get("/api/v1/sources/ng-foi-act-2011", headers=headers).status_code == 200
    named = client.get("/api/v1/search", params={"q": "information", "jurisdiction": "ng"}, headers=headers)
    assert named.status_code == 200 and named.json()["total"] >= 1
    assert "ng" in {j["id"] for j in client.get("/api/v1/jurisdictions", headers=headers).json()}
    # ...at the Starter rate, on an allowance of its own.
    tiers = tmp_path / "tiers.json"
    tiers.write_text('[{"id": "starter", "allowance": {"requests_per_minute": 1, "burst": 2}}]')
    monkeypatch.setattr("api.service.TIERS", tiers)
    slow = TestClient(create_service(store=Store(pg_engine)))
    codes = [slow.get("/api/v1/sources/ng-foi-act-2011", headers=headers).status_code for _ in range(4)]
    assert codes[-1] == 429
    limited = slow.get("/api/v1/sources/ng-foi-act-2011", headers=headers)
    assert limited.json()["code"] == "rate_limited" and "Starter rate" in limited.json()["detail"]
    assert int(limited.headers["retry-after"]) >= 1
    assert slow.get("/api/v1/coverage", headers=headers).status_code == 200  # within the plan: unaffected
    [day] = accounts.usage(person, organisation)
    assert day["refusals"]["rate_limited"] >= 2


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


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params else {})}


def test_mcp_needs_a_credential_and_answers_tools_within_the_access(api):
    client, accounts, person, organisation = api
    refused = client.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18"}))
    assert refused.status_code == 401 and refused.json()["error"]["code"] == 10001
    assert refused.json()["error"]["data"]["code"] == "credential_required"
    assert (
        'resource_metadata="https://api.open.alvary.ai/.well-known/oauth-protected-resource/mcp"'
        in (refused.headers["www-authenticate"])
    )
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == "https://api.open.alvary.ai/mcp"
    assert client.get("/.well-known/oauth-protected-resource").json() == metadata
    grant(accounts, organisation, scope={"jurisdictions": ["ng"]})
    headers = key(accounts, person, organisation)
    assert (
        client.post(
            "/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18"}), headers=headers
        ).json()["result"]["serverInfo"]["name"]
        == "open-alvary"
    )
    tools = client.post("/mcp", json=rpc("tools/list"), headers=headers).json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "search_catalogue",
        "get_source",
        "get_relationships",
        "get_anchor",
        "search_text",
        "resolve_citation",
        "get_coverage",
        "list_changes",
        "get_change",
    }
    found = client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "get_source", "arguments": {"source_id": "ng-foi-act-2011"}}),
        headers=headers,
    ).json()["result"]
    assert not found["isError"] and "Freedom of Information" in found["content"][0]["text"]
    assert client.get("/mcp", headers=headers).status_code == 405  # no server-initiated stream
    [usage] = accounts.usage(person, organisation)
    assert usage["class"] == "mcp" and usage["requests"] == 4  # the GET counts too


def test_mcp_searches_the_plans_countries_and_reads_others_at_the_starter_rate(api):
    client, accounts, person, organisation = api
    grant(accounts, organisation, scope={"jurisdictions": ["rw"]})
    headers = key(accounts, person, organisation)

    def call(name, arguments):
        return client.post(
            "/mcp", json=rpc("tools/call", {"name": name, "arguments": arguments}), headers=headers
        ).json()["result"]

    outside = call("get_source", {"source_id": "ng-foi-act-2011"})
    assert not outside["isError"] and "Freedom of Information" in outside["content"][0]["text"]
    assert '"total": 0' in call("search_catalogue", {"query": "information"})["content"][0]["text"]
    named = call("search_catalogue", {"query": "information", "jurisdiction": "NG"})
    assert not named["isError"] and "Freedom of Information" in named["content"][0]["text"]
    accounts.revoke_access("op", organisation, "Test")
    lapsed = client.post("/mcp", json=rpc("tools/list"), headers=headers)
    assert lapsed.status_code == 403 and lapsed.json()["error"]["data"]["code"] == "entitlement_lapsed"
    assert "www-authenticate" not in lapsed.headers


def test_activation_b_stays_off_unless_switched_on(pg_engine, monkeypatch):
    monkeypatch.delenv("ALVARY_ACCOUNTS", raising=False)
    migrate(pg_engine)
    client = TestClient(create_service(store=Store(pg_engine)))
    assert client.get("/console/api/v1/me").status_code == 404
    assert client.post("/mcp", json=rpc("tools/list")).status_code in (404, 405)
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404


def test_activation_b_closes_the_anonymous_api_but_not_the_change_feed(api):
    client, _, _, _ = api
    gone = client.get("/api/sources")
    assert gone.status_code == 410 and gone.json()["successor"] == "https://api.open.alvary.ai/api/v1"
    assert 'rel="successor-version"' in gone.headers["link"]
    mcp = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert mcp.status_code == 410 and mcp.json()["successor"] == "https://api.open.alvary.ai/mcp"
    assert client.get("/api/openapi.json").json()["successor"].endswith("/api/v1/openapi.json")
    assert client.get("/api/releases/x/ng/metadata/manifest.json").status_code == 410
    assert client.get("/api/changes").status_code == 200  # the change feed stays anonymous (SUB C1)
    assert client.get("/api/change-coverage").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_a_records_history_comes_from_checked_links_a_page_at_a_time(api):
    client, accounts, person, organisation = api
    grant(accounts, organisation)
    headers = key(accounts, person, organisation)
    path = "/api/v1/sources/ng-isa-2007/relationships"
    checked = client.get(path, headers=headers).json()  # the repeal, checked against section 356(1)
    assert checked["current"]["source_id"] == "ng-isa-2025" and checked["total"] == 1
    assert [(i["kind"], i["source_id"]) for i in checked["items"]] == [("repealed_by", "ng-isa-2025")]
    later = client.get(
        "/api/v1/sources/ng-isa-2025/relationships", params={"kind": "repeals"}, headers=headers
    )
    assert [i["source_id"] for i in later.json()["items"]] == ["ng-isa-2007"]
    for bad in ({"kind": "parent_of"}, {"limit": 0}, {"cursor": "nonsense"}):
        refused = client.get(path, params=bad, headers=headers)
        assert refused.status_code == 400 and refused.json()["code"] == "invalid_input", bad
