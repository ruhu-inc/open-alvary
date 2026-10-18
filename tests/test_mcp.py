"""Read-only MCP endpoint: lifecycle, tools, policy parity and safety checks."""

import json

import pytest
from fastapi.testclient import TestClient

from api.main import create_app


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
        "get_coverage",
        "list_changes",
        "get_change",
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


@pytest.mark.parametrize(
    "params",
    [
        {"name": "delete_everything", "arguments": {}},
        {"name": "search_catalogue", "arguments": {"query": "x", "limit": 1000}},
        {"name": "search_catalogue", "arguments": {"query": "x", "sql": "drop"}},
        {"name": "get_source"},
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
