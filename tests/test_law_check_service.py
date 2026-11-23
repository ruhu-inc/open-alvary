"""Service-level tests for Law check (/agents/mcp), keyed MCP additions, headers, and traffic wiring."""

import json
import logging
from contextlib import contextmanager
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.routing import Route

from accounts import keys
from api.admission import AdmissionConfig
from api.catalogue import LegacyCatalogue
from api.credentials import CredentialGate, FailedKeys, GateConfig
from api.law_check import ALLOWED_TOOLS, LawCheckMCP
from api.mcp import TOOLS as KEYED_TOOLS
from api.mcp import create_router as create_mcp_router
from api.receipts import ReceiptSigner, verify_receipt
from api.repositories import LegacyRepository, PublicUnavailable
from api.service import AGENTS_BRIDGE_CSP, AGENTS_CSP, DEFAULT_CSP, SANDBOX_WORKER_CSP, STATIC_PAGES, create_service
from api.traffic import page_kind
from schema.models import SourceVersion, StructureNode

RESOURCE = "https://api.example.test/mcp"
METADATA = "https://api.example.test/.well-known/oauth-protected-resource/mcp"
PEPPER = b"p" * 32


def _make_signer() -> tuple[ReceiptSigner, bytes]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    signer = ReceiptSigner(pem, kid="oa-receipt-test-1", valid_from="2026-01-01T00:00:00Z")
    return signer, pem


def _admission_config(*, capacity=40, identity_concurrency=4, global_concurrency=10, queue_size=2) -> AdmissionConfig:
    return AdmissionConfig(
        policy_revision="test-rev",
        capacity=capacity,
        refill_per_minute=20.0,
        identity_concurrency=identity_concurrency,
        global_concurrency=global_concurrency,
        lease_seconds=30,
        queue_size=queue_size,
        queue_deadline_seconds=0.05,
        trusted_proxies=0,
        hmac_secret=b"s" * 32,
        previous_hmac_secret=None,
        db_connections=50,
        operator_reserve=5,
        other_workers=5,
        public_pool_size=15,
        api_replicas=1,
    )


def _law_check_access(source_ids, *, tier_id="law-check", withdrawals="not_told"):
    allowance = {
        "requests_per_minute": 300,
        "burst": 100,
        "concurrency": 10,
        "bytes_per_day": 250_000_000,
        "withdrawals": withdrawals,
        "source_ids": list(source_ids) if source_ids is not None else None,
    }
    if source_ids is None:
        allowance.pop("source_ids")
    return {
        "ok": True,
        "organisation": "org_lawcheck0000000000000000",
        "credential": "key_" + "a" * 32,
        "tier": {"id": tier_id, "includes": ["mcp"], "allowance": allowance},
        "scope": {"all": True},
    }


def _build_mcp_stack(
    store,
    check,
    *,
    signer=None,
    usage=None,
    admission_store=None,
    agents_admission_config=None,
    agents_admission_store=None,
    key_provider=None,
    clock=None,
    failed_keys=None,
    require_admission=False,
):
    mcp_app = FastAPI()

    @contextmanager
    def catalogue_session():
        with store.engine.begin() as connection:
            yield LegacyCatalogue(LegacyRepository(connection))

    mcp_app.include_router(
        create_mcp_router(
            catalogue_session,
            (SQLAlchemyError, ValidationError, PublicUnavailable),
            lambda req: req.state.access["reach"],
            signer=(lambda: signer) if signer is not None else None,
        )
    )
    gate = CredentialGate(
        mcp_app,
        config=GateConfig(RESOURCE, "mcp", "mcp", METADATA, jsonrpc=True),
        check=check,
        record_usage=(usage.append if usage is not None else lambda entry: None),
        admission=admission_store,
        verifier=None,
        key_pepper=PEPPER,
        failed_keys=failed_keys or FailedKeys(),
    )
    law_check = LawCheckMCP(
        gate,
        admission_config=agents_admission_config,
        admission_store=agents_admission_store,
        key_provider=key_provider,
        clock=clock or (lambda: 100.0),
        require_admission=require_admission,
    )
    parent = FastAPI()
    parent.router.routes.append(Route("/mcp", gate, methods=["GET", "POST", "DELETE"]))
    parent.router.routes.append(Route("/agents/mcp", law_check, methods=["GET", "POST", "OPTIONS", "DELETE"]))
    return TestClient(parent, client=("203.0.113.10", 50000)), gate, law_check


