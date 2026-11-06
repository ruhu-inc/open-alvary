"""MAC-08/MAC-10 console back end against PostgreSQL, with a stand-in identity provider."""

import base64
import json
import threading
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import keys, mail, policies, store
from accounts.store import TIERS, AccountError, Accounts
from api.console import ConsoleConfig, Provider, create_console_app
from api.store import Store, seed
from changes.watches import Watches
from changes.webhooks import Sealer, Sender, Webhooks
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
    ago=0,
):
    """``ago``: how long since the person really signed in at the provider; None leaves the claim out. A link
    starts as the console's page starts one, and comes back to Security."""
    if purpose == "link":
        start = start_link(browser)
        assert start.status_code == 200
        query = parse_qs(urlparse(start.json()["redirect"]).query)
    else:
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
    if ago is not None:
        claims["auth_time"] = time.time() - ago
    fake.codes[code] = claims
    return browser.get("/auth/callback", params={"state": query["state"][0], "code": code}), query


def start_link(browser, method="google", **headers):
    """Adding a sign-in method, as the Security page asks for it: a change, with the session's CSRF token."""
    csrf = browser.get("/api/v1/me").json()["csrf"]
    return browser.post(
        "/api/v1/me/identities/link", json={"method": method}, headers={"X-CSRF-Token": csrf, **headers}
    )


def flows(accounts):
    with accounts.engine.connect() as conn:
        return conn.scalar(text(f"SELECT count(*) FROM {accounts._t(conn, 'sign_in_flow')}"))


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
    # Failures after a real start are audited (MAC-13.7), without the email tried; a stray callback is not.
    with accounts.engine.connect() as conn:
        failures = conn.execute(
            text(
                f"SELECT summary, client_family FROM {accounts._t(conn, 'audit_event')}"
                " WHERE action = 'session.sign_in_failed' ORDER BY seq"
            )
        ).all()
    assert [f.summary["code"] for f in failures] == ["method_not_supported", "access_denied"]
    assert failures[0].summary == {"code": "method_not_supported", "purpose": "sign_in"}
    assert "someone" not in str([f.summary for f in failures])


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
    # Creating a key needs no fresh sign-in (the owner, 2026-10-21): the second factor of this session
    # is enough. A sensitive action, such as the organisation's export, still asks.
    later = browser.post(path, json={"name": "second"}, headers=headers)
    assert later.status_code == 201
    stale = browser.post(f"/api/v1/organisations/{organisation}/export", headers=headers)
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "reauthentication_required"
    # Revoking only removes access: no fresh sign-in needed.
    assert browser.post(f"{path}/{created.json()['id']}/revoke", headers=headers).status_code == 204


def test_rotating_a_key_in_the_console_keeps_the_old_one_for_the_overlap_without_a_fresh_sign_in(console):
    """MAC-05 and MAC-03: the console's rotate route answers a new key once, with the old one working for
    the overlap asked (24 hours by default, at most 24), and asks for no fresh sign-in (owner,
    2026-10-21); the role's second factor, once in the session, is enough."""
    accounts, fake, app = console
    browser = client(app)
    organisation, headers, _ = onboard(browser, fake)
    accounts.grant_access(
        "op", organisation, tier="pilot", scope={"all": True}, source="manual", ends_at=None, reason="Pilot"
    )
    sign_in(browser, fake, purpose="second_factor", return_to="/console/keys", amr=["mfa"], code="m")
    path = f"/api/v1/organisations/{organisation}/keys"
    old = browser.post(path, json={"name": "laptop"}, headers=headers).json()
    with accounts.engine.begin() as conn:
        table = accounts.namespace.qualified(conn, "accounts", "console_session")
        conn.execute(text(f"UPDATE {table} SET authenticated_at = authenticated_at - interval '11 minutes'"))

    def checked(secret):
        with accounts.engine.begin() as conn:
            fn = accounts.namespace.qualified(conn, "policy", "check_credential")
            request = {
                "kind": "key",
                "key_id": keys.parse(secret),
                "secret_hash": keys.secret_hash(secret, b"k" * 32).hex(),
            }
            return conn.scalar(text(f"SELECT {fn}(CAST(:r AS jsonb))"), {"r": json.dumps(request)})

    too_long = browser.post(f"{path}/{old['id']}/rotate", json={"overlap_hours": 25}, headers=headers)
    assert too_long.status_code == 400 and too_long.json()["error"]["code"] == "invalid_input"
    rotated = browser.post(f"{path}/{old['id']}/rotate", json={"overlap_hours": 2}, headers=headers)
    assert rotated.status_code == 201 and rotated.headers["cache-control"] == "no-store"
    new = rotated.json()
    assert new["secret"].startswith("oa_live_") and new["name"] == "laptop" and new["id"] != old["id"]
    assert (
        checked(old["secret"])["credential"] == old["id"]
        and checked(new["secret"])["credential"] == new["id"]
    )
    listed = {k["id"]: k for k in browser.get(path).json()["keys"]}
    assert listed[old["id"]]["state"] == listed[new["id"]]["state"] == "active"
    assert "secret" not in listed[new["id"]]
    # The replaced key is not offered for rotation again, and is refused if asked.
    assert listed[old["id"]]["replaced"] and not listed[new["id"]]["replaced"]
    twice = browser.post(f"{path}/{old['id']}/rotate", json={}, headers=headers)
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "already_rotated"
    # The default overlap is 24 hours. Rotating during the first overlap stops the oldest key now.
    again = browser.post(f"{path}/{new['id']}/rotate", json={}, headers=headers)
    assert again.status_code == 201
    assert checked(old["secret"])["code"] == "invalid_credentials"
    with accounts.engine.begin() as conn:
        table = accounts.namespace.qualified(conn, "accounts", "api_key")
        left = conn.scalar(
            text(f"SELECT expires_at - statement_timestamp() FROM {table} WHERE key_id = :k"),
            {"k": new["id"].removeprefix("key_")},
        )
    assert timedelta(hours=23, minutes=59) < left <= timedelta(hours=24)
    assert checked(new["secret"])["ok"] and checked(again.json()["secret"])["ok"]


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


