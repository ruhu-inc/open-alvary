"""MAC-08/MAC-10 console back end against PostgreSQL, with a stand-in identity provider."""

import base64
import json
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

from accounts import keys, mail, policies
from accounts.store import TIERS, Accounts
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


LINK = {"purpose": "link", "method": "google", "return_to": "/console/security"}
LINK_START = "/console/auth/start?purpose=link&method=google&return_to=%2Fconsole%2Fsecurity"


def test_linking_needs_a_fresh_sign_in_and_comes_back_after_it(console):
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    age_session(accounts, 11)
    refused = browser.get("/auth/start", params=LINK)
    target = urlparse(refused.headers["location"])
    assert refused.status_code == 303 and target.path == "/console/auth/start"  # not the provider
    query = parse_qs(target.query)
    assert query["purpose"] == ["reauthenticate"] and query["return_to"] == [LINK_START]
    assert "method" not in query  # any method already linked will do
    with accounts.engine.connect() as conn:
        assert not conn.scalar(text(f"SELECT count(*) FROM {accounts._t(conn, 'sign_in_flow')}"))
    done, _ = sign_in(browser, fake, purpose="reauthenticate", return_to=LINK_START, code="r")
    assert done.headers["location"] == LINK_START
    next_step = browser.get(LINK_START.removeprefix("/console"))
    assert next_step.headers["location"].startswith(ISSUER + "authorize")
    assert parse_qs(urlparse(next_step.headers["location"]).query)["connection"] == ["google-oauth2"]


def test_linking_needs_the_second_factor_when_any_organisation_of_the_person_does(console):
    accounts, fake, app = console
    browser = client(app)
    organisation, _, _ = onboard(browser, fake)
    accounts.grant_access(
        "op", organisation, tier="pilot", scope={"all": True}, source="manual", ends_at=None, reason="Pilot"
    )
    refused = browser.get("/auth/start", params=LINK)
    query = parse_qs(urlparse(refused.headers["location"]).query)
    assert query["purpose"] == ["second_factor"] and query["return_to"] == [LINK_START]
    sign_in(browser, fake, purpose="second_factor", amr=["mfa"], code="m")
    assert browser.get("/auth/start", params=LINK).headers["location"].startswith(ISSUER + "authorize")


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
    assert query["prompt"] == ["login"] and "acr_values" not in query
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
    }
    done = browser.get("/auth/callback", params={"state": query["state"][0], "code": "late"})
    assert done.headers["location"] == "/console/sign-in?error=wrong_account"
    assert session_times(accounts) == before


def test_parameters_a_browser_adds_to_the_start_change_nothing_asked_of_the_provider(console):
    accounts, fake, app = console
    browser = client(app)
    onboard(browser, fake)
    started = browser.get(
        "/auth/start",
        params=LINK
        | {"acr_values": "anything", "prompt": "none", "max_age": "999999", "scope": "openid admin"},
    )
    query = parse_qs(urlparse(started.headers["location"]).query)
    assert "acr_values" not in query and query["prompt"] == ["login"]
    assert query["scope"] == ["openid email profile"]


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
        "unlink": lambda: browser.delete(f"/api/v1/me/identities/{identity}", headers=headers),
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
    "unlink",
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