def _rpc(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return body


def test_preflight_cors_and_handshakes_succeed_even_when_paused(store):
    checks = []

    def check(_asked):
        checks.append(_asked)
        return {"ok": False, "code": "invalid_credentials"}

    client, _, _ = _build_mcp_stack(store, check, key_provider=lambda: None, require_admission=True)

    opt = client.options("/agents/mcp", headers={"Origin": "https://other.example"})
    assert opt.status_code == 204
    assert opt.headers["access-control-allow-origin"] == "*"
    assert "POST" in opt.headers["access-control-allow-methods"]
    assert "OPTIONS" in opt.headers["access-control-allow-methods"]
    assert opt.headers["access-control-max-age"] == "86400"
    assert "MCP-Protocol-Version" in opt.headers["access-control-allow-headers"]
    assert opt.headers["access-control-expose-headers"] == "Mcp-Session-Id"
    assert opt.headers["cache-control"] == "no-store"

    init = client.post("/agents/mcp", json=_rpc("initialize", {"protocolVersion": "2025-06-18"}))
    assert init.status_code == 200
    assert init.headers["access-control-allow-origin"] == "*"
    assert init.json()["result"]["protocolVersion"] == "2025-06-18"

    ping = client.post("/agents/mcp", json=_rpc("ping"))
    assert ping.status_code == 200 and ping.json()["result"] == {}

    notif = client.post("/agents/mcp", json=_rpc("notifications/initialized", id_=None))
    assert notif.status_code == 202
    assert notif.headers["access-control-allow-origin"] == "*"

    listed = client.post("/agents/mcp", json=_rpc("tools/list"))
    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert {t["name"] for t in tools} == ALLOWED_TOOLS
    assert len(tools) == 5
    for tool in tools:
        desc = tool["description"].lower()
        assert "withdrawn" not in desc and "withdrawal" not in desc
        assert "rest " not in desc and "/api/v1" not in desc
        assert "check_currentness" not in desc

    # Zero database/gate checks ran for any handshake.
    assert checks == []


def test_request_rejections_carry_cors_and_cookies_are_ignored(store):
    secret = keys.generate().secret
    client, _, _ = _build_mcp_stack(
        store, lambda _: _law_check_access([]), key_provider=lambda: secret
    )

    # Authorization header refused with 400 credential_not_accepted + CORS.
    auth = client.post(
        "/agents/mcp",
        json=_rpc("ping"),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert auth.status_code == 400
    assert auth.headers["access-control-allow-origin"] == "*"
    assert auth.json()["error"]["data"]["code"] == "credential_not_accepted"

    # Batch refused with 400 + CORS.
    batch = client.post("/agents/mcp", json=[_rpc("ping")])
    assert batch.status_code == 400
    assert batch.headers["access-control-allow-origin"] == "*"

    # Body > 16 KiB refused with 413 + CORS.
    huge = json.dumps(_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "a" * 17000}}))
    big = client.post("/agents/mcp", content=huge.encode("utf-8"), headers={"Content-Type": "application/json"})
    assert big.status_code == 413
    assert big.headers["access-control-allow-origin"] == "*"
    assert big.json()["error"]["data"]["code"] == "request_too_large"

    # Cookies are ignored (not refused).
    with_cookie = client.post("/agents/mcp", json=_rpc("ping"), headers={"Cookie": "session=abc123"})
    assert with_cookie.status_code == 200 and with_cookie.json()["result"] == {}