def test_the_operator_hears_of_each_new_organisation_without_who_it_is(console):
    accounts, fake, _ = console
    mailer = RecordingMailer()
    browser = client(create_console_app(accounts, app_provider(fake), mailer=mailer))
    _, headers, _ = onboard(browser, fake)
    [welcomed] = mailer.sent
    assert welcomed.to == "hello@ruhu.ai" and welcomed.subject == "New Open Alvary registration"
    assert "Type: individual" in welcomed.text and "Country: not given" in welcomed.text
    assert "Amina" not in welcomed.text and "amina@example.org" not in welcomed.text
    # Welcoming again makes nothing, so it tells nobody.
    again = browser.post(
        "/api/v1/me/welcome",
        json={"display_name": "Amina Uwase", "organisation_name": "Amina Uwase"},
        headers=headers,
    )
    assert again.status_code == 200 and len(mailer.sent) == 1
    made = browser.post(
        "/api/v1/organisations",
        json={"name": "Kigali Law Clinic", "kind": "non_profit", "country": "rw"},
        headers=headers,
    )
    assert made.status_code == 201
    second = mailer.sent[1]
    assert "Type: non profit" in second.text and "Country: RW" in second.text
    assert "Kigali" not in second.text and "Kigali" not in second.subject


def test_registration_emails_stop_at_the_hours_allowance(console, monkeypatch):
    accounts, fake, _ = console
    monkeypatch.setattr("api.console.REGISTRATION_EMAILS_AN_HOUR", 1)
    mailer = RecordingMailer()
    browser = client(create_console_app(accounts, app_provider(fake), mailer=mailer))
    _, headers, _ = onboard(browser, fake)
    made = browser.post("/api/v1/organisations", json={"name": "Second"}, headers=headers)
    assert made.status_code == 201 and len(mailer.sent) == 1  # made all the same, and on the Access page


def test_an_allowance_starts_again_each_hour():
    now = [0.0]
    allowance = mail.Allowance(2, clock=lambda: now[0])
    assert [allowance.take() for _ in range(3)] == [True, True, False]
    now[0] = 3599.0
    assert allowance.take() is False
    now[0] = 3600.0
    assert allowance.take() is True


def test_invitations_and_access_requests_send_mail_after_the_change(console):
    accounts, fake, _ = console
    mailer = RecordingMailer()
    app = create_console_app(accounts, app_provider(fake), mailer=mailer)
    browser = client(app)
    organisation, headers, _ = onboard(browser, fake)
    mailer.sent.clear()
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
    accounts.set_tiers(
        [
            TIER,
            TIER | {"id": "developer", "name": "Developer"},
            TIER | {"id": "partner", "name": "Partner", "published": False},
        ]
    )
    mailer = RecordingMailer()
    browser = client(create_console_app(accounts, app_provider(fake), mailer=mailer))
    organisation, headers, _ = onboard(browser, fake)
    mailer.sent.clear()
    tiers = browser.get("/api/v1/tiers").json()["tiers"]
    assert "partner" not in {t["id"] for t in tiers}  # granted by agreement, never listed
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


def test_owners_see_the_published_countries_and_change_their_plans_countries(console):
    accounts, fake, _ = console
    limited = TIER | {
        "id": "developer",
        "name": "Developer",
        "allowance": TIER["allowance"] | {"jurisdictions": 2},
    }
    accounts.set_tiers([TIER, limited])
    published = [{"id": "rw", "name": "Rwanda", "sources": 3, "full_text": 3}]
    browser = client(create_console_app(accounts, app_provider(fake), countries=lambda: published))
    organisation, headers, _ = onboard(browser, fake)
    assert browser.get("/api/v1/countries").json() == {"countries": published}
    accounts.grant_access(
        "op",
        organisation,
        tier="developer",
        scope={"jurisdictions": ["rw"]},
        source="manual",
        ends_at=None,
        reason="Test",
    )
    path = f"/api/v1/organisations/{organisation}/access/countries"
    assert browser.post(path, headers=headers, json={"jurisdictions": ["rw", "ng"]}).status_code == 204
    details = browser.get(f"/api/v1/organisations/{organisation}/access", headers=headers).json()
    assert details["current"]["scope"] == {"jurisdictions": ["ng", "rw"]} and details["country_change_after"]
    again = browser.post(path, headers=headers, json={"jurisdictions": ["ke"]})
    assert again.status_code == 409 and again.json()["error"]["code"] == "change_too_soon"


