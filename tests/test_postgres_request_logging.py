"""Queries reveal what someone is researching, so nothing the public service logs may carry one (RSV-01,
ACS-06, OA-A2; the privacy notice: no request logs for the API and MCP, and an error never records what
was sent).

The service runs as its image runs it: api.service under uvicorn with the options of the image's CMD,
on the engine production builds (api.store.engine_for), with REST v1 and MCP behind the credential gate
and a search engine and reranker configured but unreachable, so the HTTP client logs too. Every logger
listens at DEBUG while searches, evidence packs, citation lookups, checks and refused requests carry a
marker; no record may hold it, in its message, its arguments or its exception text."""

import base64
import json
import logging
from urllib.parse import quote, urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_api_v1 import PEPPER, TIER, grant, key, rpc
from test_request_logging import MARKER, TRACE, ask, carried, every_log, free_port, image_options, served
from test_v1_text import UNITS, publish_act

from accounts import policies
from accounts.store import Accounts
from api.service import create_service
from api.store import Store, engine_for
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres


@pytest.fixture
def service(pg_engine, monkeypatch):
    monkeypatch.setenv("ALVARY_KEY_PEPPER", base64.b64encode(PEPPER).decode())
    monkeypatch.setenv("ALVARY_ACCOUNTS", "1")
    for name in (
        "ALVARY_CONSOLE_CLIENT_ID",
        "ALVARY_ADMISSION_CONFIG",
        "ALVARY_SITE_ADMISSION_CONFIG",
        "OPEN_ALVARY_VECTOR_PROFILE",
        "PUBLIC_DOMAIN",
    ):
        monkeypatch.delenv(name, raising=False)
    unreachable = f"http://127.0.0.1:{free_port()}"
    monkeypatch.setenv("OPEN_ALVARY_ENGINE_URL", unreachable)
    monkeypatch.setenv("OPEN_ALVARY_RERANKER_URL", unreachable)
    monkeypatch.setenv("OPEN_ALVARY_RERANKER_PROFILE", "test")
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        schema = conn.scalar(text("SELECT current_schema()"))
    url = pg_engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = engine_for(url.render_as_string(hide_password=False))
    store = Store(engine)
    _, _, nodes = publish_act(store, UNITS)
    accounts = Accounts(engine, PolicyNamespace(schema), key_pepper=PEPPER)
    accounts.set_tiers([TIER])
    signed = accounts.sign_in(
        issuer="https://login.example.test/",
        subject="email|a",
        method="email",
        email="a@example.org",
        label="a@example.org",
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    organisation = accounts.welcome(signed["id"], "Amina", "Amina")
    grant(accounts, organisation)
    yield create_service(store=store), key(accounts, signed["id"], organisation), nodes
    engine.dispose()


def test_no_log_carries_a_query_or_a_citation_even_when_the_request_fails(service):
    app, credential, nodes = service
    m, v1 = MARKER, "/api/v1"
    requests = [  # (method, path, body, status); REST v1 with a key unless 401 is expected
        ("GET", f"{v1}/search/text?" + urlencode({"q": f"salary {m}"}), None, 200),
        ("GET", f"{v1}/retrieve?" + urlencode({"q": f"repaid from salary {m}"}), None, 200),
        ("GET", f"{v1}/citations/" + quote(f"s. 2 Example Act {m}"), None, 200),
        ("GET", f"{v1}/search?" + urlencode({"q": m}), None, 200),
        ("POST", f"{v1}/currentness", {"items": [{"anchor_id": nodes["sec_1"].id}, {"anchor_id": m}]}, 200),
        # Refused: out of range, not a date, too long, not a language, not a check, not JSON.
        ("GET", f"{v1}/search/text?" + urlencode({"q": m, "limit": 0}), None, 400),
        ("GET", f"{v1}/retrieve?" + urlencode({"q": m, "as_of": m}), None, 400),
        ("GET", f"{v1}/retrieve?" + urlencode({"q": m * 250}), None, 400),
        ("GET", f"{v1}/citations/{m}?" + urlencode({"language": m + "!"}), None, 400),
        ("POST", f"{v1}/currentness", {"items": [{"locator": m}]}, 400),
        ("POST", f"{v1}/currentness", b'{"items": ["' + m.encode(), 400),
        # Not found.
        ("GET", f"{v1}/sources/{m}", None, 404),
        ("GET", f"{v1}/anchors/{m}", None, 404),
        ("GET", f"{v1}/versions/{m}/text", None, 404),
        # Without a key, and with one in the address.
        ("GET", f"{v1}/search/text?" + urlencode({"q": m}), None, 401),
        ("GET", f"{v1}/search/text?" + urlencode({"q": m, "key": m}), None, 400),
        # The website's own search, and the anonymous API activation B closed.
        ("GET", "/site/v1/search?" + urlencode({"q": m}), None, 200),
        ("GET", "/api/search?" + urlencode({"q": m}), None, 410),
    ]
    tools = [  # (name, arguments, refused)
        ("search_text", {"query": f"salary {m}"}, False),
        ("retrieve_evidence", {"query": f"repaid from salary {m}"}, False),
        ("resolve_citation", {"citation": f"s. 2 Example Act {m}"}, False),
        ("check_currentness", {"items": [{"anchor_id": m}]}, False),
        ("search_catalogue", {"query": m}, False),
        ("search_text", {"query": m, "limit": 99}, True),
        ("retrieve_evidence", {"query": m, "as_of": m}, True),
        ("get_source", {"source_id": m}, True),
        ("get_anchor", {"anchor_id": m}, True),
        (m, {"query": m}, True),
    ]
    with every_log() as records, served(app, image_options("deployment/public-service.Dockerfile")) as port:
        for method, path, body, status in requests:
            keyed = path.startswith(v1) and status != 401
            answered, answer = ask(port, method, path, body, credential if keyed else {})
            assert answered == status, (path, answer)
            # A refusal never repeats what was sent (ACS: a safe detail, no URL query).
            assert status == 200 or TRACE not in answer.lower(), answer
        for name, arguments, refused in tools:
            call = rpc("tools/call", {"name": name, "arguments": arguments})
            answered, answer = ask(port, "POST", "/mcp", call, credential)
            message = json.loads(answer)
            assert answered == 200 and ("error" in message or message["result"]["isError"]) is refused, answer
            assert not refused or TRACE not in answer.lower(), answer
        unparsed = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "' + m.encode()
        answered, answer = ask(port, "POST", "/mcp", unparsed, credential)
        assert answered == 400 and TRACE not in answer.lower(), answer
    # Every source was heard: the server, the service's engine (its statements, values hidden) and the HTTP
    # client the search engine and reranker use. uvicorn wrote no access log.
    names = {r.name for r in records}
    assert {"uvicorn.error", "sqlalchemy.engine.Engine.open_alvary", "httpcore.connection"} <= names, names
    assert "uvicorn.access" not in names
    assert [(r.name, logging.Formatter().format(r)) for r in records if carried(r)] == []


def test_rest_v1_mcp_and_the_console_keep_a_pages_address_from_other_sites(service):
    """Nor may a browser pass a page's address, with its query, to a site it links to: activation B's
    answers carry Referrer-Policy as the website's do (tests/test_site.py; owner, 2026-10-21)."""
    app, credential, _ = service
    client = TestClient(app)
    search = "/api/v1/search?" + urlencode({"q": MARKER})
    answers = {
        "REST v1": client.get(search, headers=credential),
        "REST v1, refused": client.get(search),
        "MCP": client.post("/mcp", json=rpc("tools/list"), headers=credential),
        "console, signed out": client.get("/console/api/v1/me"),
        "resource metadata": client.get("/.well-known/oauth-protected-resource/mcp"),
    }
    assert {name: answer.status_code for name, answer in answers.items()} == {
        "REST v1": 200,
        "REST v1, refused": 401,
        "MCP": 200,
        "console, signed out": 401,
        "resource metadata": 200,
    }
    policies = {name: answer.headers.get("referrer-policy") for name, answer in answers.items()}
    assert policies == dict.fromkeys(answers, "strict-origin-when-cross-origin")
