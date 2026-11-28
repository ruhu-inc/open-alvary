"""Consolidated findings item 8: no route reads more of a request body than its limit."""

import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.bodylimit import DEFAULT, BodyLimit, limit_for
from api.service import create_service

EIGHT_MIB = b"x" * (8 * 1024 * 1024)


@pytest.fixture
def service(store, tmp_path):
    return TestClient(create_service(store, tmp_path))


@pytest.mark.parametrize("path", ["/console/oauth/api/decision", "/console/mail-events", "/api/v1/retrieve"])
def test_a_body_over_the_limit_is_refused_before_it_is_read(service, path):
    answer = service.post(path, content=EIGHT_MIB, headers={"Content-Type": "application/json"})
    assert answer.status_code == 413
    assert answer.json()["code"] == "request_too_large"
    assert answer.headers["content-type"] == "application/problem+json"


def test_each_route_has_its_limit():
    assert limit_for("/console/mail-events") == 64 * 1024
    assert limit_for("/console/oauth/api/decision") == 16 * 1024
    assert limit_for("/console/oauth/api/decisions") == DEFAULT
    assert limit_for("/mcp") == 32 * 1024


def test_mcp_body_over_32_kib_returns_jsonrpc_413_for_content_length_and_chunked():
    from api.bodylimit import TooLarge, mcp_too_large_response

    app = FastAPI()

    @app.exception_handler(TooLarge)
    async def handle_too_large(request: Request, exc: TooLarge):
        return mcp_too_large_response(getattr(request.state, "request_id", None) or "req-chunked", exc.limit)

    @app.post("/mcp")
    async def mcp_echo(request: Request):
        return {"read": len(await request.body())}

    wrapped = BodyLimit(app)
    # Content-Length > 32 KiB: refused before body is read, with JSON-RPC 10005 envelope.
    client = TestClient(wrapped)
    over = b"x" * (32 * 1024 + 1)
    declared = client.post("/mcp", content=over, headers={"Content-Type": "application/json"})
    assert declared.status_code == 413
    assert declared.headers["content-type"] == "application/json"
    assert declared.headers["cache-control"] == "no-store"
    assert declared.headers["connection"] == "close"
    rid = declared.headers["x-request-id"]
    assert rid
    body = declared.json()
    assert body == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": 10005,
            "message": "Request body exceeds 32,768 bytes.",
            "data": {
                "code": "request_too_large",
                "retryable": False,
                "request_id": rid,
                "schema_version": "1",
            },
        },
    }

    # Chunked (no Content-Length) > 32 KiB: stopped at limit and returned in the same JSON-RPC 10005 shape.
    receive, pulled = stream(chunks=8, size=16 * 1024)
    sent = run(wrapped, "/mcp", receive)
    assert sent[0]["status"] == 413
    assert pulled["chunks"] <= 3
    headers = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert headers["connection"] == "close"
    chunk_rid = headers["x-request-id"]
    assert chunk_rid
    chunk_body = json.loads(b"".join(m.get("body", b"") for m in sent[1:]))
    assert chunk_body == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": 10005,
            "message": "Request body exceeds 32,768 bytes.",
            "data": {
                "code": "request_too_large",
                "retryable": False,
                "request_id": chunk_rid,
                "schema_version": "1",
            },
        },
    }

    # Also test unhandled TooLarge inside BodyLimit directly on /mcp (without route exception handler).
    async def bare_app(scope, receive, send):
        while True:
            msg = await receive()
            if not msg.get("more_body", False):
                break

    receive2, _ = stream(chunks=4, size=16 * 1024)
    sent2 = run(BodyLimit(bare_app), "/mcp", receive2)
    assert sent2[0]["status"] == 413
    headers2 = {k.decode().lower(): v.decode() for k, v in sent2[0]["headers"]}
    assert headers2["content-type"] == "application/json"
    assert headers2["cache-control"] == "no-store"
    assert headers2["connection"] == "close"
    body2 = json.loads(sent2[-1]["body"])
    assert body2["error"]["code"] == 10005 and body2["error"]["data"]["schema_version"] == "1"


def stream(chunks: int, size: int = 64 * 1024):
    """A body sent without Content-Length, in chunks, counting how many the app pulled."""
    pulled = {"chunks": 0}

    async def receive():
        pulled["chunks"] += 1
        more = pulled["chunks"] < chunks
        return {"type": "http.request", "body": b"x" * size, "more_body": more}

    return receive, pulled


def run(app, path, receive):
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
    asyncio.run(app(scope, receive, send))
    return sent


def reader_app():
    app = FastAPI()

    @app.post("/console/mail-events")
    async def events(request: Request):
        return {"read": len(await request.body())}

    return app


def test_a_body_without_its_length_is_read_only_up_to_the_limit_then_refused():
    receive, pulled = stream(chunks=128)  # 8 MiB in 64 KiB chunks
    sent = run(BodyLimit(reader_app()), "/console/mail-events", receive)
    assert sent[0]["status"] == 413
    assert pulled["chunks"] <= 2  # the limit is one chunk: the second goes over and stops the read
    # Refused inside the route, it is the app's own answer to a 413, in the app's own shape.
    assert "larger than this address accepts" in sent[-1]["body"].decode()


def test_a_body_within_the_limit_reaches_the_route_whole():
    receive, _ = stream(chunks=1, size=1000)
    sent = run(BodyLimit(reader_app()), "/console/mail-events", receive)
    assert sent[0]["status"] == 200 and json.loads(sent[-1]["body"]) == {"read": 1000}