# ---------- step-up: what a session records, and when ----------
def session_times(accounts):
    with accounts.engine.connect() as conn:
        return conn.execute(
            text(f"SELECT authenticated_at, second_factor_at FROM {accounts._t(conn, 'console_session')}")
        ).one()


def age_session(accounts, minutes):
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'console_session')} SET authenticated_at = authenticated_at"
                f" - interval '{minutes} minutes',"
                f" second_factor_at = second_factor_at - interval '{minutes} minutes'"
            )
        )


def test_linking_needs_a_fresh_sign_in_and_comes_back_after_it(console):
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    age_session(accounts, 11)
    refused = start_link(browser)
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "reauthentication_required"
    assert flows(accounts) == 0 and "oa_console_flow" not in browser.cookies
    # Any method already linked will do; the page comes back to Security and asks again.
    done, _ = sign_in(browser, fake, purpose="reauthenticate", return_to="/console/security", code="r")
    assert done.headers["location"] == "/console/security"
    started = start_link(browser)
    assert started.status_code == 200 and started.headers["cache-control"] == "no-store"
    target = started.json()["redirect"]
    assert target.startswith(ISSUER + "authorize")
    assert parse_qs(urlparse(target).query)["connection"] == ["google-oauth2"]
    assert flows(accounts) == 1 and browser.cookies.get("oa_console_flow", path="/console/auth")


def test_linking_needs_the_second_factor_when_any_organisation_of_the_person_does(console):
    accounts, fake, app = console
    browser = client(app)
    organisation, _, _ = onboard(browser, fake)
    accounts.grant_access(
        "op", organisation, tier="pilot", scope={"all": True}, source="manual", ends_at=None, reason="Pilot"
    )
    refused = start_link(browser)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "second_factor_required"
    sign_in(browser, fake, purpose="second_factor", amr=["mfa"], code="m")
    assert start_link(browser).json()["redirect"].startswith(ISSUER + "authorize")


def test_a_link_starts_only_from_the_console_itself(console):
    """Adding a sign-in method is a change like any other: a link, a form or a script on another site cannot
    start one, nor can a request without the session's CSRF token."""
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    linked = len(browser.get("/api/v1/me/identities").json()["identities"])
    for params in (
        {"purpose": "link", "method": "google", "return_to": "/console/security"},
        {"purpose": "link"},
    ):
        followed = browser.get("/auth/start", params=params)
        assert followed.headers["location"] == "/console/sign-in?error=invalid", params
    assert browser.post("/api/v1/me/identities/link", json={"method": "google"}).status_code == 403
    other_site = start_link(browser, origin="https://elsewhere.example")
    assert other_site.status_code == 403 and other_site.json()["error"]["code"] == "forbidden_origin"
    assert start_link(browser, method="github").json()["error"]["code"] == "invalid_input"
    assert flows(accounts) == 0 and "oa_console_flow" not in browser.cookies
    assert client(app).post("/api/v1/me/identities/link", json={"method": "google"}).status_code == 401
    assert len(browser.get("/api/v1/me/identities").json()["identities"]) == linked


def test_finishing_a_link_leaves_the_session_as_it_was_and_tells_the_person(console):
    accounts, fake, _ = console
    mailer = RecordingMailer()
    browser = client(create_console_app(accounts, app_provider(fake), mailer=mailer))
    onboard(browser, fake)
    age_session(accounts, 5)
    before = session_times(accounts)
    assert before.second_factor_at is None
    mailer.sent.clear()
    done, _ = sign_in(
        browser,
        fake,
        purpose="link",
        sub="google-oauth2|second",
        email="amina.second@example.org",
        return_to="/console/security",
        amr=["mfa"],
        code="l",
    )
    assert done.headers["location"] == "/console/security"
    assert session_times(accounts) == before
    assert len(browser.get("/api/v1/me/identities").json()["identities"]) == 2
    [notice] = mailer.sent
    assert notice.to == "amina@example.org" and notice.subject == "A sign-in method was added"
    # The window keeps running from the sign-in before the link.
    age_session(accounts, 6)
    headers = {"X-CSRF-Token": browser.get("/api/v1/me").json()["csrf"]}
    stale = browser.post("/api/v1/me/export", headers=headers)
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "reauthentication_required"


def test_linking_a_method_already_linked_records_nothing_and_sends_nothing(console):
    accounts, fake, _ = console
    mailer = RecordingMailer()
    browser = client(create_console_app(accounts, app_provider(fake), mailer=mailer))
    onboard(browser, fake)
    age_session(accounts, 5)
    before = session_times(accounts)
    mailer.sent.clear()
    sign_in(browser, fake, purpose="link", amr=["mfa"], code="l")
    assert session_times(accounts) == before and mailer.sent == []