def test_tool_filtering_and_bounds(cleared, store):
    store.save(cleared)
    secret = keys.generate().secret
    source_id = cleared.sources[0].id
    checks = []

    def check(asked):
        checks.append(asked)
        return _law_check_access([source_id])

    client, _, _ = _build_mcp_stack(store, check, key_provider=lambda: secret)

    # Tool outside the five -> -32602 and never reaches the gate.
    refused = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "get_source", "arguments": {"source_id": source_id}}),
    )
    assert refused.status_code == 200
    assert refused.json()["error"]["code"] == -32602
    assert checks == []

    # retrieve_evidence bounds > 5 units or > 12288 bytes -> -32602 before gate.
    too_many = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "retrieve_evidence", "arguments": {"query": "test", "max_units": 6}}),
    )
    assert too_many.status_code == 200 and too_many.json()["error"]["code"] == -32602
    too_big = client.post(
        "/agents/mcp",
        json=_rpc(
            "tools/call",
            {"name": "retrieve_evidence", "arguments": {"query": "test", "max_evidence_bytes": 12289}},
        ),
    )
    assert too_big.status_code == 200 and too_big.json()["error"]["code"] == -32602
    assert checks == []

    # get_anchor with > 8 KiB of words -> result_too_large tool error.
    big_text = "Section 1. " + ("word " * 2000)  # > 8 KiB
    digest = sha256(big_text.encode()).hexdigest()
    big_corpus = cleared.model_copy(deep=True)
    big_corpus.versions.append(
        SourceVersion(
            **{
                **big_corpus.versions[0].model_dump(),
                "id": "test-version-big",
                "raw_hash": digest,
                "content_hash": digest,
                "canonical_text": big_text,
            }
        )
    )
    big_corpus.structure.append(
        StructureNode(
            **{
                **big_corpus.structure[0].model_dump(),
                "id": "test-node-big",
                "version_id": "test-version-big",
                "end_byte": len(big_text.encode()),
                "source_hash": digest,
            }
        )
    )
    big_corpus.rights[0].approved_content_hashes = [
        *big_corpus.rights[0].approved_content_hashes,
        digest,
    ]
    store.save(big_corpus)

    anchor_resp = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "get_anchor", "arguments": {"anchor_id": "test-node-big"}}),
    )
    assert anchor_resp.status_code == 200
    res = anchor_resp.json()["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["code"] == "result_too_large"
    assert res["content"][0]["text"].startswith("result_too_large:")


def test_gate_usage_inner_headers_stripped_and_error_mapping(cleared, store):
    store.save(cleared)
    secret = keys.generate().secret
    source_id = cleared.sources[0].id
    signer, _ = _make_signer()
    usage = []
    failed_keys = FailedKeys()
    now = [100.0]

    state = {"mode": "ok"}

    def check(_asked):
        state["calls"] = state.get("calls", 0) + 1
        if state["mode"] == "ok":
            return _law_check_access([source_id])
        if state["mode"] == "revoked":
            return {"ok": False, "code": "invalid_credentials"}
        if state["mode"] == "starter":
            return _law_check_access(None, tier_id="starter", withdrawals="told")
        return {"ok": False, "code": "temporarily_unavailable"}

    class FakeAdmission:
        def __init__(self):
            self.admit_result = {"admitted": True, "lease_id": "lease-1", "buckets": {}}

        def admit(self, _req):
            return self.admit_result

        def release(self, _lease):
            pass

    gate_admission = FakeAdmission()
    client, _, _ = _build_mcp_stack(
        store,
        check,
        signer=signer,
        usage=usage,
        admission_store=gate_admission,
        key_provider=lambda: secret,
        clock=lambda: now[0],
        failed_keys=failed_keys,
    )

    # 1. Successful check_claim: counted under Law check org/key/mcp, inner headers stripped, receipt verifies.
    resp = client.post(
        "/agents/mcp",
        json=_rpc(
            "tools/call",
            {
                "name": "check_claim",
                "arguments": {
                    "anchor_id": "test-node",
                    "quote": "Synthetic fixture — not legislation.\nSection 1. Test only.",
                },
            },
        ),
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    for forbidden in ("ratelimit", "ratelimit-policy", "x-request-id", "www-authenticate"):
        assert forbidden not in resp.headers
    result = resp.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    verified = verify_receipt(payload["receipt"], signer.jwks(), claim=payload["claim"])
    assert verified["ok"] is True
    assert len(usage) == 1
    assert usage[0]["organisation"] == "org_lawcheck0000000000000000"
    assert usage[0]["class"] == "mcp"

    # 2. Gate 429 -> HTTP 200 tool error service_rate_limited with seconds.
    gate_admission.admit_result = {"admitted": False, "blocked": ["org:lawcheck"], "retry_after": 17}
    r429 = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "Act"}}),
    )
    assert r429.status_code == 200
    err429 = r429.json()["result"]
    assert err429["isError"] is True
    assert err429["structuredContent"] == {"code": "service_rate_limited", "retry_after_seconds": 17}
    assert err429["content"][0]["text"].startswith("service_rate_limited:")
    gate_admission.admit_result = {"admitted": True, "lease_id": "lease-1", "buckets": {}}

    # 3. Plan mismatch (starter fallback) -> 403 inside mcp.py -> paused + 5s cache armed!
    state["mode"] = "starter"
    state["calls"] = 0
    r_mismatch = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "Act"}}),
    )
    assert r_mismatch.status_code == 200
    assert r_mismatch.json()["result"]["structuredContent"]["code"] == "paused"
    calls_after_first = state["calls"]
    assert calls_after_first >= 1

    # Within 5 seconds, zero gate checks!
    now[0] = 103.0
    r_cached = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "Act"}}),
    )
    assert r_cached.status_code == 200
    assert r_cached.json()["result"]["structuredContent"]["code"] == "paused"
    assert state["calls"] == calls_after_first

    # After 5 seconds, re-checks; revoked key -> paused, and failed_keys stays empty (client=None).
    now[0] = 106.0
    state["mode"] = "revoked"
    r_revoked = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "Act"}}),
    )
    assert r_revoked.status_code == 200
    assert r_revoked.json()["result"]["structuredContent"]["code"] == "paused"
    assert state["calls"] > calls_after_first
    assert failed_keys._seen == {}


