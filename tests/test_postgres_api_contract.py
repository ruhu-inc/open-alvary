"""The published contract matches the service (G13). Every REST v1 route is called through the public
service, as production runs it: the credential gate with a real key, admission and the accounts schema in
PostgreSQL, over a published text and its recorded legal effects. Each answer is checked against the
OpenAPI document the developer documentation publishes (web/src/docs/data/openapi.json): its status, its
body, the headers it must carry and the headers it may. A route that declares 304 answers If-None-Match
with one, and no other route does. Refusals, the organisation's allowance in RateLimit headers, the
calling key (GET /key) and an address that is no route are checked the same way."""

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from openapi_support import response_errors, schema_errors
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_api_v1 import PEPPER, grant, rpc
from test_postgres_events import UNITS, an_event, publish
from test_v1_text import WORK, publish_act

from accounts import keys, policies
from accounts.store import Accounts
from api.service import create_service
from api.store import Store
from changes.routes import tokens
from exports.release import write_release
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
SPEC = json.loads(
    (Path(__file__).resolve().parents[1] / "web" / "src" / "docs" / "data" / "openapi.json").read_text()
)
TIER = {
    "id": "contract",
    "name": "Contract",
    "description": "Test",
    "includes": ["rest", "mcp", "bulk"],
    "allowance": {"requests_per_minute": 600, "burst": 100, "concurrency": 8},
    "published": True,
}
TINY = {**TIER, "id": "tiny", "allowance": {"requests_per_minute": 1, "burst": 1, "concurrency": 2}}
RELEASE = "2026-10-05"