def test_a_real_sign_in_with_a_linked_method_refreshes_the_session(console):
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    sign_in(browser, fake, purpose="link", sub="google-oauth2|second", email="a2@example.org", code="l")
    age_session(accounts, 11)
    headers = {"X-CSRF-Token": browser.get("/api/v1/me").json()["csrf"]}
    assert browser.post("/api/v1/me/export", headers=headers).status_code == 401
    before = session_times(accounts)
    done, query = sign_in(browser, fake, purpose="reauthenticate", code="r")
    assert query["prompt"] == ["login"] and query["max_age"] == ["0"] and "acr_values" not in query
    assert done.headers["location"] == "/console"
    after = session_times(accounts)
    assert after.authenticated_at > before.authenticated_at and after.second_factor_at is None
    assert browser.post("/api/v1/me/export", headers=headers).status_code == 200
    # A method linked in an earlier flow is a sign-in method like any other, with what it proved.
    age_session(accounts, 11)
    sign_in(
        browser,
        fake,
        purpose="reauthenticate",
        sub="google-oauth2|second",
        email="a2@example.org",
        amr=["mfa"],
        code="s",
    )
    assert session_times(accounts).second_factor_at is not None


def test_a_method_linked_after_a_step_up_began_cannot_finish_it(console):
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    started = browser.get("/auth/start", params={"purpose": "reauthenticate"})
    query = parse_qs(urlparse(started.headers["location"]).query)
    held = browser.cookies.get("oa_console_flow", path="/console/auth")
    sign_in(browser, fake, purpose="link", sub="google-oauth2|second", email="a2@example.org", code="l")
    age_session(accounts, 11)
    before = session_times(accounts)
    browser.cookies.set("oa_console_flow", held, path="/console/auth")
    fake.codes["late"] = {
        "iss": ISSUER,
        "aud": "console",
        "exp": time.time() + 60,
        "nonce": query["nonce"][0],
        "sub": "google-oauth2|second",
        "email": "a2@example.org",
        "email_verified": True,
        "auth_time": time.time(),
    }
    done = browser.get("/auth/callback", params={"state": query["state"][0], "code": "late"})
    assert done.headers["location"] == "/console/sign-in?error=wrong_account"
    assert session_times(accounts) == before


@pytest.mark.parametrize("ago", [3600, None], ids=["provider-session-kept", "no-sign-in-time"])
@pytest.mark.parametrize("purpose", ["reauthenticate", "second_factor"])
def test_a_step_up_needs_a_sign_in_made_since_it_began(console, purpose, ago):
    """The redirect to the provider passes through the browser, so what it asked for is checked on the way
    back: the sign-in the provider reports must be newer than the flow."""
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    age_session(accounts, 11)
    before = session_times(accounts)
    done, _ = sign_in(browser, fake, purpose=purpose, amr=["mfa"], ago=ago, code="s")
    assert done.headers["location"] == "/console/sign-in?error=sign_in_refused"
    assert session_times(accounts) == before


def test_parameters_a_browser_adds_to_the_start_change_nothing_asked_of_the_provider(console):
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    added = {"acr_values": "anything", "prompt": "none", "max_age": "999999", "scope": "openid admin"}
    started = browser.get("/auth/start", params={"purpose": "reauthenticate", "method": "google"} | added)
    csrf = {"X-CSRF-Token": browser.get("/api/v1/me").json()["csrf"]}
    linking = browser.post("/api/v1/me/identities/link", json={"method": "google"} | added, headers=csrf)
    for address in (started.headers["location"], linking.json()["redirect"]):
        query = parse_qs(urlparse(address).query)
        assert "acr_values" not in query and query["prompt"] == ["login"] and query["max_age"] == ["0"]
        assert query["scope"] == ["openid email profile"]


def test_a_sign_in_counts_as_fresh_only_from_when_the_person_really_signed_in(console):
    accounts, fake, app = console
    onboard(client(app), fake)
    with accounts.engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {accounts._t(conn, 'console_session')}"))
    # A sign-in the provider answers from its own earlier session, whatever that session proved.
    browser = client(app)
    done, query = sign_in(browser, fake, amr=["mfa"], ago=3600, code="k")
    assert done.headers["location"] == "/console" and "max_age" in query and "prompt" not in query
    assert session_times(accounts).second_factor_at is None
    headers = {"X-CSRF-Token": browser.get("/api/v1/me").json()["csrf"]}
    assert browser.post("/api/v1/me/export", headers=headers).status_code == 401
    assert start_link(browser).json()["error"]["code"] == "reauthentication_required"
    # No time reported: the session works, and sensitive changes ask for a sign-in first.
    browser = client(app)
    sign_in(browser, fake, ago=None, code="n")
    me = browser.get("/api/v1/me")
    assert me.status_code == 200
    headers = {"X-CSRF-Token": me.json()["csrf"]}
    assert browser.post("/api/v1/me/export", headers=headers).status_code == 401
    # A sign-in just made counts, with its second factor.
    browser = client(app)
    sign_in(browser, fake, amr=["mfa"], code="f")
    me = browser.get("/api/v1/me").json()
    assert me["second_factor_until"] is not None
    headers = {"X-CSRF-Token": me["csrf"]}
    assert browser.post("/api/v1/me/export", headers=headers).status_code == 200