def test_per_visitor_admission_and_structured_logs_never_log_arguments(cleared, store, caplog):
    store.save(cleared)
    secret = keys.generate().secret
    source_id = cleared.sources[0].id

    class VisitorAdmission:
        def __init__(self):
            self.response = {"admitted": True, "lease_id": "v-1"}

        def admit(self, _req):
            return self.response

        def release(self, _lease):
            pass

    visitor_store = VisitorAdmission()
    client, _, _ = _build_mcp_stack(
        store,
        lambda _: _law_check_access([source_id]),
        agents_admission_config=_admission_config(),
        agents_admission_store=visitor_store,
        key_provider=lambda: secret,
        require_admission=True,
    )

    # Visitor rate limited -> HTTP 200 rate_limited with seconds.
    visitor_store.response = {"admitted": False, "blocked": ["agents:ip-abc"], "retry_after": 23}
    with caplog.at_level(logging.INFO, logger="open_alvary.law_check"):
        r_lim = client.post(
            "/agents/mcp",
            json=_rpc(
                "tools/call",
                {
                    "name": "check_claim",
                    "arguments": {
                        "citation": "SECRET_CLIENT_CITATION_XYZ",
                        "quote": "SECRET_PERSONAL_QUOTE_ABC",
                        "reading": "SECRET_READING_123",
                    },
                },
            ),
        )
    assert r_lim.status_code == 200
    res = r_lim.json()["result"]
    assert res["isError"] is True
    assert res["structuredContent"] == {"code": "rate_limited", "retry_after_seconds": 23}
    assert res["content"][0]["text"].startswith("rate_limited:")

    # Verify structured JSON log has required fields and ZERO argument content.
    assert caplog.records
    entry = json.loads(caplog.records[-1].message)
    assert entry["tool"] == "check_claim"
    assert entry["outcome"] == "rate_limited"
    assert isinstance(entry["bytes"], int)
    assert isinstance(entry["duration_ms"], (int, float))
    assert isinstance(entry["request_id"], str)
    all_logs = "\n".join(r.message for r in caplog.records)
    assert "SECRET_CLIENT_CITATION_XYZ" not in all_logs
    assert "SECRET_PERSONAL_QUOTE_ABC" not in all_logs
    assert "SECRET_READING_123" not in all_logs

    # Global concurrency exhausted & deadline exceeded -> temporarily_unavailable.
    visitor_store.response = {"admitted": False, "blocked": ["agents:global-all"], "retry_after": 1}
    r_busy = client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "Act"}}),
    )
    assert r_busy.status_code == 200
    assert r_busy.json()["result"]["structuredContent"]["code"] == "temporarily_unavailable"


