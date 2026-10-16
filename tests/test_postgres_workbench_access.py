"""MAC-11 end to end: the workbench's Access section needs the access_admin role, a passkey enrolled
with a server-issued code, and a passkey check within 10 minutes for every change."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_passkeys import ORIGIN, SoftKey
from test_postgres import pg_engine as pg_engine
from test_postgres_access_admin import person
from test_postgres_access_admin import world as world  # noqa: F401  (fixture)

from workbench import content
from workbench import store as workbench_store
from workbench.app import create_app
from workbench.passkeys import RelyingParty
from workbench.store import Store, connect

pytestmark = pytest.mark.postgres


@pytest.fixture
def bench(world, tmp_path):
    owner_store, admin, _, mailer = world
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("ops@example.org", "Ops", {"reviewer", "access_admin"})
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    app = create_app(
        store, content.Library(tmp_path), dev_login=True, access=admin, relying=RelyingParty(ORIGIN)
    )
    return owner_store, store, app, mailer


def browser(app, email):
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.post("/auth/dev", json={"email": email}).status_code == 200
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    return client


def enrol(client, store, key, code=None):
    options = client.post("/api/passkeys/options", json={"purpose": "register"}).json()
    code = code or store.issue_enrolment("ops@example.org")
    return client.post(
        "/api/passkeys", json={"name": "Laptop", "code": code, "credential": key.create(options)}
    )


def check(client, key):
    options = client.post("/api/passkeys/options", json={"purpose": "verify"}).json()
    return client.post("/api/passkeys/verify", json={"credential": key.get(options)})


def test_only_access_admins_reach_it_and_a_first_passkey_needs_the_enrolment_code(bench):
    _, store, app, _ = bench
    reviewer = browser(app, "reviewer@example.org")
    assert reviewer.get("/api/me").json()["access"] is False
    assert reviewer.get("/api/access/requests").status_code == 403
    ops = browser(app, "ops@example.org")
    assert ops.get("/api/me").json()["access"] is True
    assert ops.get("/api/passkeys/me").json()["needs_enrolment"] is True
    key = SoftKey()
    refused = enrol(ops, store, key, code="AAAA-BBBB-CCCC-DDDD")
    assert refused.status_code == 403 and refused.json()["code"] == "enrolment_required"
    code = store.issue_enrolment("ops@example.org")
    assert enrol(ops, store, key, code=code.replace("-", " ").lower()).status_code == 200
    # A further passkey needs a passkey check, not a code: another session has none yet.
    elsewhere = browser(app, "ops@example.org")
    later = enrol(elsewhere, store, SoftKey(), code=code)
    assert later.status_code == 403 and later.json()["code"] == "passkey_required"
    assert enrol(ops, store, SoftKey(), code="unused").status_code == 200  # this session has checked
    status = ops.get("/api/passkeys/me").json()
    assert [p["name"] for p in status["passkeys"]] == ["Laptop", "Laptop"] and status["fresh_until"]


def test_changes_need_a_passkey_check_within_ten_minutes(bench, monkeypatch):
    owner_store, store, app, mailer = bench
    amina, own = person(owner_store, "amina")
    ops = browser(app, "ops@example.org")
    key = SoftKey()
    enrol(ops, store, key)
    grant = {"tier": "pilot", "reason": "Pilot agreed", "evidence": "email 2026-10-16"}
    assert ops.post(f"/api/access/organisations/{own}/grant", json=grant).status_code == 200
    assert owner_store.access(own)["tier"]["id"] == "pilot"
    later = workbench_store.now() + timedelta(minutes=11)
    monkeypatch.setattr("workbench.privileged.now", lambda: later)
    stale = ops.post(f"/api/access/organisations/{own}/revoke", json={"reason": "Ended"})
    assert stale.status_code == 403 and stale.json()["code"] == "passkey_required"
    assert check(ops, key).status_code == 200
    monkeypatch.setattr("workbench.privileged.now", workbench_store.now)
    assert ops.post(f"/api/access/organisations/{own}/revoke", json={"reason": "Ended"}).status_code == 200
    assert mailer.sent[-1].to == "amina@example.org"
    log = ops.get(f"/api/access/audit?organisation={own}").json()
    assert [e["action"] for e in log[:2]] == ["entitlement.revoked", "entitlement.granted"]
    assert log[0]["actor"] == "ops@example.org" and log[0]["client_family"] == "review workbench"
    # No CSRF token, no change, even with a fresh check.
    ops.headers.pop("X-CSRF-Token")
    assert (
        ops.post(
            f"/api/access/organisations/{own}/state", json={"change": "suspend", "reason": "x"}
        ).status_code
        == 403
    )


def test_a_wrong_passkey_or_a_replayed_challenge_is_refused_and_recorded(bench):
    _, store, app, _ = bench
    ops = browser(app, "ops@example.org")
    key = SoftKey()
    enrol(ops, store, key)
    options = ops.post("/api/passkeys/options", json={"purpose": "verify"}).json()
    answer = key.get(options)
    assert ops.post("/api/passkeys/verify", json={"credential": answer}).status_code == 200
    replay = ops.post("/api/passkeys/verify", json={"credential": answer})
    assert replay.status_code == 400 and replay.json()["code"] == "challenge_expired"
    stranger = SoftKey()
    options = ops.post("/api/passkeys/options", json={"purpose": "verify"}).json()
    assert ops.post("/api/passkeys/verify", json={"credential": stranger.get(options)}).status_code == 403
    assert {"passkey_added", "passkey_checked"} <= {e["action"] for e in store.events()}


def test_the_organisation_view_and_search_show_no_secrets(bench):
    owner_store, store, app, _ = bench
    amina, own = person(owner_store, "amina", "Kigali Law Clinic")
    secret = owner_store.create_key(amina, own, kind="personal", name="laptop")["secret"]
    ops = browser(app, "ops@example.org")
    assert [o["id"] for o in ops.get("/api/access/search?q=kigali").json()] == [own]
    view = ops.get(f"/api/access/organisations/{own}")
    assert view.status_code == 200 and secret not in view.text and "secret_hash" not in view.text
    assert {t["id"] for t in ops.get("/api/access/tiers").json()} >= {"starter", "pilot"}