def stepped_world(accounts, fake, app):
    """An owner of a paid organisation, stepped up, with a second sign-in method, a second member and a
    webhook endpoint, ready for each sensitive change."""
    accounts.set_tiers(json.loads(TIERS.read_text()))
    browser = client(app)
    organisation, headers, _ = onboard(browser, fake)
    accounts.grant_access(
        "op",
        organisation,
        tier="developer",
        scope={"jurisdictions": ["ng"]},
        source="manual",
        ends_at=None,
        reason="Test",
    )
    sign_in(browser, fake, purpose="second_factor", amr=["mfa"], code="m")
    sign_in(browser, fake, purpose="link", sub="google-oauth2|second", email="a2@example.org", code="l")
    identities = browser.get("/api/v1/me/identities").json()["identities"]
    identity = next(i["id"] for i in identities if i["label"] == "a2@example.org")
    invited = browser.post(
        f"/api/v1/organisations/{organisation}/invitations",
        headers=headers,
        json={"email": "d@example.org", "role": "developer"},
    )
    other = client(app)
    sign_in(other, fake, sub="google-oauth2|dev", email="d@example.org", code="d")
    other.post(
        f"/api/v1/invitations/{invited.json()['id']}/accept",
        headers={"X-CSRF-Token": other.get("/api/v1/me").json()["csrf"]},
    )
    with accounts.engine.begin() as conn:  # the other person's session is not the one under test
        conn.execute(
            text(
                f"DELETE FROM {accounts._t(conn, 'console_session')} WHERE person_id ="
                f" (SELECT person_id FROM {accounts._t(conn, 'identity')} WHERE subject = 'google-oauth2|dev')"
            )
        )
    listed = browser.get(f"/api/v1/organisations/{organisation}/members").json()["members"]
    member = next(m["id"] for m in listed if m["email"] == "d@example.org")
    person = browser.get("/api/v1/me").json()["person"]["id"]
    seed(Store(accounts.engine))
    watch = Watches(accounts).create(person, organisation, {"name": "N", "filters": {"jurisdiction": ["ng"]}})
    hooks = Webhooks(
        accounts,
        Sender(resolver=lambda host: ["93.184.216.34"], transport=lambda *a: (200, {}, b"{}")),
        Sealer({"k": b"k" * 32}, "k"),
    )
    endpoint = hooks.create(
        person, organisation, {"url": "https://hooks.example.org/in", "watch_ids": [watch["id"]]}
    )
    return browser, organisation, headers, identity, member, endpoint["endpoint"]["id"]


def sensitive(browser, organisation, headers, identity, member, endpoint):
    org = f"/api/v1/organisations/{organisation}"
    return {
        "add a sign-in method": lambda: browser.post(
            "/api/v1/me/identities/link", headers=headers, json={"method": "email"}
        ),
        "unlink": lambda: browser.delete(f"/api/v1/me/identities/{identity}", headers=headers),
        "sign out everywhere": lambda: browser.post("/api/v1/me/sessions/revoke-all", headers=headers),
        "close account": lambda: browser.post(
            "/api/v1/me/delete", headers=headers, json={"confirm": "amina@example.org"}
        ),
        "delete organisation": lambda: browser.delete(org, headers=headers),
        "transfer ownership": lambda: browser.post(
            f"{org}/transfer-ownership", headers=headers, json={"member_id": member, "keep_owner": True}
        ),
        "invite an owner": lambda: browser.post(
            f"{org}/invitations", headers=headers, json={"email": "o@example.org", "role": "owner"}
        ),
        "invite an admin": lambda: browser.post(
            f"{org}/invitations", headers=headers, json={"email": "a@example.org", "role": "admin"}
        ),
        "repoint a webhook": lambda: browser.patch(
            f"{org}/webhooks/{endpoint}", headers=headers, json={"url": "https://other.example.org/in"}
        ),
    }


SENSITIVE = (
    "add a sign-in method",
    "unlink",
    "sign out everywhere",
    "close account",
    "delete organisation",
    "transfer ownership",
    "invite an owner",
    "invite an admin",
    "repoint a webhook",
)


@pytest.mark.parametrize("action", SENSITIVE)
def test_sensitive_changes_need_a_fresh_sign_in(console, action):
    accounts, fake, app = console
    browser, *rest = stepped_world(accounts, fake, app)
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'console_session')} SET second_factor_at = statement_timestamp(),"
                " authenticated_at = statement_timestamp() - interval '11 minutes'"
            )
        )
    answer = sensitive(browser, *rest)[action]()
    assert answer.status_code == 401 and answer.json()["error"]["code"] == "reauthentication_required"


@pytest.mark.parametrize("action", SENSITIVE)
def test_sensitive_changes_need_the_second_factor_where_the_role_needs_one(console, action):
    accounts, fake, app = console
    browser, *rest = stepped_world(accounts, fake, app)
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'console_session')} SET authenticated_at = statement_timestamp(),"
                " second_factor_at = statement_timestamp() - interval '11 minutes'"
            )
        )
    answer = sensitive(browser, *rest)[action]()
    assert answer.status_code == 403 and answer.json()["error"]["code"] == "second_factor_required"