def test_source_ids_scoping_and_keyed_mcp_thirteenth_tool(cleared, store):
    store.save(cleared)
    secret = keys.generate().secret
    signer, _ = _make_signer()
    covered_id = cleared.sources[0].id
    off_list_id = cleared.sources[1].id
    assert covered_id != off_list_id

    # Keyed /mcp lists 13 tools including check_claim and still refuses cross-origin Origin.
    keyed_client, _, _ = _build_mcp_stack(
        store,
        lambda _: _law_check_access(None, tier_id="developer", withdrawals="told"),
        signer=signer,
        key_provider=lambda: secret,
    )
    assert len(KEYED_TOOLS) == 13
    keyed_list = keyed_client.post("/mcp", json=_rpc("tools/list"), headers={"Authorization": f"Bearer {secret}"})
    assert keyed_list.status_code == 200
    assert len(keyed_list.json()["result"]["tools"]) == 13
    assert any(t["name"] == "check_claim" for t in keyed_list.json()["result"]["tools"])

    cross_origin = keyed_client.post(
        "/mcp",
        json=_rpc("ping"),
        headers={"Authorization": f"Bearer {secret}", "Origin": "https://evil.example", "Host": "api.open.alvary.ai"},
    )
    assert cross_origin.status_code == 403

    # Scoped law-check plan covering ONLY covered_id:
    scoped_client, _, _ = _build_mcp_stack(
        store,
        lambda _: _law_check_access([covered_id]),
        signer=signer,
        key_provider=lambda: secret,
    )
    # search_catalogue returns only covered_id and exact filtered total.
    sr = scoped_client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "Act"}}),
    )
    sr_data = json.loads(sr.json()["result"]["content"][0]["text"])
    assert all(item["id"] == covered_id for item in sr_data["items"])
    assert sr_data["total"] == len(sr_data["items"])

    # Empty source_ids: [] returns 0 results for search_catalogue and unavailable for get_anchor.
    empty_client, _, _ = _build_mcp_stack(
        store,
        lambda _: _law_check_access([]),
        signer=signer,
        key_provider=lambda: secret,
    )
    empty_sr = empty_client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "search_catalogue", "arguments": {"query": "Act"}}),
    )
    empty_data = json.loads(empty_sr.json()["result"]["content"][0]["text"])
    assert empty_data["total"] == 0 and empty_data["items"] == []

    off_anchor = empty_client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "get_anchor", "arguments": {"anchor_id": "test-node"}}),
    )
    assert off_anchor.json()["result"]["isError"] is True
    assert off_anchor.json()["result"]["structuredContent"]["code"] == "unavailable"

    # check_claim on an off-list public record -> unavailable (no receipt); never-held -> signed not_in_catalogue.
    off_title = cleared.sources[1].title
    off_claim = scoped_client.post(
        "/agents/mcp",
        json=_rpc("tools/call", {"name": "check_claim", "arguments": {"citation": f"{off_title}, s. 1"}}),
    )
    assert off_claim.json()["result"]["isError"] is True
    assert off_claim.json()["result"]["structuredContent"]["code"] == "unavailable"

    unknown_claim = scoped_client.post(
        "/agents/mcp",
        json=_rpc(
            "tools/call",
            {"name": "check_claim", "arguments": {"citation": "Imaginary Nonexistent Act 1999, s. 99"}},
        ),
    )
    assert unknown_claim.json()["result"]["isError"] is False
    unk_payload = json.loads(unknown_claim.json()["result"]["content"][0]["text"])
    assert unk_payload["receipt"]["status"] == "not_in_catalogue"
    assert verify_receipt(unk_payload["receipt"], signer.jwks(), claim=unk_payload["claim"])["ok"] is True


