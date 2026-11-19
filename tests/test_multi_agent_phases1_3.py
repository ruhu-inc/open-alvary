"""Tests for multi-agent review Phases 1, 2, and 3 (store, auth, operators, editorial, citations, claims)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, update
from test_passkeys import ORIGIN, SoftKey

from workbench import auth, claims, content
from workbench import store as workbench_store
from workbench.app import create_app
from workbench.passkeys import RelyingParty
from workbench.store import Store, connect, now, session, work_claim


@pytest.fixture
def store(tmp_path):
    s = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    s.add_operator("boss@example.org", "Boss", {"reviewer", "approver", "admin"})
    s.add_operator("human@example.org", "Human", {"reviewer"})
    s.add_operator("claude@example.org", "Claude", {"reviewer"}, agent=True)
    s.add_operator("codex@example.org", "Codex", {"reviewer"}, agent=True)
    return s


@pytest.fixture
def app_bench(tmp_path):
    s = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    s.add_operator("boss@example.org", "Boss", {"reviewer", "approver", "admin"})
    s.add_operator("human@example.org", "Human", {"reviewer"})
    s.add_operator("claude@example.org", "Claude", {"reviewer"}, agent=True)
    app = create_app(s, content.Library(tmp_path), dev_login=True, relying=RelyingParty(ORIGIN))
    return s, app


def _browser(app, email: str) -> TestClient:
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.post("/auth/dev", json={"email": email}).status_code == 200
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    return client


def _with_passkey(app, store: Store, email: str) -> TestClient:
    client = _browser(app, email)
    options = client.post("/api/passkeys/options", json={"purpose": "register"}).json()
    code = store.issue_enrolment(email)
    registered = client.post(
        "/api/passkeys",
        json={"name": "Key", "code": code, "credential": SoftKey().create(options)},
    )
    assert registered.status_code == 200
    return client


def _who(store: Store, email: str) -> dict:
    person = store.operator(email)
    assert person is not None
    return {"email": email, "operator": person}


# --- Schema, _upgrade, and connect pooling -------------------------------------------------------


def test_schema_upgrade_and_pooling(tmp_path, monkeypatch):
    engine = connect(f"sqlite:///{tmp_path / 'schema.db'}")
    # Running _upgrade a second time is idempotent.
    workbench_store._upgrade(engine)

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"work_claim", "work_release", "blocked_label", "review_audit"} <= tables

    op_cols = {c["name"] for c in insp.get_columns("operator")}
    assert {"agent", "may_change_approved"} <= op_cols
    page_cols = {c["name"] for c in insp.get_columns("page_revision")}
    assert {"label", "task"} <= page_cols
    detail_cols = {c["name"] for c in insp.get_columns("detail_revision")}
    assert {"label", "task"} <= detail_cols

    # Verify non-SQLite URLs receive pool settings while SQLite URLs do not.
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured[url] = kwargs
        return engine

    monkeypatch.setattr(workbench_store, "create_engine", fake_create_engine)
    monkeypatch.setattr(workbench_store.metadata, "create_all", lambda _e: None)
    monkeypatch.setattr(workbench_store, "_upgrade", lambda _e: None)

    workbench_store.connect("postgresql+psycopg://u:p@localhost/db")
    pg_kwargs = captured["postgresql+psycopg://u:p@localhost/db"]
    assert pg_kwargs["pool_size"] == 4
    assert pg_kwargs["max_overflow"] == 4
    assert pg_kwargs["pool_recycle"] == 1800

    workbench_store.connect("sqlite:///:memory:")
    sq_kwargs = captured["sqlite:///:memory:"]
    assert "pool_size" not in sq_kwargs


# --- Phase 1: Operator invariants, sessions API, pause-all, editorial/citations 403 --------------


def test_operator_agent_invariants_and_session_invalidation(store):
    # Refused on create with privileged roles.
    with pytest.raises(ValueError):
        store.add_operator("bad@example.org", "Bad", {"reviewer", "approver"}, agent=True)
    with pytest.raises(ValueError):
        store.add_operator("bad2@example.org", "Bad2", {"admin"}, agent=True)

    # Refused on update: giving an agent account a privileged role.
    with pytest.raises(ValueError):
        store.add_operator("claude@example.org", "Claude", {"reviewer", "approver"})

    # Refused on update: flipping agent=True on a privileged account.
    with pytest.raises(ValueError):
        store.add_operator("boss@example.org", "Boss", {"reviewer"}, agent=True)

    # Changing roles or agent or may_change_approved ends active sessions.
    tok, _ = store.start_session("claude@example.org")
    assert store.resume(tok) is not None
    store.add_operator("claude@example.org", "Claude", {"reviewer"}, may_change_approved=True)
    assert store.resume(tok) is None
    assert store.operator("claude@example.org")["may_change_approved"] is True

    # Pause all agents deactivates agents and ends their sessions, leaving humans untouched.
    tok_agent, _ = store.start_session("claude@example.org")
    tok_human, _ = store.start_session("human@example.org")
    paused = store.pause_all_agents(actor="boss@example.org")
    assert paused == ["claude@example.org", "codex@example.org"]
    assert store.resume(tok_agent) is None
    assert store.resume(tok_human) is not None
    assert store.operator("claude@example.org") is None
    assert store.operator("human@example.org") is not None


def test_operators_api_agent_flags_and_session_endpoints(app_bench):
    store, app = app_bench
    boss_no_pk = _browser(app, "boss@example.org")
    boss = _with_passkey(app, store, "boss@example.org")
    agent_client = _browser(app, "claude@example.org")

    # POST /api/operators supports agent & may_change_approved and enforces invariants.
    bad = boss.post(
        "/api/operators",
        json={"email": "x@example.org", "name": "X", "roles": ["reviewer", "admin"], "agent": True},
    )
    assert bad.status_code == 409
    assert bad.json()["code"] == "invalid_agent_roles"

    good = boss.post(
        "/api/operators",
        json={
            "email": "antigravity@example.org",
            "name": "Antigravity",
            "roles": ["reviewer"],
            "agent": True,
            "may_change_approved": False,
        },
    )
    assert good.status_code == 200
    ops = {o["email"]: o for o in boss.get("/api/operators").json()["operators"]}
    assert ops["antigravity@example.org"]["agent"] is True
    assert ops["antigravity@example.org"]["may_change_approved"] is False

    # GET /api/operators/{email}/sessions requires a fresh passkey!
    no_pk = boss_no_pk.get("/api/operators/claude@example.org/sessions")
    assert no_pk.status_code == 403
    assert no_pk.json()["code"] == "passkey_required"

    listed = boss.get("/api/operators/claude@example.org/sessions")
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    assert len(sessions) == 1
    short_id = sessions[0]["id"]
    assert len(short_id) == 12

    # End that single session -> agent gets 401 on next request.
    ended = boss.post(f"/api/operators/claude@example.org/sessions/{short_id}/end")
    assert ended.status_code == 200
    assert agent_client.get("/api/me").status_code == 401

    # End-all sessions.
    a1 = _browser(app, "claude@example.org")
    a2 = _browser(app, "claude@example.org")
    assert len(boss.get("/api/operators/claude@example.org/sessions").json()["sessions"]) == 2
    end_all = boss.post("/api/operators/claude@example.org/sessions/end-all")
    assert end_all.status_code == 200
    assert end_all.json()["ended"] == 2
    assert a1.get("/api/me").status_code == 401
    assert a2.get("/api/me").status_code == 401


def test_agent_refused_on_editorial_and_citations_write_routes(app_bench):
    _, app = app_bench
    agent = _browser(app, "claude@example.org")
    human = _browser(app, "human@example.org")

    expected = "Agent accounts may not change legal events, amendment instructions, or citations."
    for path, body in (
        ("/api/documents/doc-1/events/proposals", None),
        ("/api/documents/doc-1/events", {"status": "proposed", "event": {}}),
        ("/api/documents/doc-1/instructions", {"status": "proposed", "instruction": {}}),
        ("/api/documents/doc-1/citations/proposals", {"work": "doc-1", "proposals": []}),
    ):
        resp = agent.post(path, json=body) if body is not None else agent.post(path)
        assert resp.status_code == 403
        assert resp.json()["message"] == expected

        # Human reviewer passes the agent check (reaches 404 "No such document" instead of 403).
        h_resp = human.post(path, json=body) if body is not None else human.post(path)
        assert h_resp.status_code == 404


def test_approved_guard_and_shared_details_sibling(store, monkeypatch):
    monkeypatch.setenv("ALVARY_REVIEW_APPROVED_GUARD", "enforce")
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "off")

    store.record_approval(
        "rw-law-1-en",
        0,
        "boss@example.org",
        decision="approved",
        content_sha256="a" * 64,
        rights_basis="Public domain",
        materials={},
    )

    claude = _who(store, "claude@example.org")
    human = _who(store, "human@example.org")

    # Direct approved document is refused for agent.
    with store.engine.begin() as db:
        with pytest.raises(claims.ClaimError) as exc:
            claims.guard(db, "rw-law-1", claude, act_id="rw-law-1-en")
        assert exc.value.status_code == 403
        assert exc.value.message == claims.MSG_APPROVED

    # Shared Details edit via unapproved sibling rw-law-1-fr is ALSO refused when sibling rw-law-1-en is approved.
    with store.engine.begin() as db:
        with pytest.raises(claims.ClaimError) as exc:
            claims.guard(db, "rw-law-1", claude, approved_act_ids=["rw-law-1-fr", "rw-law-1-en"])
        assert exc.value.status_code == 403
        assert exc.value.message == claims.MSG_APPROVED

    # Human reviewer is not blocked by approved guard.
    with store.engine.begin() as db:
        assert claims.guard(db, "rw-law-1", human, approved_act_ids=["rw-law-1-en"]) is None

    # Agent with may_change_approved=True is allowed.
    store.add_operator("claude@example.org", "Claude", {"reviewer"}, may_change_approved=True)
    claude_allowed = _who(store, "claude@example.org")
    with store.engine.begin() as db:
        assert claims.guard(db, "rw-law-1", claude_allowed, approved_act_ids=["rw-law-1-en"]) is None

    # Switch off -> allowed for normal agent too.
    monkeypatch.setenv("ALVARY_REVIEW_APPROVED_GUARD", "off")
    with store.engine.begin() as db:
        assert claims.guard(db, "rw-law-1", claude, approved_act_ids=["rw-law-1-en"]) is None


# --- Phase 2: Atomic claims, concurrency, lost-token, blocked labels, guard, audits --------------


def test_claim_concurrency_no_duplicates(store):
    works = [f"work-{i:03d}" for i in range(20)]

    def worker(idx: int) -> str:
        res = claims.claim_next(
            store,
            works,
            "claude@example.org",
            f"claude-m1-{idx:02d}",
            task="review",
            check_limit=False,
        )
        return res["work_id"]

    with ThreadPoolExecutor(max_workers=20) as pool:
        claimed = list(pool.map(worker, range(20)))

    assert len(claimed) == 20
    assert len(set(claimed)) == 20
    assert set(claimed) == set(works)
    assert len(claims.list_claims(store)) == 20
    # Operational claim events must keep act_id = None so activity_stamps stays empty.
    assert store.activity_stamps() == {}


def test_one_claim_per_label_and_lost_token_recovery(store, monkeypatch):
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "enforce")
    c1 = claims.claim_work(store, "work-a", "claude@example.org", "claude-m1-01")
    assert c1["rotated"] is False

    # Same label claiming a second work -> 409 via partial unique index.
    with pytest.raises(claims.ClaimError) as exc:
        claims.claim_work(store, "work-b", "claude@example.org", "claude-m1-01")
    assert exc.value.status_code == 409
    assert exc.value.message == "claude-m1-01 already holds work-a. Release it before claiming another."

    # Lost-token recovery: same (account, label) re-claiming work-a rotates token.
    c2 = claims.claim_work(store, "work-a", "claude@example.org", "claude-m1-01")
    assert c2["rotated"] is True
    assert c2["token"] != c1["token"]

    # Old token is now rejected; new token works.
    claude = _who(store, "claude@example.org")
    with store.engine.begin() as db:
        with pytest.raises(claims.ClaimError) as exc:
            claims.guard(db, "work-a", claude, token=c1["token"])
        assert exc.value.status_code == 409

    with store.engine.begin() as db:
        res = claims.guard(db, "work-a", claude, token=c2["token"])
        assert res["label"] == "claude-m1-01"

    # Release by matching (account, label) without token works too.
    rel = claims.release_claim(store, "work-a", claude, outcome="done", label="claude-m1-01")
    assert rel["outcome"] == "done"
    assert claims.list_claims(store) == []


def test_lapsed_claim_revival_takeover_and_blocked_label(store, monkeypatch):
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "enforce")
    c1 = claims.claim_work(store, "work-lapsed", "claude@example.org", "claude-m1-02")

    # Expire the claim.
    past = now() - timedelta(minutes=5)
    with store.engine.begin() as db:
        db.execute(update(work_claim).where(work_claim.c.work_id == "work-lapsed").values(expires_at=past))

    # Holder can revive its own lapsed claim via renew_claim.
    renewed = claims.renew_claim(store, "work-lapsed", "claude@example.org", c1["token"])
    assert renewed["work_id"] == "work-lapsed"

    # Block the label: renewal, guard, and new claims are all refused with 403.
    claims.block_label(
        store, "claude@example.org", "claude-m1-02", blocked_by="boss@example.org", reason="audit"
    )
    assert claims.is_label_blocked(store, "claude@example.org", "claude-m1-02") is not None

    with pytest.raises(claims.ClaimError) as exc:
        claims.renew_claim(store, "work-lapsed", "claude@example.org", c1["token"])
    assert exc.value.status_code == 403
    assert exc.value.message == "The label claude-m1-02 has been paused by the owner. Stop and report."

    claude = _who(store, "claude@example.org")
    with store.engine.begin() as db:
        with pytest.raises(claims.ClaimError) as exc:
            claims.guard(db, "work-lapsed", claude, token=c1["token"])
        assert exc.value.status_code == 403

    with pytest.raises(claims.ClaimError) as exc:
        claims.claim_next(store, ["work-other"], "claude@example.org", "claude-m1-02")
    assert exc.value.status_code == 403

    # Unblock restores access.
    assert claims.unblock_label(store, "claude@example.org", "claude-m1-02", actor="boss@example.org")
    assert claims.is_label_blocked(store, "claude@example.org", "claude-m1-02") is None

    # Expire again and let another agent take it over.
    with store.engine.begin() as db:
        db.execute(update(work_claim).where(work_claim.c.work_id == "work-lapsed").values(expires_at=past))
    c2 = claims.claim_work(store, "work-lapsed", "codex@example.org", "codex-m1-01")
    assert c2["work_id"] == "work-lapsed"


def test_claim_rate_limit_per_account_and_label(store):
    for i in range(5):
        claims.claim_work(store, f"w-{i}", "claude@example.org", "claude-m1-05")
        claims.release_claim(
            store, f"w-{i}", _who(store, "claude@example.org"), outcome="abandoned", label="claude-m1-05"
        )
    # 6th call within 60s for same (account, label) -> 429.
    with pytest.raises(claims.ClaimError) as exc:
        claims.claim_work(store, "w-6", "claude@example.org", "claude-m1-05")
    assert exc.value.status_code == 429
    assert exc.value.message == "Too many claim requests for claude-m1-05. Wait a minute and try again."

    # Different label under same account is unaffected.
    ok = claims.claim_work(store, "w-6", "claude@example.org", "claude-m1-06")
    assert ok["work_id"] == "w-6"


def test_guard_modes_human_implicit_owner_override_and_warn(store, monkeypatch):
    claude = _who(store, "claude@example.org")
    human = _who(store, "human@example.org")
    boss = _who(store, "boss@example.org")

    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "enforce")

    # Agent without claim -> 409.
    with store.engine.begin() as db:
        with pytest.raises(claims.ClaimError) as exc:
            claims.guard(db, "work-free", claude)
        assert exc.value.status_code == 409
        assert exc.value.message == claims.MSG_AGENT_NEEDS_CLAIM

    # Human on free work -> takes implicit 'unlabelled' claim; can hold multiple unlabelled works.
    with store.engine.begin() as db:
        g1 = claims.guard(db, "work-free", human)
        assert g1["implicit"] is True and g1["label"] == "unlabelled" and g1["claim_token"]
    with store.engine.begin() as db:
        g2 = claims.guard(db, "work-free-2", human)
        assert g2["implicit"] is True and g2["label"] == "unlabelled"

    # Agent trying to save on human-held work -> 409 with holder details.
    with store.engine.begin() as db:
        with pytest.raises(claims.ClaimError) as exc:
            claims.guard(db, "work-free", claude)
        assert exc.value.status_code == 409
        assert "This work is being reviewed by unlabelled (human@example.org) until " in exc.value.message
        assert " UTC. Nothing was saved. Claim another work." in exc.value.message

    # Owner (approver/admin) can save over a claim, logged as saved_over_claim with act_id=None.
    with store.engine.begin() as db:
        ov = claims.guard(db, "work-free", boss)
        assert ov["saved_over_claim"] is True
    events = store.events()
    assert any(e["action"] == "saved_over_claim" and e["act_id"] is None for e in events)

    # Warn mode logs claim_would_refuse (act_id=None) and allows save to proceed.
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "warn")
    with store.engine.begin() as db:
        w = claims.guard(db, "work-free", claude, act_id="work-free-en")
        assert w["would_refuse"] is True and w["reason"] == "held_by_other"
    assert any(e["action"] == "claim_would_refuse" and e["act_id"] is None for e in store.events())
    assert store.activity_stamps() == {}


def test_revisions_store_label_task_and_review_audit_isolation(store):
    rev = store.save_page(
        "doc-1",
        1,
        0,
        "claude@example.org",
        label="claude-m1-01",
        task="review",
        reading_sha256="r" * 64,
        units=[{"kind": "paragraph", "text": "Original text."}],
        status="checked",
        resolutions={},
    )
    assert rev == 1
    latest = store.latest_pages("doc-1")[1]
    assert latest["label"] == "claude-m1-01" and latest["task"] == "review"

    drev = store.save_details(
        "doc-1",
        0,
        "claude@example.org",
        label="claude-m1-01",
        task="review",
        details={"identity.title": "Act"},
        absent={},
        status="confirmed",
    )
    assert drev == 1
    dlatest = store.latest_details("doc-1")
    assert dlatest["label"] == "claude-m1-01" and dlatest["task"] == "review"

    # Auditor submission NEVER touches page_revision.
    audit_id = store.create_audit_draw(
        work_id="doc-1",
        act_id="doc-1",
        page=1,
        release_id=1,
        reviewer="claude@example.org",
        reviewer_label="claude-m1-01",
        reviewer_revision=1,
    )
    assert len(store.list_audits(work_id="doc-1", open_only=True)) == 1

    submitted = store.save_audit_submission(
        audit_id,
        auditor="codex@example.org",
        auditor_label="codex-m1-01",
        units=[{"kind": "paragraph", "text": "Corrected text."}],
        resolutions={},
        status="checked",
        paragraphs_changed=1,
        words_changed=1,
        other_changes=0,
        verdict="material",
    )
    assert submitted["verdict"] == "material"
    # Live page_revision is still revision 1 with "Original text."
    assert store.latest_pages("doc-1")[1]["revision"] == 1
    assert store.latest_pages("doc-1")[1]["units"][0]["text"] == "Original text."

    # Owner resolves accepted_auditor -> writes revision 2 under owner's account.
    resolved = store.resolve_audit(audit_id, decision="accepted_auditor", decided_by="boss@example.org")
    assert resolved["new_revision"] == 2
    assert store.latest_pages("doc-1")[1]["revision"] == 2
    assert store.latest_pages("doc-1")[1]["author"] == "boss@example.org"
    assert store.latest_pages("doc-1")[1]["units"][0]["text"] == "Corrected text."


# --- Phase 3: Per-account session limits, 60s last_seen throttle, cookie max_age -----------------


def test_session_limits_throttle_and_cookie_max_age(store, monkeypatch):
    t0 = now()
    monkeypatch.setattr(workbench_store, "now", lambda: t0)

    tok_agent, _ = store.start_session("claude@example.org")
    tok_human, _ = store.start_session("human@example.org")
    tok_boss, _ = store.start_session("boss@example.org")

    # 30 seconds later: resume succeeds, last_seen is NOT updated (< 60s throttle).
    monkeypatch.setattr(workbench_store, "now", lambda: t0 + timedelta(seconds=30))
    assert store.resume(tok_agent) is not None
    with store.engine.connect() as db:
        seen = db.execute(
            select(session.c.last_seen).where(session.c.token_hash == workbench_store.digest(tok_agent))
        ).scalar()
    assert workbench_store._aware(seen) == t0

    # 65 seconds later: last_seen IS updated.
    t65 = t0 + timedelta(seconds=65)
    monkeypatch.setattr(workbench_store, "now", lambda: t65)
    assert store.resume(tok_agent) is not None
    with store.engine.connect() as db:
        seen2 = db.execute(
            select(session.c.last_seen).where(session.c.token_hash == workbench_store.digest(tok_agent))
        ).scalar()
    assert workbench_store._aware(seen2) == t65

    # 2 hours later: human & boss sessions expired (1h idle), agent still alive (24h idle).
    monkeypatch.setattr(workbench_store, "now", lambda: t0 + timedelta(hours=2))
    assert store.resume(tok_human) is None
    assert store.resume(tok_boss) is None
    assert store.resume(tok_agent) is not None

    # 169 hours later: agent exceeds 168h max life -> expired.
    monkeypatch.setattr(workbench_store, "now", lambda: t0 + timedelta(hours=169))
    assert store.resume(tok_agent) is None

    # Cookie max_age matches account limits.
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "scheme": "http",
        "headers": [],
        "server": ("127.0.0.1", 8090),
        "client": ("127.0.0.1", 12345),
    }
    req = Request(scope)
    assert auth.cookie_options(req)["max_age"] == 12 * 3600
    assert (
        auth.cookie_options(req, max_age=auth.cookie_max_age(store.operator("claude@example.org")))["max_age"]
        == 168 * 3600
    )
    assert (
        auth.cookie_options(req, max_age=auth.cookie_max_age(store.operator("boss@example.org")))["max_age"]
        == 12 * 3600
    )