def test_changes_that_are_not_sensitive_stay_free_of_a_step_up(console, monkeypatch):
    accounts, fake, app = console
    browser, organisation, headers, _, _, endpoint = stepped_world(accounts, fake, app)
    org = f"/api/v1/organisations/{organisation}"
    age_session(accounts, 11)
    invited = browser.post(
        f"{org}/invitations", headers=headers, json={"email": "v@example.org", "role": "viewer"}
    )
    assert invited.status_code == 201
    same_url = browser.patch(
        f"{org}/webhooks/{endpoint}",
        headers=headers,
        json={"url": "https://hooks.example.org/in", "description": "Ours"},
    )
    assert same_url.status_code == 200 and same_url.json()["description"] == "Ours"
    # With a fresh sign-in and second factor, a webhook can be repointed.
    sign_in(browser, fake, purpose="second_factor", amr=["mfa"], code="again")
    monkeypatch.setattr(Webhooks, "verify", lambda self, p, o, e: {"endpoint": {"id": e}})
    moved = browser.patch(
        f"{org}/webhooks/{endpoint}", headers=headers, json={"url": "https://other.example.org/in"}
    )
    assert moved.status_code == 200


def test_a_session_that_is_not_fresh_cannot_sign_the_person_out_everywhere(console):
    """A cookie taken from one browser cannot end the person's other sessions, nor its own: it gets the
    refusal any sensitive change gets, and the person, signing in again, can."""
    accounts, fake, app = console
    browser = client(app)
    _, headers, _ = onboard(browser, fake)
    elsewhere = client(app)
    sign_in(elsewhere, fake, code="e")
    age_session(accounts, 11)
    refused = browser.post("/api/v1/me/sessions/revoke-all", headers=headers)
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "reauthentication_required"
    assert browser.get("/api/v1/me").status_code == 200 and elsewhere.get("/api/v1/me").status_code == 200
    assert len(browser.get("/api/v1/me/sessions").json()["sessions"]) == 2
    sign_in(elsewhere, fake, purpose="reauthenticate", code="r")
    done = elsewhere.post(
        "/api/v1/me/sessions/revoke-all", headers={"X-CSRF-Token": elsewhere.get("/api/v1/me").json()["csrf"]}
    )
    assert done.status_code == 204
    assert browser.get("/api/v1/me").status_code == 401 and elsewhere.get("/api/v1/me").status_code == 401


def test_signing_out_everywhere_disconnects_the_persons_apps_and_leaves_their_keys(console):
    """Every app the person connected stops at its next request, as if disconnected one by one; keys are
    not sessions and stay."""
    accounts, fake, app = console
    consent_app = create_console_app(accounts, app_provider(fake), consent_secret=CONSENT_SECRET)
    browser = client(consent_app)
    organisation, headers, _ = onboard(browser, fake)
    accounts.grant_access(
        "op", organisation, tier="pilot", scope={"all": True}, source="manual", ends_at=None, reason="Pilot"
    )
    answer = client(consent_app).post(
        "/oauth/api/decision",
        json={"session_token": hand_off(), "state": "s1", "decision": "allow", "organisation": organisation},
    )
    from api.console import verify_hs256

    grant = verify_hs256(
        parse_qs(urlparse(answer.json()["redirect"]).query)["consent_token"][0], CONSENT_SECRET
    )
    credential = {
        "kind": "grant",
        "grant_id": grant["grant"],
        "resource": "mcp",
        "issuer": ISSUER,
        "subject": "google-oauth2|amina",
    }
    assert accounts.check_credential(credential)["ok"]
    sign_in(browser, fake, purpose="second_factor", amr=["mfa"], code="m")
    key = browser.post(f"/api/v1/organisations/{organisation}/keys", json={"name": "CI"}, headers=headers)
    assert key.status_code == 201
    done = browser.post("/api/v1/me/sessions/revoke-all", headers=headers)
    assert done.status_code == 204
    assert not accounts.check_credential(credential)["ok"]
    with accounts.engine.connect() as conn:
        grants = conn.execute(
            text(f"SELECT revoked_at IS NOT NULL, revoked_reason FROM {accounts._t(conn, 'client_grant')}")
        ).all()
        live_keys = conn.scalar(
            text(f"SELECT count(*) FROM {accounts._t(conn, 'api_key')} WHERE revoked_at IS NULL")
        )
    assert grants == [(True, "person")] and live_keys == 1


# ---------- what the console sends to developers' endpoints ----------
def test_a_test_beyond_its_hourly_limit_is_refused_with_when_to_try_again(console):
    from accounts.ids import new_id
    from changes.webhooks import TESTS_PER_ENDPOINT

    accounts, fake, app = console
    browser, organisation, headers, _, _, endpoint = stepped_world(accounts, fake, app)
    with accounts.engine.begin() as conn:
        actor = conn.scalar(
            text(f"SELECT id FROM {accounts._t(conn, 'actor')} WHERE person_id IS NOT NULL LIMIT 1")
        )
        for _ in range(TESTS_PER_ENDPOINT):
            conn.execute(
                text(
                    f"INSERT INTO {accounts._t(conn, 'audit_event')} (id, actor_type, actor_ref, organisation_id,"
                    " action, target_type, target_id) VALUES (:id, 'person', :a, :o, 'webhook.tested',"
                    " 'webhook_endpoint', :e)"
                ),
                {"id": new_id("evt"), "a": actor, "o": organisation, "e": endpoint},
            )
    refused = browser.post(f"/api/v1/organisations/{organisation}/webhooks/{endpoint}/test", headers=headers)
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "rate_limited"
    assert 3500 < int(refused.headers["retry-after"]) <= 3600
    assert refused.json()["error"]["retry_after_seconds"] == int(refused.headers["retry-after"])