def test_service_headers_jwks_static_files_and_traffic(store, tmp_path, monkeypatch):
    signer, pem = _make_signer()
    monkeypatch.setenv("ALVARY_RECEIPT_SIGNING_KEY", pem.decode("utf-8"))
    monkeypatch.setenv("ALVARY_RECEIPT_KID", "oa-receipt-test-1")
    monkeypatch.delenv("ALVARY_RECEIPT_KEYS_JSON", raising=False)

    # Create minimal frontend tree.
    (tmp_path / "index.html").write_text("<html>root</html>")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "index.html").write_text("<html>agents</html>")
    (tmp_path / "agents" / "records.json").write_text('{"records":[]}')
    (tmp_path / "agents" / "runtime").mkdir()
    (tmp_path / "agents" / "runtime" / "sandbox-worker.js").write_text("// worker")
    (tmp_path / "agents" / "bridge").mkdir()
    (tmp_path / "agents" / "bridge" / "index.html").write_text("<html>bridge</html>")

    app = create_service(store=store, frontend=tmp_path)
    client = TestClient(app)

    # /.well-known/open-alvary-receipt-keys.json
    jwks_resp = client.get("/.well-known/open-alvary-receipt-keys.json")
    assert jwks_resp.status_code == 200
    assert jwks_resp.headers["access-control-allow-origin"] == "*"
    assert jwks_resp.headers["cache-control"] == "max-age=300"
    assert jwks_resp.json()["keys"][0]["kid"] == "oa-receipt-test-1"

    # /agents/ CSP
    agents_page = client.get("/agents/")
    assert agents_page.status_code == 200
    assert agents_page.text == "<html>agents</html>"
    assert agents_page.headers["content-security-policy"] == AGENTS_CSP

    # /agents/runtime/sandbox-worker.js worker CSP
    worker = client.get("/agents/runtime/sandbox-worker.js")
    assert worker.status_code == 200
    assert worker.headers["content-security-policy"] == SANDBOX_WORKER_CSP

    # /agents/bridge/ frameable (no frame-ancestors 'none' and no X-Frame-Options)
    bridge = client.get("/agents/bridge/")
    assert bridge.status_code == 200
    assert bridge.headers["content-security-policy"] == AGENTS_BRIDGE_CSP
    assert "frame-ancestors" not in bridge.headers["content-security-policy"]
    assert "x-frame-options" not in bridge.headers

    # /agents/**/*.json CORS * + Cache-Control: max-age=300
    rec = client.get("/agents/records.json")
    assert rec.status_code == 200
    assert rec.headers["access-control-allow-origin"] == "*"
    assert rec.headers["cache-control"] == "max-age=300"

    # Missing file under /agents/ returns 404 (no SPA fallback to root index.html)
    missing = client.get("/agents/does-not-exist")
    assert missing.status_code == 404

    # Normal page keeps default CSP
    home = client.get("/")
    assert home.status_code == 200
    assert home.headers["content-security-policy"] == DEFAULT_CSP

    # /agents/mcp is mounted and answers handshake even without accounts_on
    mcp_ping = client.post("/agents/mcp", json=_rpc("ping"))
    assert mcp_ping.status_code == 200 and mcp_ping.json()["result"] == {}

    # Traffic and sitemap wiring
    assert page_kind("/agents") == "agents"
    assert page_kind("/agents/") == "agents"
    assert page_kind("/agents/check/law_check") == "agents"
    assert "/agents/" in STATIC_PAGES
