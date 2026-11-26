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
        "check_claim",
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
    assert result["structuredContent"] == {
        "code": "rate_limited",
        "retry_after_seconds": 17,
        "retryable": True,
        "manage_url": "https://open.alvary.ai/console/access",
    }
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


META_2026 = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test-host", "version": "1.0"},
}


def rpc26(client, method, params=None, id_=1, *, headers=None, include_meta=True):
    body_params = dict(params or {})
    if include_meta and method != "server/discover" and "_meta" not in body_params:
        body_params["_meta"] = META_2026
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if body_params:
        body["params"] = body_params
    hdrs = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if method == "tools/call" and isinstance(body_params.get("name"), str):
        hdrs["Mcp-Name"] = body_params["name"]
    if headers:
        hdrs.update(headers)
    return client.post("/mcp", json=body, headers=hdrs)


def _test_signer():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from api.receipts import ReceiptSigner

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return ReceiptSigner(pem, kid="test-kid", valid_from="2026-01-01T00:00:00Z")


def test_2026_discover_ping_wire_errors_2025_03_26_and_paginated_tools_list(store, catalogue):
    import base64

    from api.mcp import INSTRUCTIONS, SERVER_INFO, TOOL_SCHEMA_REVISION

    store.save(catalogue)

    @contextmanager
    def session():
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    app = FastAPI()
    app.include_router(create_router(session, (PublicUnavailable,), tools_page_size=5))
    client = TestClient(app)

    # server/discover (2026-07-28)
    disc = rpc26(client, "server/discover")
    assert disc.status_code == 200
    dres = disc.json()["result"]
    assert dres == {
        "resultType": "complete",
        "supportedVersions": ["2026-07-28", "2025-06-18"],
        "capabilities": {"tools": {}},
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
        "instructions": INSTRUCTIONS,
        "ttlMs": 0,
        "cacheScope": "public",
    }
    assert "bulk-download alternative" not in dres["instructions"]
    assert "https://open.alvary.ai/docs/ai-tools" in dres["instructions"]
    # initialize (2025-06-18) carries the same expanded instructions
    init25 = rpc(client, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init25["instructions"] == INSTRUCTIONS

    # ping: 2026 vs 2025-03-26
    assert rpc26(client, "ping").json()["result"] == {"resultType": "complete"}
    ping_0326 = rpc(client, "ping", **{"MCP-Protocol-Version": "2025-03-26"})
    assert ping_0326.status_code == 200 and ping_0326.json()["result"] == {}
    list_0326 = rpc(client, "tools/list", **{"MCP-Protocol-Version": "2025-03-26"}).json()["result"]
    assert "outputSchema" not in list_0326["tools"][0] and "resultType" not in list_0326

    # Multi-page lexicographic tools/list (tools_page_size=5 -> 5, 5, 3 = 13)
    collected_26 = []
    cursor = None
    pages = 0
    while True:
        pages += 1
        params = {"cursor": cursor} if cursor is not None else {}
        resp = rpc26(client, "tools/list", params)
        assert resp.status_code == 200
        res = resp.json()["result"]
        assert res["resultType"] == "complete" and res["ttlMs"] == 0 and res["cacheScope"] == "public"
        collected_26.extend(res["tools"])
        cursor = res["nextCursor"]
        if cursor is None:
            break
    assert pages == 3 and len(collected_26) == 13
    names = [t["name"] for t in collected_26]
    assert names == sorted(names)
    expected_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert all(t["annotations"] == expected_annotations for t in collected_26)
    assert all("outputSchema" in t for t in collected_26)

    # Multi-page tools/list under 2025-06-18 omits nextCursor on last page and omits outputSchema
    collected_25 = []
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor is not None else None
        res = rpc(client, "tools/list", params).json()["result"]
        assert "resultType" not in res and "ttlMs" not in res and "cacheScope" not in res
        collected_25.extend(res["tools"])
        if "nextCursor" not in res:
            break
        cursor = res["nextCursor"]
    assert [t["name"] for t in collected_25] == names
    assert all("outputSchema" not in t for t in collected_25)

    # Wire error matrix
    # 1. unsupported_protocol_version
    bad_ver = rpc(client, "ping", **{"MCP-Protocol-Version": "2099-01-01"})
    assert bad_ver.status_code == 400
    assert bad_ver.json()["error"]["code"] == -32600
    assert bad_ver.json()["error"]["data"] == {
        "code": "unsupported_protocol_version",
        "supportedVersions": ["2026-07-28", "2025-06-18"],
    }
    # 2. unsupported_method_for_version (initialize under 2026-07-28)
    init26 = rpc26(client, "initialize", {"protocolVersion": "2026-07-28"})
    assert init26.status_code == 400
    assert init26.json()["error"]["code"] == -32600
    assert init26.json()["error"]["data"] == {"code": "unsupported_method_for_version"}
    # 3. notifications_unsupported under 2026-07-28
    note26 = rpc26(client, "notifications/initialized", id_=None)
    assert note26.status_code == 400
    assert note26.json()["error"]["code"] == -32600
    assert note26.json()["error"]["data"] == {"code": "notifications_unsupported"}
    # 4. header_mismatch (missing header, wrong method, missing/wrong Mcp-Name, stray Mcp-Name, header vs _meta)
    no_hdr_disc = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    assert no_hdr_disc.status_code == 400 and no_hdr_disc.json()["error"]["data"] == {
        "code": "header_mismatch"
    }
    wrong_method = rpc26(client, "ping", headers={"Mcp-Method": "tools/list"})
    assert wrong_method.status_code == 400 and wrong_method.json()["error"]["data"] == {
        "code": "header_mismatch"
    }
    stray_name = rpc26(client, "ping", headers={"Mcp-Name": "get_source"})
    assert stray_name.status_code == 400 and stray_name.json()["error"]["data"] == {"code": "header_mismatch"}
    wrong_name = rpc26(
        client,
        "tools/call",
        {"name": "get_coverage", "arguments": {}},
        headers={"Mcp-Name": "get_source"},
    )
    assert wrong_name.status_code == 400 and wrong_name.json()["error"]["data"] == {"code": "header_mismatch"}
    meta_mismatch = rpc26(
        client,
        "ping",
        {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2025-06-18",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
        include_meta=False,
    )
    assert meta_mismatch.status_code == 400 and meta_mismatch.json()["error"]["data"] == {
        "code": "header_mismatch"
    }
    # 5. invalid_meta
    no_meta = rpc26(client, "ping", {}, include_meta=False)
    assert no_meta.status_code == 400 and no_meta.json()["error"]["code"] == -32602
    assert no_meta.json()["error"]["data"] == {"code": "invalid_meta"}
    bad_caps = rpc26(
        client,
        "ping",
        {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": "not-an-object",
            }
        },
        include_meta=False,
    )
    assert bad_caps.status_code == 400 and bad_caps.json()["error"]["data"] == {"code": "invalid_meta"}
    # 6. invalid_cursor & stale_cursor (400 under 2026, 200 under 2025)
    for bad_cur in ("", 123, "not!b64", base64.urlsafe_b64encode(b"{}").decode().rstrip("=")):
        r26 = rpc26(client, "tools/list", {"cursor": bad_cur})
        assert r26.status_code == 400 and r26.json()["error"]["data"] == {"code": "invalid_cursor"}
        r25 = rpc(client, "tools/list", {"cursor": bad_cur})
        assert r25.status_code == 200 and r25.json()["error"]["data"] == {"code": "invalid_cursor"}
    for bad_offset in (-1, True, 999):
        raw = json.dumps({"rev": TOOL_SCHEMA_REVISION, "offset": bad_offset}).encode()
        cur = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        r26 = rpc26(client, "tools/list", {"cursor": cur})
        assert r26.status_code == 400 and r26.json()["error"]["data"] == {"code": "invalid_cursor"}
    stale_raw = json.dumps({"rev": "0000000000000000", "offset": 5}).encode()
    stale_cur = base64.urlsafe_b64encode(stale_raw).rstrip(b"=").decode()
    stale26 = rpc26(client, "tools/list", {"cursor": stale_cur})
    assert stale26.status_code == 400 and stale26.json()["error"]["data"] == {"code": "stale_cursor"}
    stale25 = rpc(client, "tools/list", {"cursor": stale_cur})
    assert stale25.status_code == 200 and stale25.json()["error"]["data"] == {"code": "stale_cursor"}
    # 7. method_not_found: 404 with data under 2026 vs 200 without data under 2025
    unk26 = rpc26(client, "resources/list")
    assert unk26.status_code == 404 and unk26.json()["error"]["code"] == -32601
    assert unk26.json()["error"]["data"] == {"code": "method_not_found"}
    unk25 = rpc(client, "resources/list")
    assert unk25.status_code == 200 and unk25.json()["error"]["code"] == -32601
    assert "data" not in unk25.json()["error"]
    # 8. invalid tool args: 400 with data under 2026 vs 200 without data under 2025
    bad_arg26 = rpc26(client, "tools/call", {"name": "get_source", "arguments": {"source_id": 7}})
    assert bad_arg26.status_code == 400 and bad_arg26.json()["error"]["code"] == -32602
    assert bad_arg26.json()["error"]["data"] == {"code": "invalid_input"}


def test_output_schemas_validate_all_11_sqlite_tools_and_2026_error_envelopes(store, cleared):
    import jsonschema

    from api.mcp_schemas import OUTPUT_SCHEMAS

    store.save(cleared)
    signer = _test_signer()

    @contextmanager
    def session():
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    app = FastAPI()
    app.include_router(create_router(session, (PublicUnavailable,), signer=lambda: signer))
    client = TestClient(app)

    source_id = cleared.sources[0].id
    calls = [
        ("search_catalogue", {"query": "Freedom", "limit": 5}),
        ("get_source", {"source_id": source_id}),
        ("get_relationships", {"source_id": source_id}),
        ("list_expressions", {"source_id": source_id}),
        ("get_anchor", {"anchor_id": "test-node"}),
        ("search_text", {"query": "Synthetic"}),
        ("retrieve_evidence", {"query": "Synthetic fixture"}),
        ("check_currentness", {"items": [{"anchor_id": "test-node"}]}),
        ("resolve_citation", {"citation": "Freedom of Information Act 2011"}),
        ("get_coverage", {}),
        ("check_claim", {"anchor_id": "test-node"}),
    ]
    assert len(calls) == 11
    for tool_name, arguments in calls:
        resp = rpc26(client, "tools/call", {"name": tool_name, "arguments": arguments})
        assert resp.status_code == 200, tool_name
        result = resp.json()["result"]
        assert result["resultType"] == "complete", tool_name
        assert result["isError"] is False, (tool_name, result)
        assert len(result["content"][0]["text"].encode("utf-8")) <= 2048
        jsonschema.Draft202012Validator(OUTPUT_SCHEMAS[tool_name]).validate(result["structuredContent"])

    # Empty scoped searches also validate against OUTPUT_SCHEMAS (with scoped notice populated).
    scoped_app = FastAPI()
    scoped_app.include_router(
        create_router(session, (PublicUnavailable,), lambda request: Reach(frozenset({"rw"})))
    )
    scoped_client = TestClient(scoped_app)
    for tool_name, arguments in (
        ("search_catalogue", {"query": "Freedom"}),
        ("search_text", {"query": "Synthetic"}),
        ("retrieve_evidence", {"query": "Synthetic fixture"}),
    ):
        resp = rpc26(scoped_client, "tools/call", {"name": tool_name, "arguments": arguments})
        result = resp.json()["result"]
        assert result["isError"] is False, tool_name
        jsonschema.Draft202012Validator(OUTPUT_SCHEMAS[tool_name]).validate(result["structuredContent"])

    # 2026 isError: True envelope includes resultType: "complete" alongside structuredContent.
    missing = rpc26(client, "tools/call", {"name": "get_source", "arguments": {"source_id": "missing-id"}})
    assert missing.status_code == 200
    mres = missing.json()["result"]
    assert mres["resultType"] == "complete" and mres["isError"] is True
    assert mres["structuredContent"] == {"code": "unavailable"}


def test_strict_origin_host_front_door_and_law_check_internal_checks(store, catalogue):
    from fastapi import Request

    store.save(catalogue)

    @contextmanager
    def session():
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    app = FastAPI()
    app.include_router(
        create_router(
            session,
            (PublicUnavailable,),
            allowed_origins=frozenset({"https://api.open.alvary.ai", "http://localhost:8022"}),
            allowed_hosts=frozenset({"api.open.alvary.ai", "localhost:8022"}),
            front_door_id="fd-secret",
        )
    )
    client = TestClient(app)

    # Disallowed Host -> 403 10002 host_not_allowed
    bad_host = rpc(client, "ping", host="evil.example")
    assert bad_host.status_code == 403 and bad_host.headers["cache-control"] == "no-store"
    assert bad_host.headers["x-request-id"]
    err = bad_host.json()["error"]
    assert err["code"] == 10002 and err["data"]["code"] == "host_not_allowed"
    assert err["data"]["retryable"] is False and err["data"]["schema_version"] == "1"

    # Allowed Host + Origin: null / scheme mismatch / port mismatch -> 403 10002 origin_not_allowed
    for bad_origin in (
        "null",
        "http://api.open.alvary.ai",
        "https://api.open.alvary.ai:8443",
        "https://evil.example",
    ):
        refused = rpc(client, "ping", host="api.open.alvary.ai", origin=bad_origin)
        assert refused.status_code == 403, bad_origin
        assert refused.json()["error"]["code"] == 10002
        assert refused.json()["error"]["data"]["code"] == "origin_not_allowed"

    # Allowed Host (including standard :443 stripped) + allowed Origin -> 200
    assert (
        rpc(client, "ping", host="api.open.alvary.ai:443", origin="https://api.open.alvary.ai").status_code
        == 200
    )
    # Non-standard port localhost:8022 preserved and allowed
    assert rpc(client, "ping", host="localhost:8022", origin="http://localhost:8022").status_code == 200
    assert rpc(client, "ping", host="localhost:9999").status_code == 403

    # Azure Front Door: validates first trimmed X-Forwarded-Host instead of internal Host
    fd_ok = rpc(
        client,
        "ping",
        host="internal.azurecontainerapps.io",
        **{"X-Azure-FDID": "fd-secret", "X-Forwarded-Host": " api.open.alvary.ai:443 , proxy.internal"},
    )
    assert fd_ok.status_code == 200
    fd_bad = rpc(
        client,
        "ping",
        host="api.open.alvary.ai",
        **{"X-Azure-FDID": "fd-secret", "X-Forwarded-Host": "evil.example"},
    )
    assert fd_bad.status_code == 403 and fd_bad.json()["error"]["data"]["code"] == "host_not_allowed"

    # law_check_internal exempts Host: law-check.internal
    internal_app = FastAPI()

    @internal_app.middleware("http")
    async def mark_internal(request: Request, call_next):
        request.state.law_check_internal = True
        return await call_next(request)

    internal_app.include_router(
        create_router(
            session,
            (PublicUnavailable,),
            allowed_hosts=frozenset({"api.open.alvary.ai"}),
        )
    )
    assert rpc(TestClient(internal_app), "ping", host="law-check.internal").status_code == 200


def test_tool_timeout_returns_503_10004(store, catalogue):
    import time

    store.save(catalogue)

    @contextmanager
    def slow_session():
        time.sleep(0.2)
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    app = FastAPI()
    app.include_router(create_router(slow_session, (PublicUnavailable,), tool_timeout=0.05))
    client = TestClient(app)
    resp = rpc(client, "tools/call", {"name": "get_coverage", "arguments": {}})
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store" and resp.headers["x-request-id"]
    err = resp.json()["error"]
    assert err["code"] == 10004 and err["data"]["code"] == "temporarily_unavailable"
    assert err["data"]["retryable"] is True and err["data"]["schema_version"] == "1"


def test_check_claim_starter_refusal_paid_single_debit_and_request_state_refusal(store, cleared):
    from fastapi import Request

    from api.reach import starter_only

    # Add a second Nigeria source with a similar title so citation resolution can return ambiguous matches.
    corpus = cleared.model_copy(deep=True)
    dup_source = corpus.sources[0].model_copy(
        update={
            "id": "ng-foi-act-2011-dup",
            "title": "Freedom of Information Act 2011",
            "citation": "Freedom of Information Act 2011 (Copy)",
        }
    )
    dup_rights = corpus.rights[0].model_copy(
        update={"id": "rights-dup", "source_id": dup_source.id, "approved_content_hashes": []}
    )
    corpus.sources.append(dup_source)
    corpus.rights.append(dup_rights)
    store.save(corpus)

    signer = _test_signer()

    @contextmanager
    def session():
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    # 1. Starter covering Rwanda ("rw"): must refuse NG claims on input jurisdiction, matched part,
    #    and part-is-None outcomes (ambiguous, no_part_named, part_not_found).
    starter_scope = {"jurisdictions": ["rw"]}
    starter_reach = Reach.of(starter_scope, starter_only(starter_scope))
    refusals = []
    starter_app = FastAPI()

    @starter_app.middleware("http")
    async def capture_refusal(request: Request, call_next):
        resp = await call_next(request)
        refusals.append(getattr(request.state, "refusal", None))
        return resp

    starter_app.include_router(
        create_router(session, (PublicUnavailable,), lambda req: starter_reach, signer=lambda: signer)
    )
    s_client = TestClient(starter_app)

    # Valid input jurisdiction outside Starter -> refused before signing
    for args in (
        {"citation": "Some Unknown Law, s. 1", "jurisdiction": "ng"},  # input jurisdiction
        {"anchor_id": "test-node"},  # matched part by anchor_id
        {"citation": "Freedom of Information Act 2011"},  # ambiguous (part is None)
        {"citation": "Central Bank of Nigeria Act 2007"},  # no_part_named (part is None)
        {"citation": "Freedom of Information Act 2011, s. 999"},  # ambiguous / part_not_found
    ):
        res = rpc(s_client, "tools/call", {"name": "check_claim", "arguments": args}).json()["result"]
        assert res["isError"] is True, args
        assert res["structuredContent"] == {
            "code": "jurisdiction_not_included",
            "retryable": False,
            "manage_url": "https://open.alvary.ai/console/access",
        }, args
        assert "currently Rwanda, rw" in res["content"][0]["text"]
    assert refusals[-5:] == ["jurisdiction_not_included"] * 5

    # Single-source part_not_found (part is None) under Starter
    store_single = store
    store_single.save(cleared)
    pnf = rpc(
        s_client,
        "tools/call",
        {"name": "check_claim", "arguments": {"citation": "Freedom of Information Act 2011, s. 999"}},
    ).json()["result"]
    assert pnf["isError"] is True and pnf["structuredContent"]["code"] == "jurisdiction_not_included"

    # 2. Paid plan covering Rwanda ("rw"): invalid jurisdiction fails -32602 with ZERO debit;
    #    valid outside-plan claim with BOTH outside input jurisdiction and outside source debits ONCE.
    debits = []
    paid_reach = Reach.of({"jurisdictions": ["rw"]}, lambda: debits.append(1))
    paid_app = FastAPI()
    paid_app.include_router(
        create_router(session, (PublicUnavailable,), lambda req: paid_reach, signer=lambda: signer)
    )
    p_client = TestClient(paid_app)

    for bad_jur in (123, "bad!!", "x"):
        err = rpc(
            p_client,
            "tools/call",
            {"name": "check_claim", "arguments": {"anchor_id": "test-node", "jurisdiction": bad_jur}},
        ).json()
        assert err["error"]["code"] == -32602, bad_jur
    assert debits == []  # zero debit on invalid jurisdiction

    ok = rpc(
        p_client,
        "tools/call",
        {"name": "check_claim", "arguments": {"anchor_id": "test-node", "jurisdiction": "ng"}},
    ).json()["result"]
    assert ok["isError"] is False
    assert len(debits) == 1  # single-debit guarantee across input jurisdiction + anchor owner + step 2

    # 3. OutsidePlanLimited sets request.state.refusal = "rate_limited"
    lim_refusals = []
    lim_app = FastAPI()

    @lim_app.middleware("http")
    async def capture_lim(request: Request, call_next):
        resp = await call_next(request)
        lim_refusals.append(getattr(request.state, "refusal", None))
        return resp

    def fail_limit():
        raise OutsidePlanLimited(5)

    lim_app.include_router(
        create_router(session, (PublicUnavailable,), lambda req: Reach(frozenset({"rw"}), fail_limit))
    )
    lim_res = rpc(
        TestClient(lim_app),
        "tools/call",
        {"name": "get_source", "arguments": {"source_id": cleared.sources[0].id}},
    ).json()["result"]
    assert lim_res["isError"] is True and lim_refusals == ["rate_limited"]


def test_scoped_plan_unnamed_search_notices_and_record_list_suppression(store, cleared):
    from fastapi import Request

    store.save(cleared)

    @contextmanager
    def session():
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    scoped = Reach(frozenset({"ng"}))
    app = FastAPI()
    app.include_router(create_router(session, (PublicUnavailable,), lambda req: scoped))
    client = TestClient(app)

    # Unnamed searches on a scoped plan receive the coverage notice.
    cat = json.loads(
        rpc(client, "tools/call", {"name": "search_catalogue", "arguments": {"query": "Freedom"}}).json()[
            "result"
        ]["content"][0]["text"]
    )
    assert any(
        "Nigeria, ng" in n and "https://open.alvary.ai/console/access" in n for n in cat.get("notices", [])
    )
    txt = json.loads(
        rpc(client, "tools/call", {"name": "search_text", "arguments": {"query": "Synthetic"}}).json()[
            "result"
        ]["content"][0]["text"]
    )
    assert any(
        "Nigeria, ng" in n and "https://open.alvary.ai/console/access" in n for n in txt.get("notices", [])
    )
    ev = json.loads(
        rpc(
            client, "tools/call", {"name": "retrieve_evidence", "arguments": {"query": "Synthetic fixture"}}
        ).json()["result"]["content"][0]["text"]
    )
    assert any(
        "Nigeria, ng" in n and "https://open.alvary.ai/console/access" in n
        for n in ev["coverage"].get("notices", [])
    )

    # Named jurisdiction searches do NOT receive the unnamed-search notice.
    cat_named = json.loads(
        rpc(
            client,
            "tools/call",
            {"name": "search_catalogue", "arguments": {"query": "Freedom", "jurisdiction": "ng"}},
        ).json()["result"]["content"][0]["text"]
    )
    assert not any("Search limited" in n for n in cat_named.get("notices", []))

    # Record-list plans (allowed_source_ids is not None) suppress the scoped-plan notice.
    listed_app = FastAPI()

    @listed_app.middleware("http")
    async def attach_listed_access(request: Request, call_next):
        request.state.access = {
            "tier": {"id": "law-check", "allowance": {"source_ids": [cleared.sources[0].id]}}
        }
        return await call_next(request)

    listed_app.include_router(create_router(session, (PublicUnavailable,), lambda req: scoped))
    l_client = TestClient(listed_app)
    cat_listed = json.loads(
        rpc(l_client, "tools/call", {"name": "search_catalogue", "arguments": {"query": "Freedom"}}).json()[
            "result"
        ]["content"][0]["text"]
    )
    assert not any("Search limited" in n for n in cat_listed.get("notices", []))
    ev_listed = json.loads(
        rpc(
            l_client, "tools/call", {"name": "retrieve_evidence", "arguments": {"query": "Synthetic fixture"}}
        ).json()["result"]["content"][0]["text"]
    )
    assert not any("Search limited" in n for n in ev_listed["coverage"].get("notices", []))