def test_a_test_or_verification_is_sent_from_threads_the_website_does_not_use(console, monkeypatch):
    """A slow or silent endpoint holds only a thread kept for sending to endpoints, never one of the threads
    the website's routes run on."""
    from anyio import from_thread, to_thread

    accounts, fake, app = console
    browser, organisation, headers, _, _, endpoint = stepped_world(accounts, fake, app)

    def taken(*args):
        return {
            "site_threads_taken": from_thread.run_sync(
                lambda: to_thread.current_default_thread_limiter().borrowed_tokens
            )
        }

    monkeypatch.setattr(Webhooks, "test", taken)
    monkeypatch.setattr(Webhooks, "verify", taken)
    for action in ("test", "verify"):
        answer = browser.post(
            f"/api/v1/organisations/{organisation}/webhooks/{endpoint}/{action}", headers=headers
        )
        assert answer.json() == {"site_threads_taken": 0}, action


# ---------- mail a request can cause ----------
ASK = {"purpose": "Research", "expected_volume": "Some", "contact_name": "A"}


def mailing(console):
    accounts, fake, _ = console
    mailer = RecordingMailer()
    app = create_console_app(accounts, app_provider(fake), mailer=mailer)
    browser = client(app)
    organisation, headers, _ = onboard(browser, fake)
    mailer.sent.clear()
    return accounts, fake, app, mailer, browser, organisation, headers


def invite_and_revoke(browser, organisation, headers, email):
    path = f"/api/v1/organisations/{organisation}/invitations"
    made = browser.post(path, headers=headers, json={"email": email, "role": "developer"})
    if made.status_code == 201:
        assert browser.delete(f"{path}/{made.json()['id']}", headers=headers).status_code == 204
    return made


def request_and_withdraw(browser, organisation, headers, email):
    path = f"/api/v1/organisations/{organisation}/access-requests"
    made = browser.post(path, headers=headers, json=ASK | {"contact_email": email})
    if made.status_code == 201:
        assert browser.post(f"{path}/{made.json()['id']}/withdraw", headers=headers).status_code == 204
    return made


def assert_limited(answer):
    assert answer.status_code == 429
    error = answer.json()["error"]
    assert error["code"] == "rate_limited" and error["message"] and error["request_id"]
    wait = int(answer.headers["retry-after"])
    assert error["retry_after_seconds"] == wait and 0 < wait <= 86400
    assert answer.headers["cache-control"] == "no-store"
    return wait


def test_one_address_gets_a_limited_number_of_invitations_however_it_is_written_or_revoked(console):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    for _ in range(store.MAILS_PER_ADDRESS):
        assert invite_and_revoke(browser, organisation, headers, "guest@gmail.com").status_code == 201
    sent = len(mailer.sent)
    assert sent == store.MAILS_PER_ADDRESS
    for same in ("guest@gmail.com", "GUEST@gmail.com", "guest+x@gmail.com", "g.u.e.s.t@googlemail.com"):
        assert_limited(invite_and_revoke(browser, organisation, headers, same))
    assert len(mailer.sent) == sent
    # Another organisation shares the same allowance for that address.
    second = browser.post("/api/v1/organisations", headers=headers, json={"name": "Second"}).json()["id"]
    assert_limited(invite_and_revoke(browser, second, headers, "guest@gmail.com"))
    with accounts.engine.connect() as conn:
        rows = conn.scalar(text(f"SELECT count(*) FROM {accounts._t(conn, 'invitation')}"))
    assert rows == store.MAILS_PER_ADDRESS
    assert invite_and_revoke(browser, organisation, headers, "someone.else@gmail.com").status_code == 201


def test_invitations_and_requests_to_one_address_count_together(console):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    for _ in range(store.REQUESTS_PER_ORGANISATION):
        assert request_and_withdraw(browser, organisation, headers, "c@example.org").status_code == 201
    while invite_and_revoke(browser, organisation, headers, "c@example.org").status_code == 201:
        pass
    with accounts.engine.connect() as conn:
        invited = conn.scalar(text(f"SELECT count(*) FROM {accounts._t(conn, 'invitation')}"))
    assert invited + store.REQUESTS_PER_ORGANISATION == store.MAILS_PER_ADDRESS


def test_the_limits_roll_with_the_day_and_retry_after_says_when_room_returns(console, monkeypatch):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    monkeypatch.setattr(store, "INVITATIONS_PER_ORGANISATION", 2)
    with accounts.engine.begin() as conn:
        conn.execute(
            text(f"UPDATE {accounts._t(conn, 'organisation')} SET invitation_limit = 1 WHERE id = :o"),
            {"o": organisation},
        )
    assert invite_and_revoke(browser, organisation, headers, "a@example.org").status_code == 201
    assert invite_and_revoke(browser, organisation, headers, "b@example.org").status_code == 201
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'invitation')} SET created_at = created_at - interval '23 hours'"
                " WHERE email = 'a@example.org'"
            )
        )
    wait = assert_limited(invite_and_revoke(browser, organisation, headers, "c@example.org"))
    assert 3500 < wait <= 3600
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'invitation')} SET created_at = created_at - interval '2 hours'"
                " WHERE email = 'a@example.org'"
            )
        )
    assert invite_and_revoke(browser, organisation, headers, "c@example.org").status_code == 201


