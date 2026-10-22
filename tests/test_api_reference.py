"""REST v1's own description and refusals, which the developer documentation is built from: every
route, parameter and field described; every refusal a problem document with a documented code; and
the documentation's copies (web/src/docs/data) current."""

import json
from pathlib import Path
from typing import get_args

import httpx
import pytest
from fastapi.testclient import TestClient
from openapi_support import response_errors

from api.mcp import TOOLS
from api.repositories import EDGE_ORDER
from api.v1 import create_v1_app
from changes.feed import api_url
from exports.release import write_release
from schema.public import RelationKind
from scripts import docs_reference

ERRORS = Path(__file__).resolve().parents[1] / "web" / "src" / "docs" / "errors.ts"


@pytest.fixture
def v1(catalogue, store, tmp_path):
    store.save(catalogue)
    write_release(catalogue, tmp_path, "2026-10-05", "ng")
    return TestClient(create_v1_app(store, tmp_path, lambda: []))


def test_every_route_parameter_and_field_is_described():
    spec = json.loads(docs_reference.documents()["openapi.json"])
    for path, methods in spec["paths"].items():
        [operation] = methods.values()
        assert operation["summary"] and operation["description"] and operation["tags"], path
        assert all(p.get("description") for p in operation.get("parameters", [])), path
        assert "requestBody" not in operation or operation["requestBody"]["description"], path
        assert "422" not in operation["responses"], path
        for status, response in operation["responses"].items():
            if status not in ("200", "304"):
                assert response["content"]["application/problem+json"]["schema"]["$ref"].endswith("/Problem")
            assert "content" not in response or status != "304", path  # a 304 has no body
            for header in response.get("headers", {}).values():
                assert header["$ref"].removeprefix("#/components/headers/") in spec["components"]["headers"]
    for name, schema in spec["components"]["schemas"].items():
        for field, value in schema.get("properties", {}).items():
            assert value.get("description"), f"{name}.{field}"
    for name, header in spec["components"]["headers"].items():
        assert header["description"] and header["schema"], name
    for tool in TOOLS:
        assert all(p.get("description") for p in tool["inputSchema"]["properties"].values()), tool["name"]


def test_documented_error_codes_are_the_ones_the_api_gives():
    spec = json.loads(docs_reference.documents()["openapi.json"])
    documented = ERRORS.read_text()
    for methods in spec["paths"].values():
        [operation] = methods.values()
        for status, response in operation["responses"].items():
            if status == "304":  # not a refusal: its words name headers, not codes
                continue
            for code in response["description"].split("`")[1::2]:
                assert f'"{code}"' in documented, code


def test_the_documentation_has_the_current_reference():
    assert docs_reference.main(["--check"]) == 0, "run python -m scripts.docs_reference"


def test_refusals_are_problem_documents(v1):
    for path, status, code in [
        ("/sources?limit=101", 400, "invalid_input"),
        ("/search", 400, "invalid_input"),
        ("/sources/no-such-record", 404, "unavailable"),
        ("/rights/no-such-record", 404, "unavailable"),
    ]:
        response = v1.get(path)
        assert response.status_code == status, path
        assert response.headers["content-type"] == "application/problem+json", path
        body = response.json()
        assert body["code"] == code and body["type"].endswith(f"/docs/errors#{code}"), path
    assert "limit" in v1.get("/sources?limit=101").json()["detail"]


def test_an_address_that_is_no_route_is_a_problem_document_unlike_a_missing_record(v1):
    """G13: a consumer tells a missing route (not_found_route) from a missing record (unavailable)."""
    for path in ("/no-such-route", "/sources/ng-foi-act-2011/no-such-part", "/versions/x", "/"):
        response = v1.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"] == "application/problem+json", path
        body = response.json()
        assert body["code"] == "not_found_route" and body["type"].endswith("/docs/errors#not_found_route")
        assert body["retryable"] is False and "detail" in body and response.headers["x-request-id"]
    assert v1.get("/sources/no-such-record").json()["code"] == "unavailable"
    assert v1.post("/sources").status_code == 405  # a route that exists, asked with another method