@pytest.fixture
def contract(pg_engine, monkeypatch, tmp_path):
    monkeypatch.setenv("ALVARY_KEY_PEPPER", base64.b64encode(PEPPER).decode())
    monkeypatch.setenv("ALVARY_ACCOUNTS", "1")
    for name in (
        "ALVARY_CONSOLE_CLIENT_ID",
        "ALVARY_ADMISSION_CONFIG",
        "ALVARY_SITE_ADMISSION_CONFIG",
        "ALVARY_FEED_KEYS",
        "ALVARY_RUNTIME_MODE",
        "PUBLIC_DOMAIN",
    ):
        monkeypatch.delenv(name, raising=False)
    tokens.cache_clear()  # change-feed tokens of this process, so replay answers
    migrate(pg_engine)
    store = Store(pg_engine)
    _, version, nodes = publish_act(store, UNITS)
    commenced = an_event("commenced", "2023-06-12")
    publish(store, commenced, an_event("repealed", "2024-03-01", scope="explicit_nodes", locators=["sec_2"]))
    write_release(store.load(), tmp_path, RELEASE, "ng")
    with pg_engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    accounts = Accounts(pg_engine, namespace, key_pepper=PEPPER)
    accounts.set_tiers([TIER, TINY])
    signed = accounts.sign_in(
        issuer="https://login.example.test/",
        subject="email|a",
        method="email",
        email="a@example.org",
        label="a@example.org",
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    organisation = accounts.welcome(signed["id"], "Amina", "Amina")
    grant(accounts, organisation, tier="contract")
    client = TestClient(create_service(store=store, releases=tmp_path))
    yield client, accounts, signed["id"], organisation, version, nodes
    tokens.cache_clear()


def bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def new_key(accounts, person, organisation, kind="personal", name="k", days=90) -> dict:
    return accounts.create_key(person, organisation, kind=kind, name=name, days=days)


def calls(version, nodes, event_id) -> list[tuple[str, str, str, dict, dict | None]]:
    """(method, the path as the document writes it, the path called, query, body) for every route."""
    anchor = nodes["sec_1"]
    text_id = quote(version.id, safe="")
    return [
        ("get", "/search", "/search", {"q": "loan"}, None),
        ("get", "/sources", "/sources", {}, None),
        ("get", "/sources/{source_id}", f"/sources/{WORK}", {}, None),
        ("get", "/sources/{source_id}/relationships", f"/sources/{WORK}/relationships", {}, None),
        ("get", "/sources/{source_id}/expressions", f"/sources/{WORK}/expressions", {}, None),
        ("get", "/sources/{source_id}/versions", f"/sources/{WORK}/versions", {}, None),
        ("get", "/versions/{version_id}/text", f"/versions/{text_id}/text", {}, None),
        ("get", "/versions/{version_id}/structure", f"/versions/{text_id}/structure", {}, None),
        ("get", "/anchors/{anchor_id}", f"/anchors/{quote(anchor.id, safe='')}", {}, None),
        ("get", "/rights/{source_id}", f"/rights/{WORK}", {}, None),
        ("get", "/rights/{source_id}/history", f"/rights/{WORK}/history", {}, None),
        ("get", "/search/text", "/search/text", {"q": "salary"}, None),
        ("get", "/retrieve", "/retrieve", {"q": "repaid from salary"}, None),
        (
            "post",
            "/currentness",
            "/currentness",
            {},
            {"items": [{"anchor_id": anchor.id, "span_hash": anchor.span_hash, "as_of": "2024-01-01"}]},
        ),
        ("get", "/citations/{citation}", "/citations/" + quote("Example Act, 2023, s. 1"), {}, None),
        ("get", "/jurisdictions", "/jurisdictions", {}, None),
        ("get", "/jurisdictions/{jurisdiction_id}", "/jurisdictions/ng", {"as_of": "2026-01-01"}, None),
        ("get", "/courts", "/courts", {}, None),
        ("get", "/courts/{jurisdiction}", "/courts/ke", {}, None),
        ("get", "/coverage", "/coverage", {}, None),
        ("get", "/releases", "/releases", {}, None),
        (
            "get",
            "/bulk/{release_id}/{jurisdiction}/{mode}/{filename}",
            f"/bulk/{RELEASE}/ng/corpus/manifest.json",
            {},
            None,
        ),
        ("get", "/tiers", "/tiers", {}, None),
        ("get", "/quota-policy", "/quota-policy", {}, None),
        ("get", "/key", "/key", {}, None),
        ("get", "/changes", "/changes", {"jurisdiction": "ng"}, None),
        ("get", "/changes/recent", "/changes/recent", {"jurisdiction": "ng"}, None),
        ("get", "/changes/feed.atom", "/changes/feed.atom", {"jurisdiction": "ng"}, None),
        ("get", "/changes/feed.rss", "/changes/feed.rss", {"jurisdiction": "ng"}, None),
        ("get", "/changes/{event_id}", f"/changes/{event_id}", {}, None),
        ("get", "/change-coverage", "/change-coverage", {}, None),
    ]


def test_every_route_answers_as_the_document_says_and_a_304_where_it_declares_one(contract):
    client, accounts, person, organisation, version, nodes = contract
    headers = bearer(new_key(accounts, person, organisation)["secret"])
    feed = client.get("/api/v1/changes", params={"jurisdiction": "ng"}, headers=headers).json()
    assert feed["events"], "the recorded legal effects are changes"
    routes = calls(version, nodes, feed["events"][-1]["event_id"])
    # Every route the document lists is called here, so a new one cannot go unchecked.
    assert {template for _, template, *_ in routes} == set(SPEC["paths"])
    differences, conditional = [], set()
    for method, template, path, query, body in routes:
        answer = client.request(method, "/api/v1" + path, params=query, json=body, headers=headers)
        assert answer.status_code == 200, (path, answer.status_code, answer.text[:300])
        differences += response_errors(SPEC, template, answer, method)
        declared = "304" in SPEC["paths"][template][method]["responses"]
        # The answer's own tag is matched when the route answers If-None-Match, and ignored otherwise.
        tag = answer.headers.get("etag", '"no-tag"')
        again = client.request(
            method, "/api/v1" + path, params=query, json=body, headers=headers | {"If-None-Match": tag}
        )
        if again.status_code == 304:
            conditional.add(template)
            assert again.content == b"" and again.headers["etag"] == tag, path
            differences += response_errors(SPEC, template, again, method)
        assert (again.status_code == 304) == declared, (path, again.status_code)
    assert not differences, "\n".join(differences)
    assert conditional == {
        "/sources/{source_id}/versions",
        "/versions/{version_id}/text",
        "/versions/{version_id}/structure",
        "/anchors/{anchor_id}",
        "/search/text",
        "/retrieve",
        "/currentness",
    }


def test_refusals_answer_as_the_document_says(contract, monkeypatch):
    client, accounts, person, organisation, version, nodes = contract
    headers = bearer(new_key(accounts, person, organisation)["secret"])
    answers = [
        ("/sources", client.get("/api/v1/sources"), 401),
        ("/sources", client.get("/api/v1/sources", params={"limit": 101}, headers=headers), 400),
        ("/sources/{source_id}", client.get("/api/v1/sources/no-such-record", headers=headers), 404),
        ("/anchors/{anchor_id}", client.get("/api/v1/anchors/no-such-part", headers=headers), 404),
    ]
    # Replay switched off, as on a persistent deployment without feed keys: 503 with Retry-After.
    monkeypatch.setenv("ALVARY_RUNTIME_MODE", "persistent")
    tokens.cache_clear()
    answers.append(("/changes", client.get("/api/v1/changes", headers=headers), 503))
    # The organisation's allowance used up: 429 with Retry-After, refused before it is counted.
    accounts.revoke_access("op", organisation, "Test")
    grant(accounts, organisation, tier="tiny")
    answers += [
        ("/coverage", client.get("/api/v1/coverage", headers=headers), status) for status in (200, 429)
    ]
    differences = []
    for template, answer, status in answers:
        assert answer.status_code == status, (template, answer.status_code, answer.text[:300])
        differences += response_errors(SPEC, template, answer)
    assert not differences, "\n".join(differences)
    limited = answers[-1][1]
    assert int(limited.headers["retry-after"]) >= 1 and "ratelimit" not in limited.headers
    assert answers[4][1].headers["retry-after"] == "3600"


def test_answers_state_what_is_left_of_the_organisations_requests(contract):
    """G13: as the website's data path does, REST v1 and MCP answers counted against the organisation's
    requests a minute carry RateLimit-Policy (the plan's burst and its refill time) and RateLimit (what is
    left, and the seconds until the burst is full again)."""
    client, accounts, person, organisation, version, nodes = contract
    headers = bearer(new_key(accounts, person, organisation)["secret"])
    first = client.get("/api/v1/coverage", headers=headers)
    second = client.get(f"/api/v1/versions/{quote(version.id, safe='')}/text", headers=headers)
    unchanged = client.get(
        f"/api/v1/versions/{quote(version.id, safe='')}/text",
        headers=headers | {"If-None-Match": second.headers["etag"]},
    )
    mcp = client.post("/mcp", json=rpc("tools/list"), headers=headers)
    left = []
    for answer in (first, second, unchanged, mcp):
        assert answer.status_code in (200, 304)
        assert answer.headers["ratelimit-policy"] == '"organisation";q=100;w=10'
        name, remaining, reset = answer.headers["ratelimit"].split(";")
        assert name == '"organisation"' and reset.startswith("t=") and int(reset[2:]) >= 0
        left.append(int(remaining.removeprefix("r=")))
    # Each counted answer has spent a request (never the full burst of 100); the bucket refills at ten a
    # second, so on a busy machine a later answer may show more left than an earlier one.
    assert all(0 <= n <= 99 for n in left)
    assert "ratelimit" not in client.get("/api/v1/tiers").headers  # a public answer is not counted


def test_the_calling_key_is_described_and_nothing_else(contract):
    client, accounts, person, organisation, version, nodes = contract
    other = new_key(accounts, person, organisation, name="Laptop")
    server = new_key(accounts, person, organisation, kind="service", name="Production server", days=30)
    answer = client.get("/api/v1/key", headers=bearer(server["secret"]))
    assert answer.status_code == 200 and not response_errors(SPEC, "/key", answer)
    described = answer.json()
    assert described == {
        "id": server["id"],
        "kind": "service",
        "name": "Production server",
        "created_at": described["created_at"],
        "expires_at": described["expires_at"],
        "replaced": False,
        "organisation_id": organisation,
        "entitlement": {
            "tier": {"id": "contract", "includes": ["rest", "mcp", "bulk"], "allowance": TIER["allowance"]},
            "scope": {"all": True},
            "ends_at": None,
        },
    }
    assert datetime.fromisoformat(described["expires_at"]) == server["expires_at"]
    assert datetime.fromisoformat(described["created_at"]) == server["created_at"]
    # Never the secret, any part of it or its hash, and never another key.
    secret = server["secret"]
    stored = keys.secret_hash(secret, PEPPER).hex()
    for leaked in (secret, secret.rsplit("_", 1)[1], stored, other["id"], other["id"].removeprefix("key_")):
        assert leaked not in answer.text
    # It counts as one request, like any other read.
    [day] = accounts.usage(person, organisation)
    assert day["requests"] == 1 and day["class"] == "rest"
    # A rotated key says it was replaced, and when its overlap ends.
    accounts.rotate_key(person, organisation, server["id"], overlap_hours=3)
    rotated = client.get("/api/v1/key", headers=bearer(secret)).json()
    assert rotated["replaced"] is True and rotated["id"] == server["id"]
    assert datetime.fromisoformat(rotated["expires_at"]) <= datetime.now(UTC) + timedelta(hours=3)
    # An organisation's access with countries and an end says so.
    accounts.revoke_access("op", organisation, "Test")
    ends = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    accounts.grant_access(
        "op",
        organisation,
        tier="contract",
        scope={"jurisdictions": ["ng", "ke"]},
        source="manual",
        ends_at=ends,
        reason="Test",
    )
    narrowed = client.get("/api/v1/key", headers=bearer(other["secret"])).json()
    assert narrowed["id"] == other["id"] and narrowed["kind"] == "personal" and narrowed["name"] == "Laptop"
    assert narrowed["entitlement"]["scope"] == {"jurisdictions": ["ng", "ke"]}
    assert datetime.fromisoformat(narrowed["entitlement"]["ends_at"]) == ends
    refused = client.get("/api/v1/key")
    assert refused.status_code == 401 and refused.json()["code"] == "credential_required"


def test_an_address_that_is_no_route_is_a_problem_document_not_a_missing_record(contract):
    client, accounts, person, organisation, version, nodes = contract
    headers = bearer(new_key(accounts, person, organisation)["secret"])
    problem = {"$ref": "#/components/schemas/Problem"}
    for path in ("/api/v1/retrieve/now", "/api/v1/no-such-route", f"/api/v1/sources/{WORK}/no-such-part"):
        answer = client.get(path, headers=headers)
        assert answer.status_code == 404 and answer.headers["content-type"] == "application/problem+json"
        body = answer.json()
        assert body["code"] == "not_found_route" and body["request_id"] == answer.headers["x-request-id"]
        assert not schema_errors(SPEC, problem, body) and answer.headers["ratelimit"]
    missing = client.get("/api/v1/sources/no-such-record", headers=headers).json()
    assert missing["code"] == "unavailable"
    # Without a key, the credential is asked for first, as for any other address.
    assert client.get("/api/v1/no-such-route").json()["code"] == "credential_required"