def test_withdrawing_a_request_does_not_give_its_mail_back(console):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    for number in range(store.REQUESTS_PER_ORGANISATION):
        made = request_and_withdraw(browser, organisation, headers, f"contact{number}@example.org")
        assert made.status_code == 201
    sent = len(mailer.sent)
    assert sent == 2 * store.REQUESTS_PER_ORGANISATION  # the contact and the operator
    assert_limited(request_and_withdraw(browser, organisation, headers, "another@example.org"))
    assert len(mailer.sent) == sent
    with accounts.engine.connect() as conn:
        requests = conn.scalar(text(f"SELECT count(*) FROM {accounts._t(conn, 'access_request')}"))
    assert requests == store.REQUESTS_PER_ORGANISATION


def test_a_new_organisation_does_not_start_a_persons_mail_again(console):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    for number in range(store.MAILS_PER_CREATOR):
        made = browser.post("/api/v1/organisations", headers=headers, json={"name": f"Org {number}"}).json()
        assert invite_and_revoke(browser, made["id"], headers, f"a{number}@example.org").status_code == 201
        assert browser.delete(f"/api/v1/organisations/{made['id']}", headers=headers).status_code == 204
    made = browser.post("/api/v1/organisations", headers=headers, json={"name": "Last"}).json()
    assert_limited(invite_and_revoke(browser, made["id"], headers, "last@example.org"))


def test_an_organisation_the_operator_gave_more_invitations_keeps_them(console, monkeypatch):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    monkeypatch.setattr(store, "INVITATIONS_PER_ORGANISATION", 1)
    monkeypatch.setattr(store, "MAILS_PER_CREATOR", 1)
    with accounts.engine.begin() as conn:
        conn.execute(
            text(f"UPDATE {accounts._t(conn, 'organisation')} SET invitation_limit = 2 WHERE id = :o"),
            {"o": organisation},
        )
    assert invite_and_revoke(browser, organisation, headers, "a@example.org").status_code == 201
    assert invite_and_revoke(browser, organisation, headers, "b@example.org").status_code == 201
    assert_limited(invite_and_revoke(browser, organisation, headers, "c@example.org"))


def test_the_limits_hold_when_requests_arrive_together(console, monkeypatch):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    monkeypatch.setattr(store, "MAILS_PER_ADDRESS", 1)
    person = browser.get("/api/v1/me").json()["person"]["id"]
    organisations = [
        browser.post("/api/v1/organisations", headers=headers, json={"name": f"Org {n}"}).json()["id"]
        for n in range(4)
    ]
    outcomes = []

    def attempt(organisation_id):
        try:
            accounts.invite(person, organisation_id, "same@example.org", "developer")
            outcomes.append("sent")
        except AccountError as refused:
            outcomes.append(refused.code)

    threads = [threading.Thread(target=attempt, args=(o,)) for o in organisations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["rate_limited"] * 3 + ["sent"]


def test_an_organisation_name_that_looks_like_a_link_stays_out_of_mail_to_others(console):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    revision = browser.get(f"/api/v1/organisations/{organisation}").json()["revision"]
    renamed = browser.patch(
        f"/api/v1/organisations/{organisation}",
        headers=headers,
        json={"revision": revision, "name": "Claim your refund at refund-now.example today"},
    )
    assert renamed.status_code == 204
    named = browser.patch("/api/v1/me", headers=headers, json={"display_name": "See www.x.example"})
    assert named.status_code == 204
    invited = browser.post(
        f"/api/v1/organisations/{organisation}/invitations",
        headers=headers,
        json={"email": "guest@example.org", "role": "developer"},
    )
    assert invited.status_code == 201
    asked = browser.post(
        f"/api/v1/organisations/{organisation}/access-requests",
        headers=headers,
        json=ASK | {"contact_email": "contact@example.org"},
    )
    assert asked.status_code == 201
    invitation, received, operator = mailer.sent
    for message in (invitation, received, operator):
        assert "refund-now" not in message.subject + message.text and "www.x" not in message.text
    assert "Someone invited you to join an organisation on Open Alvary" in invitation.text
    assert "the request for your organisation" in received.text
    assert browser.get(f"/api/v1/organisations/{organisation}").json()["name"].startswith("Claim")


@pytest.mark.parametrize(
    "address",
    [
        "a@b@example.org",
        "two words@example.org",
        "a@example.org, b@example.org",
        "x\ny@example.org",
        "<a>@example.org",
    ],
)
def test_an_invitation_or_request_goes_to_one_plain_address(console, address):
    accounts, fake, _, mailer, browser, organisation, headers = mailing(console)
    invited = browser.post(
        f"/api/v1/organisations/{organisation}/invitations",
        headers=headers,
        json={"email": address, "role": "developer"},
    )
    asked = browser.post(
        f"/api/v1/organisations/{organisation}/access-requests",
        headers=headers,
        json=ASK | {"contact_email": address},
    )
    assert invited.status_code == 400 and asked.status_code == 400 and mailer.sent == []
