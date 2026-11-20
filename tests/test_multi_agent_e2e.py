"""End-to-end FastAPI TestClient tests for multi-agent review wiring (workbench/app.py)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_passkeys import ORIGIN, SoftKey

from workbench import claims, content
from workbench.app import create_app
from workbench.passkeys import RelyingParty
from workbench.store import Store, connect, now, work_claim

WORK_LAW = "rw-law-001-2024"
DOC_EN = "rw-law-001-2024-en"
DOC_FR = "rw-law-001-2024-fr"
WORK_LAW2 = "rw-law-002-2024"
DOC_LAW2 = "rw-law-002-2024-en"
WORK_JUDGMENT = "ng-judgment-001-2024"
DOC_JUDGMENT = "ng-judgment-001-2024"


def _make_reading(prefix: str, pages: int = 3) -> dict:
    page_list = []
    for num in range(1, pages + 1):
        text = f"Article {num}. {prefix} provision text on page {num} with enough words to check."
        page_list.append(
            {
                "page": num,
                "nodes": [{"kind": "paragraph", "level": "paragraph", "text": text, "page": num}],
                "text": text,
                "tables": [],
                "furniture": [],
                "flags": [],
                "checks": {"mineru": "same", "azure": "same"},
            }
        )
    return {
        "input_sha256": "a" * 64,
        "primary": {
            "reader": "docai",
            "version": {"version": "pretrained-layout-parser-v1.6-pro-2025-12-01"},
        },
        "checks": {"mineru": {}, "azure": {}},
        "summary": {
            "pages": pages,
            "flags": {"high": 0, "medium": 0, "low": 0},
            "restored_markers": 0,
            "missing_checks": [],
        },
        "pages": page_list,
    }


def _manifest(docs: list[dict]) -> dict:
    return {
        "jurisdiction": "rw",
        "description": "Test collection",
        "rights_basis": "Official Gazette public domain",
        "documents": docs,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "enforce")
    monkeypatch.setenv("ALVARY_REVIEW_APPROVED_GUARD", "enforce")
    monkeypatch.setenv("ALVARY_REVIEW_STATUS_GATE", "off")
    monkeypatch.setenv("OPEN_ALVARY_REVIEW_AUDIT_SALT", "e2e-audit-salt-secret")
    monkeypatch.setenv("ALVARY_REVIEW_AUDIT_RATE", "0.5")
    monkeypatch.setenv("ALVARY_REVIEW_AUDIT_MINIMUM", "2")

    staging = tmp_path / "staging"
    staging.mkdir()
    for doc_id, prefix, pages in [
        (DOC_EN, "English Law 1", 3),
        (DOC_FR, "French Law 1", 3),
        (DOC_LAW2, "English Law 2", 3),
        (DOC_JUDGMENT, "Supreme Court", 2),
    ]:
        (staging / f"{doc_id}.reading.json").write_text(json.dumps(_make_reading(prefix, pages=pages)))

    docs = [
        {
            "id": DOC_EN,
            "work": WORK_LAW,
            "title": "Law No. 001/2024 on Data",
            "type": "law",
            "year": 2024,
            "jurisdiction": "rw",
            "language": "en",
            "page_count": 3,
            "versions": {"en": DOC_EN, "fr": DOC_FR},
            "flag_counts": {"high": 0, "medium": 0, "low": 0},
        },
        {
            "id": DOC_FR,
            "work": WORK_LAW,
            "title": "Loi No. 001/2024 sur les Donnees",
            "type": "law",
            "year": 2024,
            "jurisdiction": "rw",
            "language": "fr",
            "page_count": 3,
            "versions": {"en": DOC_EN, "fr": DOC_FR},
            "flag_counts": {"high": 0, "medium": 0, "low": 0},
        },
        {
            "id": DOC_LAW2,
            "work": WORK_LAW2,
            "title": "Law No. 002/2024 on Trade",
            "type": "law",
            "year": 2024,
            "jurisdiction": "rw",
            "language": "en",
            "page_count": 3,
            "versions": {"en": DOC_LAW2},
            "flag_counts": {"high": 0, "medium": 0, "low": 0},
        },
        {
            "id": DOC_JUDGMENT,
            "work": WORK_JUDGMENT,
            "title": "Ade v. State",
            "type": "judgment",
            "year": 2024,
            "jurisdiction": "ng",
            "language": "en",
            "page_count": 2,
            "flag_counts": {"high": 0, "medium": 0, "low": 0},
        },
    ]
    manifest_data = _manifest(docs)
    monkeypatch.setattr(content.first_collection, "load_manifest", lambda: manifest_data)
    monkeypatch.setattr(content, "extra_collections", lambda: [])

    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("owner@example.org", "Owner", {"reviewer", "approver", "admin"})
    store.add_operator("human@example.org", "Human", {"reviewer"})
    store.add_operator("claude@example.org", "Claude Fleet", {"reviewer"}, agent=True)
    store.add_operator("codex@example.org", "Codex Fleet", {"reviewer"}, agent=True)

    library = content.Library(staging)
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(library, "image", lambda act_id, page: image)

    app = create_app(store, library, dev_login=True, relying=RelyingParty(ORIGIN))
    return store, library, app


def _browser(app, email: str) -> TestClient:
    client = TestClient(app, base_url="http://127.0.0.1")
    res = client.post("/auth/dev", json={"email": email})
    assert res.status_code == 200, res.text
    client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
    return client


def _with_passkey(app, store: Store, email: str) -> TestClient:
    client = _browser(app, email)
    options = client.post("/api/passkeys/options", json={"purpose": "register"}).json()
    code = store.issue_enrolment(email)
    reg = client.post(
        "/api/passkeys",
        json={"name": "Owner Key", "code": code, "credential": SoftKey().create(options)},
    )
    assert reg.status_code == 200, reg.text
    return client


def _confirm_details_payload(client: TestClient, act_id: str, **overrides) -> dict:
    view = client.get(f"/api/documents/{act_id}/details").json()
    record = view["record"]
    allowed = {f["key"] for f in view["standard"]["fields"]}
    vals = dict(record["details"])
    absent = dict(record["absent"])
    defaults: dict = {
        "status.legal": "unknown",
        "text.form": "gazette_publication" if vals.get("identity.family") != "judgment" else "court_copy",
        "expressions": [
            {"language": "en", "title": vals.get("identity.title") or "Title", "this_text": "yes"},
            {"language": "fr", "title": vals.get("identity.title") or "Titre", "this_text": "no"},
        ],
    }
    for key, val in defaults.items():
        if key in allowed and key not in overrides and not vals.get(key):
            vals[key] = val
            absent.pop(key, None)
    if "expressions" in allowed and vals.get("expressions"):
        if not any(row.get("this_text") == "yes" for row in vals["expressions"]):
            vals["expressions"][0]["this_text"] = "yes"
    for key in view["missing"]:
        if key not in overrides and key not in defaults:
            vals.pop(key, None)
            absent[key] = "not_in_source"
    vals.update(overrides)
    for key in overrides:
        absent.pop(key, None)
    return {
        "expected_revision": record["revision"],
        "details": vals,
        "absent": absent,
        "status": "confirmed",
        "comment": "Confirmed in E2E test",
    }


def _save_all_pages(client: TestClient, act_id: str, headers: dict | None = None) -> None:
    doc = client.get(f"/api/documents/{act_id}").json()
    for p in doc["pages"]:
        page_num = p["page"]
        detail = client.get(f"/api/documents/{act_id}/pages/{page_num}").json()
        units = (detail["latest"] or {}).get("units") or detail["machine_units"]
        expected = (detail["latest"] or {}).get("revision", 0)
        res = client.post(
            f"/api/documents/{act_id}/pages/{page_num}",
            json={
                "expected_revision": expected,
                "units": units,
                "status": "checked",
                "resolutions": {},
                "comment": "checked",
            },
            headers=headers or {},
        )
        assert res.status_code == 200, res.text


def _approve_doc(owner: TestClient, act_id: str):
    doc = owner.get(f"/api/documents/{act_id}").json()
    canonical = owner.get(f"/api/documents/{act_id}/canonical").json()
    materials = {k: True for k in doc["materials"]}
    return owner.post(
        f"/api/documents/{act_id}/approval",
        json={
            "decision": "approved",
            "expected_revision": len(doc["approvals"]),
            "materials": materials,
            "rights_basis": "Official Gazette public domain",
            "shown": {
                "content_sha256": doc["content_sha256"],
                "details_sha256": doc["details_sha256"],
                "text_sha256": canonical["text_sha256"],
            },
        },
    )


def test_dynamic_cookie_max_age(env):
    _, _, app = env
    agent_client = TestClient(app, base_url="http://127.0.0.1")
    agent_res = agent_client.post("/auth/dev", json={"email": "claude@example.org"})
    assert agent_res.status_code == 200
    assert "Max-Age=604800" in agent_res.headers["set-cookie"]

    human_client = TestClient(app, base_url="http://127.0.0.1")
    human_res = human_client.post("/auth/dev", json={"email": "human@example.org"})
    assert human_res.status_code == 200
    assert "Max-Age=43200" in human_res.headers["set-cookie"]


def test_claim_lifecycle_guards_implicit_and_activity_renewal(env):
    store, _, app = env
    claude = _browser(app, "claude@example.org")
    codex = _browser(app, "codex@example.org")
    human = _browser(app, "human@example.org")
    owner = _browser(app, "owner@example.org")

    # Agent cannot save without an explicit claim.
    detail = claude.get(f"/api/documents/{DOC_EN}/pages/1").json()
    unclaimed_save = claude.post(
        f"/api/documents/{DOC_EN}/pages/1",
        json={
            "expected_revision": 0,
            "units": detail["machine_units"],
            "status": "checked",
            "resolutions": {},
        },
    )
    assert unclaimed_save.status_code == 409
    assert "Claim this work first" in unclaimed_save.json()["message"]

    # Agent claims next work in jurisdiction=rw.
    claimed = claude.post(
        "/api/queue/claim-next",
        json={"label": "claude-m1-01", "task": "review", "jurisdiction": "rw", "max_pages": 10},
    )
    assert claimed.status_code == 200, claimed.text
    claim_data = claimed.json()
    work_id = claim_data["work_id"]
    token = claim_data["token"]
    first_doc_id = claim_data["documents"][0]["id"]
    assert claim_data["task"] == "review"
    assert claim_data["documents"][0]["address"] == f"#/doc/{first_doc_id}"

    # Document detail & queue show claim info; admin GET /api/claims lists it.
    doc_view = claude.get(f"/api/documents/{first_doc_id}").json()
    assert doc_view["claim"]["label"] == "claude-m1-01"
    queue_view = claude.get("/api/queue").json()
    claimed_tasks = [t for t in queue_view["tasks"] if t["id"] == first_doc_id]
    assert claimed_tasks and claimed_tasks[0]["claim"]["label"] == "claude-m1-01"
    assert claude.get("/api/claims").status_code == 403
    admin_claims = owner.get("/api/claims").json()["claims"]
    assert any(c["work_id"] == work_id and c["label"] == "claude-m1-01" for c in admin_claims)

    # Another agent is refused with 409 naming the holder.
    page1 = codex.get(f"/api/documents/{first_doc_id}/pages/1").json()
    other_save = codex.post(
        f"/api/documents/{first_doc_id}/pages/1",
        json={
            "expected_revision": 0,
            "units": page1["machine_units"],
            "status": "checked",
            "resolutions": {},
        },
    )
    assert other_save.status_code == 409
    assert "claude-m1-01" in other_save.json()["message"]

    # Holder saves with X-Claim-Token.
    saved = claude.post(
        f"/api/documents/{first_doc_id}/pages/1",
        json={
            "expected_revision": 0,
            "units": page1["machine_units"],
            "status": "checked",
            "resolutions": {},
        },
        headers={"X-Claim-Token": token},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1

    # Activity renewal: backdate the last activity by 2 minutes, then GET page with X-Claim-Token.
    with store.engine.begin() as db:
        db.execute(
            update(work_claim)
            .where(work_claim.c.work_id == work_id)
            .values(renewed_at=now() - timedelta(minutes=2), active_at=now() - timedelta(minutes=2))
        )
    before_renew = owner.get("/api/claims").json()["claims"][0]["renewed_at"]
    assert (
        claude.get(f"/api/documents/{first_doc_id}/pages/1", headers={"X-Claim-Token": token}).status_code
        == 200
    )
    after_renew = owner.get("/api/claims").json()["claims"][0]["renewed_at"]
    assert after_renew > before_renew

    # Explicit renew endpoint works.
    renewed = claude.post(f"/api/claims/{work_id}/renew", headers={"X-Claim-Token": token})
    assert renewed.status_code == 200
    assert renewed.json()["work_id"] == work_id

    # Lost-token recovery rotates token for same (account, label).
    rotated = claude.post(f"/api/claims/{work_id}", json={"label": "claude-m1-01", "task": "review"})
    assert rotated.status_code == 200
    assert rotated.json()["rotated"] is True
    new_token = rotated.json()["token"]
    assert new_token != token

    # Owner can save over an active claim.
    owner_save = owner.post(
        f"/api/documents/{first_doc_id}/pages/1",
        json={
            "expected_revision": 1,
            "units": page1["machine_units"],
            "status": "checked",
            "resolutions": {},
        },
    )
    assert owner_save.status_code == 200

    # Release claim.
    rel = claude.post(
        f"/api/claims/{work_id}/release",
        json={"outcome": "abandoned", "reason": "switching"},
        headers={"X-Claim-Token": new_token},
    )
    assert rel.status_code == 200

    # Human reviewer takes an implicit claim on first save and receives claim_token + work/docs.
    j_page = human.get(f"/api/documents/{DOC_JUDGMENT}/pages/1").json()
    human_save = human.post(
        f"/api/documents/{DOC_JUDGMENT}/pages/1",
        json={
            "expected_revision": 0,
            "units": j_page["machine_units"],
            "status": "checked",
            "resolutions": {},
        },
    )
    assert human_save.status_code == 200, human_save.text
    assert human_save.json().get("claim_token")
    assert human_save.json().get("claim_work_id") == WORK_JUDGMENT
    assert human_save.json().get("claim_documents") == [DOC_JUDGMENT]
    assert human_save.headers.get("X-Claim-Token") == human_save.json()["claim_token"]

    # Agent is refused with 403 on save_citation even with status="proposed".
    cit_refused = claude.post(
        f"/api/documents/{DOC_JUDGMENT}/citations",
        json={"status": "proposed", "citation_id": "cit_1"},
    )
    assert cit_refused.status_code == 403
    assert "Agent accounts may not change" in cit_refused.json()["message"]


def test_shared_details_approved_sibling_guard(env):
    store, _, app = env
    owner = _browser(app, "owner@example.org")
    claude = _browser(app, "claude@example.org")

    # The agent holds the work before any of it is approved.
    claimed = claude.post(f"/api/claims/{WORK_LAW}", json={"label": "claude-m1-02", "task": "status"})
    assert claimed.status_code == 200
    token = claimed.json()["token"]

    # The owner reviews and approves DOC_EN over that claim (leaving sibling DOC_FR unapproved).
    _save_all_pages(owner, DOC_EN)
    det_payload = _confirm_details_payload(owner, DOC_EN)
    assert owner.post(f"/api/documents/{DOC_EN}/details", json=det_payload).status_code == 200
    assert _approve_doc(owner, DOC_EN).status_code == 200

    # By name, an agent account is refused a work with an approved document, as "Claim next" would not
    # hand it one: at once, not ten seconds later.
    codex = _browser(app, "codex@example.org")
    named = codex.post(f"/api/claims/{WORK_LAW}", json={"label": "codex-m1-09", "task": "review"})
    assert named.status_code == 403
    assert named.json() == {"code": "approved_guard", "message": claims.MSG_APPROVED}

    # Agent tries to edit shared Details through UNAPPROVED sibling DOC_FR -> 403!
    det_update = _confirm_details_payload(claude, DOC_FR)
    blocked = claude.post(
        f"/api/documents/{DOC_FR}/details",
        json=det_update,
        headers={"X-Claim-Token": token},
    )
    assert blocked.status_code == 403
    assert "approved" in blocked.json()["message"].lower()

    # Agent tries to save_page on approved DOC_EN -> 403!
    p1 = claude.get(f"/api/documents/{DOC_EN}/pages/1").json()
    blocked_page = claude.post(
        f"/api/documents/{DOC_EN}/pages/1",
        json={"expected_revision": 1, "units": p1["latest"]["units"], "status": "checked", "resolutions": {}},
        headers={"X-Claim-Token": token},
    )
    assert blocked_page.status_code == 403

    # Granting may_change_approved allows the save.
    store.add_operator(
        "claude@example.org", "Claude Fleet", {"reviewer"}, agent=True, may_change_approved=True
    )
    claude_allowed = _browser(app, "claude@example.org")
    ok = claude_allowed.post(
        f"/api/documents/{DOC_FR}/details",
        json=det_update,
        headers={"X-Claim-Token": token},
    )
    assert ok.status_code == 200, ok.text


def test_audit_draw_submission_isolation_and_owner_resolve(env):
    store, _, app = env
    claude = _browser(app, "claude@example.org")
    codex = _browser(app, "codex@example.org")
    owner = _with_passkey(app, store, "owner@example.org")
    owner_no_passkey = _browser(app, "owner@example.org")

    # Claude claims WORK_LAW2, saves all 3 pages, and releases as done -> triggers audit draw.
    claimed = claude.post(f"/api/claims/{WORK_LAW2}", json={"label": "claude-m1-03", "task": "review"})
    assert claimed.status_code == 200
    token = claimed.json()["token"]
    _save_all_pages(claude, DOC_LAW2, headers={"X-Claim-Token": token})
    rel = claude.post(
        f"/api/claims/{WORK_LAW2}/release",
        json={"outcome": "done"},
        headers={"X-Claim-Token": token},
    )
    assert rel.status_code == 200
    # Drawn pages are NOT leaked to the reviewer.
    assert "pages_drawn" not in rel.json()

    # Same account cannot claim audit for its own work.
    same_acc_audit = claude.post("/api/queue/claim-next", json={"label": "claude-m1-99", "task": "audit"})
    assert same_acc_audit.status_code == 404

    # Cross-account auditor (codex) claims the audit task and receives pages_drawn.
    audit_claim = codex.post("/api/queue/claim-next", json={"label": "codex-m1-01", "task": "audit"})
    assert audit_claim.status_code == 200, audit_claim.text
    adata = audit_claim.json()
    assert adata["work_id"] == WORK_LAW2
    assert adata["task"] == "audit"
    audit_token = adata["token"]
    drawn_pages = adata["documents"][0]["pages_drawn"]
    assert len(drawn_pages) >= 2

    # Record live page_revision before audit submission.
    p_first, p_second = drawn_pages[0], drawn_pages[1]
    before_live = store.latest_pages(DOC_LAW2)
    orig_text_first = before_live[p_first]["units"][0]["text"]

    # Auditor submits a MATERIAL edit on p_first and a CLEAN confirmation on p_second.
    edited_units = [{"kind": "paragraph", "text": f"Corrected text on page {p_first} by auditor."}]
    sub1 = codex.post(
        f"/api/documents/{DOC_LAW2}/pages/{p_first}",
        json={"expected_revision": 1, "units": edited_units, "status": "checked", "resolutions": {}},
        headers={"X-Claim-Token": audit_token},
    )
    assert sub1.status_code == 200, sub1.text
    assert sub1.json()["audited"] is True
    assert sub1.json()["verdict"] == "material"
    audit_id_1 = sub1.json()["audit_id"]

    sub2 = codex.post(
        f"/api/documents/{DOC_LAW2}/pages/{p_second}",
        json={
            "expected_revision": 1,
            "units": before_live[p_second]["units"],
            "status": "checked",
            "resolutions": {},
        },
        headers={"X-Claim-Token": audit_token},
    )
    assert sub2.status_code == 200, sub2.text
    assert sub2.json()["verdict"] == "clean"
    audit_id_2 = sub2.json()["audit_id"]

    # CRITICAL: Live page_revision was NOT touched by auditor submissions!
    after_live = store.latest_pages(DOC_LAW2)
    assert after_live[p_first]["revision"] == before_live[p_first]["revision"]
    assert after_live[p_first]["units"][0]["text"] == orig_text_first

    # Auditor cannot edit details under an audit claim.
    det_attempt = codex.post(
        f"/api/documents/{DOC_LAW2}/details",
        json=_confirm_details_payload(codex, DOC_LAW2),
        headers={"X-Claim-Token": audit_token},
    )
    assert det_attempt.status_code == 403

    # Admin report exposes open material audit diffs, audit_windows, page_counts, pace, claims.
    report = owner.get("/api/agents/report").json()
    assert any(a["id"] == audit_id_1 for a in report["open_material_audits"])
    assert any(w["label"] == "claude-m1-03" and w["material"] == 1 for w in report["audit_windows"])
    assert any(c["label"] == "claude-m1-03" and c["pages_saved"] == 3 for c in report["page_counts"])
    assert any(p["label"] == "claude-m1-03" for p in report["pace"])

    # Resolve requires fresh passkey.
    no_pk = owner_no_passkey.post(f"/api/audits/{audit_id_1}/resolve", json={"decision": "accepted_auditor"})
    assert no_pk.status_code == 403
    assert no_pk.json()["code"] == "passkey_required"

    # Owner accepts auditor on audit_id_1 -> writes new live page_revision under owner's account!
    resolved1 = owner.post(
        f"/api/audits/{audit_id_1}/resolve",
        json={"decision": "accepted_auditor", "comment": "Auditor caught error"},
    )
    assert resolved1.status_code == 200, resolved1.text
    assert resolved1.json()["new_revision"] == 2
    live_after_accept = store.latest_pages(DOC_LAW2)
    assert live_after_accept[p_first]["author"] == "owner@example.org"
    assert live_after_accept[p_first]["units"][0]["text"] == f"Corrected text on page {p_first} by auditor."

    # Owner keeps reviewer on audit_id_2 -> live page_revision unchanged.
    resolved2 = owner.post(f"/api/audits/{audit_id_2}/resolve", json={"decision": "kept_reviewer"})
    assert resolved2.status_code == 200
    assert resolved2.json()["new_revision"] is None


def test_status_claim_next_and_status_gate(env, monkeypatch):
    monkeypatch.setenv("ALVARY_REVIEW_STATUS_GATE", "on")
    _, _, app = env
    owner = _browser(app, "owner@example.org")
    claude = _browser(app, "claude@example.org")

    # Confirm law details with status.legal = unknown and empty status.note (pending status research).
    _save_all_pages(owner, DOC_LAW2)
    det_law = _confirm_details_payload(owner, DOC_LAW2, **{"status.legal": "unknown"})
    det_law["details"].pop("status.note", None)
    assert owner.post(f"/api/documents/{DOC_LAW2}/details", json=det_law).status_code == 200
    owner.post(f"/api/claims/{WORK_LAW2}/release", json={"outcome": "done"})

    # Confirm judgment details (never gated, never offered for task="status").
    _save_all_pages(owner, DOC_JUDGMENT)
    det_judg = _confirm_details_payload(
        owner,
        DOC_JUDGMENT,
        **{"profile.court.name": "Supreme Court of Nigeria", "profile.court.level": "apex"},
    )
    res_judg = owner.post(f"/api/documents/{DOC_JUDGMENT}/details", json=det_judg)
    assert res_judg.status_code == 200, res_judg.text
    owner.post(f"/api/claims/{WORK_JUDGMENT}/release", json={"outcome": "done"})

    # Approving the law fails 409 with "Legal status: research pending." and blind readiness.
    blocked_app = _approve_doc(owner, DOC_LAW2)
    assert blocked_app.status_code == 409
    assert blocked_app.json()["message"] == "Legal status: research pending."
    assert "routing" not in blocked_app.json()["readiness"]
    assert blocked_app.json()["readiness"]["status_pending"] is True

    # Approving the judgment succeeds even with status gate ON.
    assert _approve_doc(owner, DOC_JUDGMENT).status_code == 200

    # Status agent calls claim-next with task="status" -> gets WORK_LAW2.
    st_claim = claude.post("/api/queue/claim-next", json={"label": "status-claude-01", "task": "status"})
    assert st_claim.status_code == 200, st_claim.text
    assert st_claim.json()["work_id"] == WORK_LAW2
    st_token = st_claim.json()["token"]

    # Status agent settles legal status and releases.
    settled = _confirm_details_payload(
        claude, DOC_LAW2, **{"status.legal": "in_force", "status.note": "Checked Official Gazette"}
    )
    assert (
        claude.post(
            f"/api/documents/{DOC_LAW2}/details",
            json=settled,
            headers={"X-Claim-Token": st_token},
        ).status_code
        == 200
    )
    claude.post(
        f"/api/claims/{WORK_LAW2}/release",
        json={"outcome": "done"},
        headers={"X-Claim-Token": st_token},
    )

    # Now owner can approve DOC_LAW2!
    assert _approve_doc(owner, DOC_LAW2).status_code == 200


def test_block_unblock_label_and_pause_all_agents(env):
    store, _, app = env
    owner = _with_passkey(app, store, "owner@example.org")
    owner_no_pk = _browser(app, "owner@example.org")
    claude = _browser(app, "claude@example.org")
    human = _browser(app, "human@example.org")

    # Claim work before blocking.
    claimed = claude.post(f"/api/claims/{WORK_LAW2}", json={"label": "claude-m1-07", "task": "review"})
    assert claimed.status_code == 200
    token = claimed.json()["token"]

    # Block label requires passkey.
    assert (
        owner_no_pk.post(
            "/api/labels/block", json={"account": "claude@example.org", "label": "claude-m1-07"}
        ).status_code
        == 403
    )

    # Block label with passkey.
    blk = owner.post(
        "/api/labels/block",
        json={"account": "claude@example.org", "label": "claude-m1-07", "reason": "too fast"},
    )
    assert blk.status_code == 200
    assert blk.json()["label"] == "claude-m1-07"

    # Renew and save are refused with 403 while blocked.
    assert claude.post(f"/api/claims/{WORK_LAW2}/renew", headers={"X-Claim-Token": token}).status_code == 403
    p1 = claude.get(f"/api/documents/{DOC_LAW2}/pages/1").json()
    save_blk = claude.post(
        f"/api/documents/{DOC_LAW2}/pages/1",
        json={"expected_revision": 0, "units": p1["machine_units"], "status": "checked", "resolutions": {}},
        headers={"X-Claim-Token": token},
    )
    assert save_blk.status_code == 403
    assert "paused by the owner" in save_blk.json()["message"]

    # Unblock restores renew & save.
    unblk = owner.post("/api/labels/unblock", json={"account": "claude@example.org", "label": "claude-m1-07"})
    assert unblk.status_code == 200 and unblk.json()["unblocked"] is True
    assert claude.post(f"/api/claims/{WORK_LAW2}/renew", headers={"X-Claim-Token": token}).status_code == 200

    # Pause-all requires passkey, deactivates all agent accounts & ends their sessions, keeps humans active.
    assert owner_no_pk.post("/api/agents/pause-all", json={}).status_code == 403
    paused = owner.post("/api/agents/pause-all", json={})
    assert paused.status_code == 200
    assert set(paused.json()["paused"]) == {"claude@example.org", "codex@example.org"}
    assert claude.get("/api/me").status_code == 401
    assert human.get("/api/me").status_code == 200
