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
    assert limit_for("/mcp") == DEFAULT


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
