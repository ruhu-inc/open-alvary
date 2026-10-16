"""Operators page: administrators add, change and deactivate workbench users and issue passkey codes,
each change confirmed with their own passkey; nobody deactivates themselves or the last administrator."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_passkeys import ORIGIN, SoftKey

from workbench import content
from workbench import store as workbench_store
from workbench.app import create_app
from workbench.passkeys import RelyingParty
from workbench.store import Store, connect


@pytest.fixture
def bench(tmp_path):
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("boss@example.org", "Boss", {"reviewer", "admin"})
    store.add_operator("rev@example.org", "Rev", {"reviewer"})
    app = create_app(store, content.Library(tmp_path), dev_login=True, relying=RelyingParty(ORIGIN))
    return store, app


def browser(app, email):
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.post("/auth/dev", json={"email": email}).status_code == 200
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    return client


def with_passkey(app, store, email):
    """Signed in, with a passkey registered from an enrolment code (which checks it too)."""
    client = browser(app, email)
    options = client.post("/api/passkeys/options", json={"purpose": "register"}).json()
    code = store.issue_enrolment(email)
    registered = client.post(
        "/api/passkeys", json={"name": "Key", "code": code, "credential": SoftKey().create(options)}
    )
    assert registered.status_code == 200
    return client


def test_only_administrators_see_and_change_operators(bench):
    store, app = bench
    reviewer = browser(app, "rev@example.org")
    assert reviewer.get("/api/me").json()["operators"] is False
    assert reviewer.get("/api/operators").status_code == 403
    boss = browser(app, "boss@example.org")
    assert boss.get("/api/me").json()["operators"] is True
    listed = boss.get("/api/operators").json()
    assert {o["email"] for o in listed["operators"]} == {"boss@example.org", "rev@example.org"}
    assert "access_admin" in listed["roles"]
    # A change without a passkey check is refused.
    added = boss.post("/api/operators", json={"email": "new@gmail.com", "name": "New", "roles": ["reviewer"]})
    assert added.status_code == 403 and added.json()["code"] == "passkey_required"


def test_an_administrator_adds_a_person_issues_their_code_and_deactivates_them(bench, monkeypatch):
    store, app = bench
    boss = with_passkey(app, store, "boss@example.org")
    added = boss.post(
        "/api/operators",
        json={"email": " John@Gmail.com ", "name": "John  Doe", "roles": ["reviewer", "approver"]},
    )
    assert added.status_code == 200 and added.json()["added"] is True
    john = next(o for o in boss.get("/api/operators").json()["operators"] if o["email"] == "john@gmail.com")
    assert john["name"] == "John Doe" and john["roles"] == ["approver", "reviewer"] and john["passkeys"] == 0
    # A reviewer has no use for a passkey; an access administrator gets a code, once.
    assert boss.post("/api/operators/john@gmail.com/enrolment", json={}).json()["code"] == "no_passkey_needed"
    boss.post(
        "/api/operators",
        json={"email": "john@gmail.com", "name": "John Doe", "roles": ["reviewer", "access_admin"]},
    )
    code = boss.post("/api/operators/john@gmail.com/enrolment", json={}).json()["code"]
    assert store.take_enrolment("john@gmail.com", code)
    assert boss.post("/api/operators/john@gmail.com/active", json={"active": False}).status_code == 200
    assert store.operator("john@gmail.com") is None
    assert boss.post("/api/operators/john@gmail.com/enrolment", json={}).json()["code"] == "inactive"
    assert boss.post("/api/operators/john@gmail.com/active", json={"active": True}).status_code == 200
    assert store.operator("john@gmail.com")["roles"] == {"reviewer", "access_admin"}
    actions = [e["action"] for e in store.events()]
    assert {
        "operator_saved",
        "operator_deactivated",
        "operator_reactivated",
        "passkey_enrolment_issued",
    } <= set(actions)
    # Ten minutes on, a change needs the passkey again.
    later = workbench_store.now() + timedelta(minutes=11)
    monkeypatch.setattr("workbench.privileged.now", lambda: later)
    stale = boss.post("/api/operators/john@gmail.com/active", json={"active": False})
    assert stale.status_code == 403 and stale.json()["code"] == "passkey_required"


def test_nobody_deactivates_themselves_or_the_last_administrator(bench):
    store, app = bench
    boss = with_passkey(app, store, "boss@example.org")
    assert (
        boss.post("/api/operators/boss@example.org/active", json={"active": False}).json()["code"] == "self"
    )
    demoted = boss.post(
        "/api/operators", json={"email": "boss@example.org", "name": "Boss", "roles": ["reviewer"]}
    )
    assert demoted.status_code == 409 and demoted.json()["code"] == "last_admin"
    boss.post(
        "/api/operators", json={"email": "rev@example.org", "name": "Rev", "roles": ["reviewer", "admin"]}
    )
    assert (
        boss.post(
            "/api/operators", json={"email": "boss@example.org", "name": "Boss", "roles": ["reviewer"]}
        ).status_code
        == 200
    )


def test_a_lost_passkey_is_replaced_only_on_purpose(bench):
    store, app = bench
    boss = with_passkey(app, store, "boss@example.org")
    store.add_operator("rev@example.org", "Rev", {"reviewer", "admin"})
    with_passkey(app, store, "rev@example.org")  # Rev now holds a passkey
    refused = boss.post("/api/operators/rev@example.org/enrolment", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "has_passkey"
    replaced = boss.post("/api/operators/rev@example.org/enrolment", json={"replace": True})
    assert replaced.status_code == 200 and store.passkeys("rev@example.org") == []


def test_bad_input_is_refused(bench):
    store, app = bench
    boss = with_passkey(app, store, "boss@example.org")
    for body in (
        {"email": "not-an-email", "name": "X", "roles": ["reviewer"]},
        {"email": "x@example.org", "name": "", "roles": ["reviewer"]},
        {"email": "x@example.org", "name": "X", "roles": ["superuser"]},
        {"email": "x@example.org", "name": "X", "roles": []},
    ):
        assert boss.post("/api/operators", json=body).status_code == 400
    assert boss.post("/api/operators/nobody@example.org/active", json={"active": True}).status_code == 404