def test_relationship_kinds_in_the_document_are_the_ones_the_service_accepts():
    """G13: the document's kinds come from the code (schema.public.RelationKind), which is the set the
    catalogue and MCP accept (api.repositories.EDGE_ORDER), and each kind is described."""
    spec = json.loads(docs_reference.documents()["openapi.json"])
    accepted = set(EDGE_ORDER)
    assert set(get_args(RelationKind)) == accepted
    [parameter] = [
        p
        for p in spec["paths"]["/sources/{source_id}/relationships"]["get"]["parameters"]
        if p["name"] == "kind"
    ]
    [listed] = [s["enum"] for s in parameter["schema"]["anyOf"] if "enum" in s]
    field = spec["components"]["schemas"]["RelatedSource"]["properties"]["kind"]
    assert set(listed) == set(field["enum"]) == accepted
    assert all(f"`{kind}`" in field["description"] for kind in accepted)
    [tool] = [t for t in TOOLS if t["name"] == "get_relationships"]
    assert set(tool["inputSchema"]["properties"]["relation_type"]["enum"]) == accepted


def test_the_contract_check_reports_each_way_an_answer_can_differ_from_the_document():
    """The check the PostgreSQL contract test applies to every route's answer (tests/openapi_support.py)
    finds a wrong body, a missing or undeclared header, an undeclared status and a 304 with a body."""
    spec = json.loads(docs_reference.documents()["openapi.json"])
    sent = {"X-Request-ID": "3f1c9a2e7b4d4f0e"}
    key = {
        "id": "key_" + "a" * 32,
        "kind": "service",
        "name": "Server",
        "created_at": "2026-10-01T09:00:00Z",
        "expires_at": None,
        "replaced": False,
        "organisation_id": "org_01",
        "entitlement": {"tier": {"id": "starter", "includes": ["rest"], "allowance": {}}, "scope": {"all": True},
                        "ends_at": None},
    }  # fmt: skip
    assert response_errors(spec, "/key", httpx.Response(200, json=key, headers=sent)) == []
    cases = {
        "secret is not in the document": httpx.Response(200, json=key | {"secret": "x"}, headers=sent),
        "entitlement is missing": httpx.Response(
            200, json={k: v for k, v in key.items() if k != "entitlement"}, headers=sent
        ),
        "'admin' is not one of": httpx.Response(200, json=key | {"kind": "admin"}, headers=sent),
        "has no time zone": httpx.Response(
            200, json=key | {"created_at": "2026-10-01T09:00:00"}, headers=sent
        ),
        "required header x-request-id is missing": httpx.Response(200, json=key),
        "sends etag": httpx.Response(200, json=key, headers=sent | {"ETag": '"x"'}),
        "status is not declared": httpx.Response(418, json={}, headers=sent),
        "not a whole number": httpx.Response(429, json={}, headers=sent | {"Retry-After": "soon"}),
    }
    for expected, answer in cases.items():
        assert any(expected in e for e in response_errors(spec, "/key", answer)), expected
    text = "/versions/{version_id}/text"
    tagged = sent | {"ETag": '"x"'}
    assert response_errors(spec, text, httpx.Response(304, headers=tagged)) == []
    assert any(
        "no body" in e for e in response_errors(spec, text, httpx.Response(304, content=b"x", headers=tagged))
    )
    assert any(
        "text/html" in e for e in response_errors(spec, text, httpx.Response(200, html="<p>", headers=tagged))
    )


def test_release_files_are_listed_where_they_download(v1):
    releases = v1.get("/releases").json()
    assert releases and all(r["download_base"].startswith("/downloads/") for r in releases)


def test_change_events_link_to_the_keyed_api_once_the_anonymous_one_closes(monkeypatch):
    monkeypatch.delenv("ALVARY_ACCOUNTS", raising=False)
    assert api_url("https://open.alvary.ai", "ng-x") == "https://open.alvary.ai/api/sources/ng-x"
    monkeypatch.setenv("ALVARY_ACCOUNTS", "1")
    monkeypatch.setenv("ALVARY_API_ORIGIN", "https://api.example.test")
    assert api_url("https://open.alvary.ai", "ng-x") == "https://api.example.test/api/v1/sources/ng-x"
