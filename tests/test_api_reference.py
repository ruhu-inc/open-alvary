"""REST v1's own description and refusals, which the developer documentation is built from: every
route, parameter and field described; every refusal a problem document with a documented code; and
the documentation's copies (web/src/docs/data) current."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.mcp import TOOLS
from api.v1 import create_v1_app
from changes.feed import api_url
from exports.release import write_release
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
        operation = methods["get"]
        assert operation["summary"] and operation["description"] and operation["tags"], path
        assert all(p.get("description") for p in operation.get("parameters", [])), path
        assert "422" not in operation["responses"], path
        for status, response in operation["responses"].items():
            if status != "200":
                assert response["content"]["application/problem+json"]["schema"]["$ref"].endswith("/Problem")
    for name, schema in spec["components"]["schemas"].items():
        for field, value in schema.get("properties", {}).items():
            assert value.get("description"), f"{name}.{field}"
    for tool in TOOLS:
        assert all(p.get("description") for p in tool["inputSchema"]["properties"].values()), tool["name"]


def test_documented_error_codes_are_the_ones_the_api_gives():
    spec = json.loads(docs_reference.documents()["openapi.json"])
    documented = ERRORS.read_text()
    for methods in spec["paths"].values():
        for response in methods["get"]["responses"].values():
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


def test_release_files_are_listed_where_they_download(v1):
    releases = v1.get("/releases").json()
    assert releases and all(r["download_base"].startswith("/downloads/") for r in releases)


def test_change_events_link_to_the_keyed_api_once_the_anonymous_one_closes(monkeypatch):
    monkeypatch.delenv("ALVARY_ACCOUNTS", raising=False)
    assert api_url("https://open.alvary.ai", "ng-x") == "https://open.alvary.ai/api/sources/ng-x"
    monkeypatch.setenv("ALVARY_ACCOUNTS", "1")
    monkeypatch.setenv("ALVARY_API_ORIGIN", "https://api.example.test")
    assert api_url("https://open.alvary.ai", "ng-x") == "https://api.example.test/api/v1/sources/ng-x"
