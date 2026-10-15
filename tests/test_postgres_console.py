"""MAC-08/MAC-10 console back end against PostgreSQL, with a stand-in identity provider."""

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import policies
from accounts.store import Accounts
from api.console import ConsoleConfig, Provider, create_console_app
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
ISSUER = "https://login.example.test/"
BASE = "https://open.example.test"
TIER = {
    "id": "pilot",
    "name": "Pilot",
    "description": "Test",
    "includes": ["rest", "mcp", "bulk"],
    "allowance": {"requests_per_minute": 60},
    "published": True,
}


class FakeProvider:
    """Discovery and a token endpoint that answers each code with the claims a test registered."""

    def __init__(self):
        self.codes: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": ISSUER + "authorize",
                    "token_endpoint": ISSUER + "oauth/token",
                },
            )
        form = parse_qs(request.content.decode())
        claims = self.codes.pop(form["code"][0], None)
        if claims is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        return httpx.Response(200, json={"id_token": f"e30.{payload}.sig"})


@pytest.fixture
def console(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    accounts = Accounts(pg_engine, namespace, key_pepper=b"k" * 32)
    accounts.set_tiers([TIER])
    fake = FakeProvider()
    config = ConsoleConfig(issuer=ISSUER, client_id="console", client_secret="secret", base_url=BASE)
    provider = Provider(config, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_console_app(accounts, provider)
    return accounts, fake, app


def mounted(app):
    parent = FastAPI()
    parent.mount("/console", app)
    return parent


def client(app):
    return TestClient(mounted(app), base_url=BASE + "/console", follow_redirects=False)


def sign_in(
    browser,
    fake,
    *,
    sub="google-oauth2|amina",
    email="amina@example.org",
    purpose="sign_in",
    return_to="/console",
    amr=None,
    code="c",
):
    start = browser.get(
        "/auth/start", params={"purpose": purpose, "method": "google", "return_to": return_to}
    )
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    claims = {
        "iss": ISSUER,
        "aud": "console",
        "exp": time.time() + 60,
        "nonce": query["nonce"][0],
        "sub": sub,
        "email": email,
        "email_verified": True,
    }
    if amr:
        claims["amr"] = amr
    fake.codes[code] = claims
    return browser.get("/auth/callback", params={"state": query["state"][0], "code": code}), query


def onboard(browser, fake, **kwargs):
    done, _ = sign_in(browser, fake, **kwargs)
    me = browser.get("/api/v1/me").json()
    headers = {"X-CSRF-Token": me["csrf"]}
    assert (
        browser.post("/api/v1/me/policies", json={"versions": policies.CURRENT}, headers=headers).status_code
        == 204
    )
    organisation = browser.post(
        "/api/v1/me/welcome",
        json={"display_name": "Amina Uwase", "organisation_name": "Amina Uwase"},
        headers=headers,
    )
    return organisation.json()["organisation"], headers, done


def test_sign_in_welcome_and_session(console):
    accounts, fake, app = console
    browser = client(app)
    done, query = sign_in(browser, fake)
    assert query["code_challenge_method"] == ["S256"] and query["connection"] == ["google-oauth2"]
    assert "prompt" not in query and done.headers["location"] == "/console/welcome"
    cookie = done.headers["set-cookie"]
    assert (
        "HttpOnly" in cookie
        and "Secure" in cookie
        and "samesite=lax" in cookie.lower()
        and "Path=/console" in cookie
    )
    me = browser.get("/api/v1/me")
    assert me.headers["cache-control"] == "no-store"
    assert set(me.json()["policies"]["pending"]) == set(policies.CURRENT)
    assert browser.post("/api/v1/me/policies", json={"versions": policies.CURRENT}).json()["error"][
        "code"
    ] == ("csrf_failed")
    organisation, headers, _ = onboard(client(app), fake)  # the same person again, from a new browser
    again, _ = sign_in(client(app), fake, code="d")
    assert again.headers["location"] == "/console"  # nothing left to welcome
    browser2 = client(app)
    sign_in(browser2, fake, code="e")
    assert [o["id"] for o in browser2.get("/api/v1/me").json()["organisations"]] == [organisation]


def test_callback_needs_the_same_browser_a_live_flow_and_a_supported_method(console):
    accounts, fake, app = console
    browser = client(app)
    start = browser.get("/auth/start", params={"method": "google"})
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    stranger = client(app)
    assert stranger.get("/auth/callback", params={"state": state, "code": "x"}).headers["location"] == (
        "/console/sign-in?error=expired"
    )
    done, _ = sign_in(client(app), fake, sub="github|someone")
    assert done.headers["location"] == "/console/sign-in?error=method_not_supported"
    fresh = client(app)
    start = fresh.get("/auth/start")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert fresh.get("/auth/callback", params={"state": state, "error": "access_denied"}).headers[
        "location"
    ] == ("/console/sign-in?error=cancelled")
    assert client(app).get("/auth/start", params={"purpose": "second_factor"}).headers["location"] == (
        "/console/sign-in?error=signed_out"
    )


def test_keys_need_access_a_second_factor_and_a_fresh_sign_in(console):
    accounts, fake, app = console
    browser = client(app)
    organisation, headers, _ = onboard(browser, fake)
    path = f"/api/v1/organisations/{organisation}/keys"
    assert browser.post(path, json={"name": "laptop"}, headers=headers).json()["error"]["code"] == (
        "entitlement_inactive"
    )
    request = browser.post(
        f"/api/v1/organisations/{organisation}/access-requests",
        headers=headers,
        json={
            "purpose": "Research",
            "expected_volume": "Hundreds a day",
            "contact_name": "Amina",
            "contact_email": "amina@example.org",
        },
    )
    assert request.status_code == 201
    accounts.grant_access(
        "op",
        organisation,
        tier="pilot",
        scope={"all": True},
        source="manual",
        ends_at=None,
        reason="Pilot",
        request_id=request.json()["id"],
    )
    refused = browser.post(path, json={"name": "laptop"}, headers=headers)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "second_factor_required"
    stepped, query = sign_in(
        browser, fake, purpose="second_factor", return_to="/console/keys", amr=["mfa"], code="m"
    )
    assert query["prompt"] == ["login"] and "multi-factor" in query["acr_values"][0]
    assert stepped.headers["location"] == "/console/keys"
    created = browser.post(path, json={"name": "laptop"}, headers=headers)
    assert created.status_code == 201 and created.json()["secret"].startswith("oa_live_")
    assert created.headers["cache-control"] == "no-store"
    listed = browser.get(path).json()["keys"]
    assert [k["id"] for k in listed] == [created.json()["id"]] and "secret" not in listed[0]
    with accounts.engine.begin() as conn:
        table = accounts.namespace.qualified(conn, "accounts", "console_session")
        conn.execute(text(f"UPDATE {table} SET authenticated_at = authenticated_at - interval '11 minutes'"))
    stale = browser.post(path, json={"name": "second"}, headers=headers)
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "reauthentication_required"
    # Revoking only removes access: no fresh sign-in needed.
    assert browser.post(f"{path}/{created.json()['id']}/revoke", headers=headers).status_code == 204


def test_a_fresh_sign_in_must_be_the_same_person(console):
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    done, _ = sign_in(
        browser, fake, purpose="reauthenticate", sub="google-oauth2|other", email="o@example.org", code="r"
    )
    assert done.headers["location"] == "/console/sign-in?error=wrong_account"


def test_organisations_are_private_to_members_and_writes_check_origin(console):
    accounts, fake, app = console
    amina = client(app)
    organisation, headers, _ = onboard(amina, fake)
    other = client(app)
    onboard(other, fake, sub="google-oauth2|other", email="other@example.org", code="o")
    assert other.get(f"/api/v1/organisations/{organisation}").json()["error"]["code"] == "not_found"
    assert other.get(f"/api/v1/organisations/{organisation}/keys").status_code == 404
    hostile = amina.patch(
        "/api/v1/me", json={"display_name": "X"}, headers=headers | {"Origin": "https://evil.example"}
    )
    assert hostile.json()["error"]["code"] == "forbidden_origin"
    assert amina.patch("/api/v1/me", json={"display_name": "Amina U"}, headers=headers).status_code == 204


def test_sign_out_ends_the_session_and_dev_login_stays_off(console, tmp_path):
    accounts, fake, app = console
    browser = client(app)
    _, headers, _ = onboard(browser, fake)
    assert browser.post("/auth/sign-out", headers=headers).status_code == 204
    assert browser.get("/api/v1/me").status_code == 401
    assert browser.get("/auth/dev", params={"email": "x@example.org"}).status_code == 404
    (tmp_path / "console.html").write_text("<!doctype html><title>Console</title>")
    page = TestClient(
        mounted(create_console_app(accounts, None, frontend=tmp_path)), base_url=BASE + "/console"
    )
    response = page.get("/keys")
    assert (
        response.status_code == 200
        and "Console" in response.text
        and response.headers["cache-control"] == ("no-cache")
    )
    assert page.get("/api/v1/nothing").json()["error"]["code"] == "not_found"
