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
    "includes": ["rest", "mcp"],
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
    app = create_console_app(accounts, app_provider(fake))
    return accounts, fake, app


def app_provider(fake):
    config = ConsoleConfig(issuer=ISSUER, client_id="console", client_secret="secret", base_url=BASE)
    return Provider(config, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))


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


CONSENT_SECRET = b"c" * 32


def hand_off(sub="google-oauth2|amina", **extra):
    from api.console import sign_hs256

    payload = {
        "sub": sub,
        "email": "amina@example.org",
        "exp": time.time() + 600,
        "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
        "client_name": "claude.ai",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "resource": "https://api.open.alvary.ai/mcp",
        **extra,
    }
    return sign_hs256(payload, CONSENT_SECRET)


def test_approving_an_app_creates_a_grant_the_credential_check_accepts(console):
    accounts, fake, app = console
    consent_app = create_console_app(accounts, app_provider(fake), consent_secret=CONSENT_SECRET)
    browser = client(consent_app)
    organisation, headers, _ = onboard(browser, fake)
    token = hand_off()
    session = browser.get("/oauth/api/session", params={"session_token": token}).json()
    assert (
        session["client"]["label"] == "verified_publisher" and session["client"]["publisher"] == "Anthropic"
    )
    assert session["client"]["name"] == "Claude" and session["client"]["resource"] == "mcp"
    assert session["organisations"] == [
        {"id": organisation, "name": "Amina Uwase", "eligible": False, "reason": "No access yet"}
    ]
    refused = browser.post(
        "/oauth/api/decision",
        json={"session_token": token, "state": "s1", "decision": "allow", "organisation": organisation},
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "organisation_not_eligible"
    accounts.grant_access(
        "op", organisation, tier="pilot", scope={"all": True}, source="manual", ends_at=None, reason="Pilot"
    )
    stranger = client(consent_app)  # the hand-off proves the person; no console session is needed
    answer = stranger.post(
        "/oauth/api/decision",
        json={"session_token": token, "state": "s1", "decision": "allow", "organisation": organisation},
    )
    redirect = urlparse(answer.json()["redirect"])
    assert f"{redirect.scheme}://{redirect.netloc}/" == ISSUER and redirect.path == "/continue"
    from api.console import verify_hs256

    query = parse_qs(redirect.query)
    signed = verify_hs256(query["consent_token"][0], CONSENT_SECRET)
    assert query["state"] == ["s1"] and signed["decision"] == "allow" and signed["org"] == organisation
    [grant] = accounts.list_grants(accounts.person_for_identity(ISSUER, "google-oauth2|amina"), organisation)
    assert grant["id"] == signed["grant"] and grant["client_name"] == "Claude" and grant["resource"] == "mcp"
    assert accounts.check_credential(
        {
            "kind": "grant",
            "grant_id": signed["grant"],
            "resource": "mcp",
            "issuer": ISSUER,
            "subject": "google-oauth2|amina",
        }
    )["ok"]


def test_denying_and_bad_hand_offs(console):
    accounts, fake, app = console
    consent_app = create_console_app(accounts, app_provider(fake), consent_secret=CONSENT_SECRET)
    browser = client(consent_app)
    onboard(browser, fake)
    from api.console import verify_hs256

    denied = browser.post(
        "/oauth/api/decision", json={"session_token": hand_off(), "state": "s2", "decision": "deny"}
    )
    token = parse_qs(urlparse(denied.json()["redirect"]).query)["consent_token"][0]
    assert verify_hs256(token, CONSENT_SECRET)["decision"] == "deny"
    expired = browser.get("/oauth/api/session", params={"session_token": hand_off(exp=time.time() - 1)})
    assert expired.status_code == 400 and expired.json()["error"]["code"] == "consent_expired"
    from api.console import sign_hs256

    forged = sign_hs256({"sub": "google-oauth2|amina", "exp": time.time() + 60}, b"x" * 32)
    assert browser.get("/oauth/api/session", params={"session_token": forged}).status_code == 400
    unknown = browser.get(
        "/oauth/api/session",
        params={
            "session_token": hand_off(
                client_id="https://tools.example/oauth.json", redirect_uri="http://127.0.0.1:5000/callback"
            )
        },
    ).json()
    assert unknown["client"]["label"] == "from_host" and unknown["client"]["host"] == "tools.example"
    assert unknown["client"]["loopback"] is True
    nobody = browser.get(
        "/oauth/api/session", params={"session_token": hand_off(sub="google-oauth2|new")}
    ).json()
    assert nobody["account"] is False and nobody["organisations"] == []


class RecordingMailer:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


def test_invitations_and_access_requests_send_mail_after_the_change(console):
    accounts, fake, _ = console
    mailer = RecordingMailer()
    app = create_console_app(accounts, app_provider(fake), mailer=mailer)
    browser = client(app)
    organisation, headers, _ = onboard(browser, fake)
    invited = browser.post(
        f"/api/v1/organisations/{organisation}/invitations",
        headers=headers,
        json={"email": "guest@example.org", "role": "developer"},
    )
    assert invited.status_code == 201
    [invitation] = mailer.sent
    assert (
        invitation.to == "guest@example.org"
        and "Amina Uwase invited you to join Amina Uwase" in invitation.text
    )
    requested = browser.post(
        f"/api/v1/organisations/{organisation}/access-requests",
        headers=headers,
        json={
            "purpose": "Research",
            "expected_volume": "Some",
            "contact_name": "Amina",
            "contact_email": "contact@example.org",
            "reduced": True,
        },
    )
    assert requested.status_code == 201
    received, operator = mailer.sent[1:]
    assert received.to == "contact@example.org" and received.subject == "We received your Open Alvary request"
    assert operator.to == "hello@ruhu.ai" and "free or reduced" in operator.text
    assert accounts.notice_recipients(organisation)["to"] == [
        "amina@example.org"
    ]  # owners, without a request


def test_a_plan_request_names_the_plan_in_the_mail_and_the_tiers_list_carries_listings(console):
    accounts, fake, _ = console
    accounts.set_tiers([TIER, TIER | {"id": "developer", "name": "Developer"}])
    mailer = RecordingMailer()
    browser = client(create_console_app(accounts, app_provider(fake), mailer=mailer))
    organisation, headers, _ = onboard(browser, fake)
    tiers = browser.get("/api/v1/tiers").json()["tiers"]
    developer = next(t for t in tiers if t["id"] == "developer")
    assert developer["listing"]["action"] == "upgrade" and developer["listing"]["for"]
    ask = {
        "purpose": "Research",
        "expected_volume": "Some",
        "contact_name": "A",
        "contact_email": "a@example.org",
    }
    wrong = browser.post(
        f"/api/v1/organisations/{organisation}/access-requests",
        headers=headers,
        json=ask | {"tier": "starter"},
    )
    assert wrong.status_code == 400
    asked = browser.post(
        f"/api/v1/organisations/{organisation}/access-requests",
        headers=headers,
        json=ask | {"tier": "developer"},
    )
    assert asked.status_code == 201
    received, operator = mailer.sent
    assert "move to the Developer plan" in received.text and "the Developer plan" in operator.text
    details = browser.get(f"/api/v1/organisations/{organisation}/access").json()
    assert details["requests"][0]["tier_requested"] == "developer"


def test_starter_organisations_create_keys_without_a_second_factor(console):
    accounts, fake, app = console
    accounts.set_tiers([TIER, {**TIER, "id": "starter", "name": "Starter"}])
    browser = client(app)
    organisation, headers, _ = onboard(browser, fake)
    created = browser.post(
        f"/api/v1/organisations/{organisation}/keys", json={"name": "first"}, headers=headers
    )
    assert created.status_code == 201  # right after sign-up: no request, no second factor
    accounts.grant_access(
        "op",
        organisation,
        tier="pilot",
        scope={"all": True},
        source="manual",
        ends_at=None,
        reason="Approved",
    )
    refused = browser.post(
        f"/api/v1/organisations/{organisation}/keys", json={"name": "second"}, headers=headers
    )
    assert refused.json()["error"]["code"] == "second_factor_required"  # approved access: owners need one


def test_the_approval_answer_asks_for_a_second_factor_only_where_the_role_needs_one(console):
    accounts, fake, _ = console
    accounts.set_tiers([TIER, {**TIER, "id": "starter", "name": "Starter"}])
    consent_app = create_console_app(accounts, app_provider(fake), consent_secret=CONSENT_SECRET)
    browser = client(consent_app)
    organisation, _, _ = onboard(browser, fake)
    from api.console import verify_hs256

    def answer(state):
        reply = browser.post(
            "/oauth/api/decision",
            json={
                "session_token": hand_off(),
                "state": state,
                "decision": "allow",
                "organisation": organisation,
            },
        )
        return verify_hs256(
            parse_qs(urlparse(reply.json()["redirect"]).query)["consent_token"][0], CONSENT_SECRET
        )

    assert answer("s1")["mfa"] is False  # starter
    accounts.grant_access(
        "op",
        organisation,
        tier="pilot",
        scope={"all": True},
        source="manual",
        ends_at=None,
        reason="Approved",
    )
    assert answer("s2")["mfa"] is True  # owner of an organisation with approved access


def test_continue_as_passes_the_remembered_account_to_the_provider(console):
    _, _, app = console
    start = client(app).get("/auth/start", params={"method": "google", "login_hint": "amina@example.org"})
    assert parse_qs(urlparse(start.headers["location"]).query)["login_hint"] == ["amina@example.org"]
    odd = client(app).get("/auth/start", params={"method": "google", "login_hint": "<script>"})
    assert "login_hint" not in parse_qs(urlparse(odd.headers["location"]).query)
