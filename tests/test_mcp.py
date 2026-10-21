"""Read-only MCP endpoint: lifecycle, tools, policy parity and safety checks."""

import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.catalogue import LegacyCatalogue
from api.main import create_app
from api.mcp import create_router
from api.reach import OutsidePlanLimited, Reach
from api.repositories import LegacyRepository, PublicUnavailable


@pytest.fixture
def client(store, catalogue, tmp_path):
    store.save(catalogue)
    return TestClient(create_app(store, tmp_path))


def rpc(client, method, params=None, id_=1, **headers):
    body = {"jsonrpc": "2.0", "method": method, "id": id_}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers)


def test_initialize_negotiates_protocol_and_declares_read_only_tools(client):
    result = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}).json()["result"]
    assert result["protocolVersion"] == "2025-06-18" and "tools" in result["capabilities"]
    fallback = rpc(client, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert fallback["protocolVersion"] == "2025-06-18"
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "search_catalogue",
        "get_source",
        "get_relationships",
        "list_expressions",
        "get_anchor",
        "search_text",
        "resolve_citation",
        "get_coverage",
        "list_changes",
        "get_change",
        "retrieve_evidence",
        "check_currentness",
    }
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


def test_tools_return_the_same_data_as_the_rest_api(client, catalogue):
    source_id = catalogue.sources[0].id
    call = rpc(client, "tools/call", {"name": "get_source", "arguments": {"source_id": source_id}}).json()
    assert json.loads(call["result"]["content"][0]["text"]) == client.get(f"/sources/{source_id}").json()
    search = rpc(
        client, "tools/call", {"name": "search_catalogue", "arguments": {"query": "act", "limit": 5}}
    ).json()
    rest = client.get("/search", params={"q": "act", "limit": 5}).json()
    assert json.loads(search["result"]["content"][0]["text"]) == rest
    coverage = rpc(client, "tools/call", {"name": "get_coverage", "arguments": {}}).json()
    assert json.loads(coverage["result"]["content"][0]["text"])["open_full_text_sources"] == 0


def test_withdrawn_or_missing_sources_are_not_disclosed(client, store, catalogue):
    catalogue.rights[0].metadata_public = False
    store.save(catalogue)
    params = {"name": "get_source", "arguments": {"source_id": catalogue.sources[0].id}}
    result = rpc(client, "tools/call", params).json()["result"]
    assert result["isError"] and result["content"][0]["text"] == "Source not available"
    assert result["structuredContent"] == {"code": "unavailable"}


def test_a_tool_that_fails_gives_the_rest_code_beside_its_message(store, catalogue):
    # A tool that ran and failed is a result with isError, its message as text (as before) and the code
    # REST would give in structuredContent; a successful answer has none.
    store.save(catalogue)

    @contextmanager
    def session():
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    def outside():
        raise OutsidePlanLimited(17)

    app = FastAPI()
    app.include_router(
        create_router(session, (PublicUnavailable,), lambda request: Reach(frozenset({"rw"}), outside))
    )
    client = TestClient(app)
    source = catalogue.sources[0]
    limited = rpc(client, "tools/call", {"name": "get_source", "arguments": {"source_id": source.id}})
    result = limited.json()["result"]
    assert result["isError"] and "Retry after 17 seconds" in result["content"][0]["text"]
    assert result["structuredContent"] == {"code": "rate_limited", "retry_after_seconds": 17}
    coverage = rpc(client, "tools/call", {"name": "get_coverage", "arguments": {}}).json()["result"]
    assert not coverage["isError"] and "structuredContent" not in coverage
    # Invalid arguments stay a JSON-RPC error, -32602.
    invalid = rpc(client, "tools/call", {"name": "get_source", "arguments": {"source_id": 7}}).json()
    assert invalid["error"]["code"] == -32602


@pytest.mark.parametrize(
    "params",
    [
        {"name": "delete_everything", "arguments": {}},
        {"name": "search_catalogue", "arguments": {"query": "x", "limit": 1000}},
        {"name": "search_catalogue", "arguments": {"query": "x", "sql": "drop"}},
        {"name": "get_source"},
        {"name": "retrieve_evidence", "arguments": {"query": "x", "max_units": 99}},
        {"name": "retrieve_evidence", "arguments": {"query": "x", "jurisdictions": "ng"}},
        {"name": "check_currentness", "arguments": {"items": []}},
        {"name": "check_currentness", "arguments": {"items": [{"locator": "sec_1"}]}},
    ],
)
def test_unknown_tools_and_invalid_arguments_are_rejected(client, params):
    assert rpc(client, "tools/call", params).json()["error"]["code"] == -32602


def test_protocol_errors_notifications_and_origin_checks(client):
    assert rpc(client, "resources/list").json()["error"]["code"] == -32601
    assert client.post("/mcp", content=b"{not json").json()["error"]["code"] == -32700
    assert client.post("/mcp", json=[{"jsonrpc": "2.0", "method": "ping", "id": 1}]).status_code == 400
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    assert rpc(client, "ping", origin="https://evil.example").status_code == 403
    assert rpc(client, "ping", origin="http://testserver").json()["result"] == {}
    # Behind the TLS edge the browser origin is https while the proxied request is http.
    assert rpc(client, "ping", origin="https://testserver").json()["result"] == {}
