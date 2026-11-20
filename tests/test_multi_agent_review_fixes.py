"""What the independent review of the multi-agent change (2026-11-19) found on the server, each as a test:
refusals the page can tell apart, the switch really being off, a person never locked out of their own work,
"warn" not borrowing the holder's label, labels in one case, the audit's comparison, pass mark and limits,
strict flags and settings, and a start-up that issues nothing an up-to-date database does not need."""

from __future__ import annotations

import unicodedata
from datetime import timedelta

import pytest
import test_multi_agent_e2e as e2e
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text, update
from test_multi_agent_e2e import (
    DOC_EN,
    DOC_LAW2,
    WORK_LAW,
    WORK_LAW2,
    _browser,
    _save_all_pages,
    _with_passkey,
)

from workbench import audit, claims
from workbench import store as workbench_store
from workbench.store import Conflict, connect, now, page_revision, review_audit, work_claim
from workbench.store import audit as audit_event

env = e2e.env  # the end-to-end tests' workbench: four documents, a person, an owner and two agent accounts


def _page_body(client: TestClient, act_id: str, page: int, expected: int | None = None) -> dict:
    found = client.get(f"/api/documents/{act_id}/pages/{page}").json()
    latest = found["latest"] or {}
    return {
        "expected_revision": latest.get("revision", 0) if expected is None else expected,
        "units": latest.get("units") or found["machine_units"],
        "status": "edited",
        "resolutions": {},
    }


def _save(client: TestClient, act_id: str, page: int, token: str | None = None, expected: int | None = None):
    return client.post(
        f"/api/documents/{act_id}/pages/{page}",
        json=_page_body(client, act_id, page, expected),
        headers={"X-Claim-Token": token} if token else {},
    )


def _claim(client: TestClient, work_id: str, label: str, task: str = "review"):
    return client.post(f"/api/claims/{work_id}", json={"label": label, "task": task})


# --- what the page is told ------------------------------------------------------------------------


def test_a_claim_refusal_carries_a_code_and_a_page_conflict_does_not(env):
    _, _, app = env
    claude, codex, human = (_browser(app, f"{name}@example.org") for name in ("claude", "codex", "human"))
    token = _claim(claude, WORK_LAW2, "claude-m1-01").json()["token"]

    no_claim = _save(codex, DOC_EN, 1)  # an agent account, on a work nobody holds
    assert no_claim.status_code == 409
    assert no_claim.json() == {"code": "claim_required", "message": claims.MSG_AGENT_NEEDS_CLAIM}

    held = _save(human, DOC_LAW2, 1)  # a person, on a work an agent holds
    assert held.status_code == 409 and held.json()["code"] == "claim_held"
    assert held.json()["message"].startswith(
        "This work is being reviewed by claude-m1-01 (claude@example.org)"
    )

    assert _save(claude, DOC_LAW2, 1, token).status_code == 200
    stale = _save(claude, DOC_LAW2, 1, token, expected=0)  # someone saved first: no code, as before
    assert stale.status_code == 409 and "code" not in stale.json()


def test_with_claims_off_nothing_is_claimed_and_the_page_is_told(env, monkeypatch):
    store, _, app = env
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "off")
    claude, human, owner = (_browser(app, f"{name}@example.org") for name in ("claude", "human", "owner"))

    assert claude.get("/api/me").json()["claims"] == "off"
    assert claude.get("/api/me").json()["agent"] is True and human.get("/api/me").json()["agent"] is False
    off = {"code": "claims_off", "message": "Claims are switched off."}
    next_work = claude.post("/api/queue/claim-next", json={"label": "claude-m1-01"})
    assert (next_work.status_code, next_work.json()) == (409, off)
    named = _claim(claude, WORK_LAW2, "claude-m1-01")
    assert (named.status_code, named.json()) == (409, off)
    renewed = claude.post(f"/api/claims/{WORK_LAW2}/renew", json={"token": "anything"})
    assert (renewed.status_code, renewed.json()) == (409, off)

    saved = _save(human, DOC_LAW2, 1)  # a person's save is as it was before claims existed
    assert saved.status_code == 200 and saved.json() == {"revision": 1}
    assert human.get(f"/api/documents/{DOC_LAW2}").json()["claim"] is None
    with store.engine.connect() as db:
        assert db.execute(select(work_claim)).first() is None

    # The Agents screen is for an administrator, and only once there is something on it.
    assert human.get("/api/me").json()["agents"] is False
    assert owner.get("/api/me").json()["agents"] is True  # agent accounts exist
    store.pause_all_agents()
    assert owner.get("/api/me").json()["agents"] is False
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "warn")
    assert owner.get("/api/me").json()["agents"] is True and owner.get("/api/me").json()["claims"] == "warn"


# --- a person and their own claim -----------------------------------------------------------------


def test_a_person_carries_on_their_own_claim_from_another_browser(env):
    store, _, app = env
    first, second = _browser(app, "human@example.org"), _browser(app, "human@example.org")

    one = _save(first, DOC_LAW2, 1)
    assert one.status_code == 200
    assert one.json()["claim_label"] == "unlabelled" and one.json()["claim_work_id"] == WORK_LAW2
    token = one.json()["claim_token"]

    two = _save(second, DOC_LAW2, 2)  # another browser, or storage cleared: no token, still theirs
    assert two.status_code == 200, two.text
    assert two.json()["claim_token"] not in (None, token)

    three = _save(first, DOC_LAW2, 3, token)  # the first browser's token is the old one now: still theirs
    assert three.status_code == 200, three.text
    with store.engine.connect() as db:
        assert [(r.account, r.label) for r in db.execute(select(work_claim))] == [
            ("human@example.org", "unlabelled")
        ]

    # And theirs to give up, from a browser that holds no token.
    third = _browser(app, "human@example.org")
    released = third.post(f"/api/claims/{WORK_LAW2}/release", json={"outcome": "abandoned"})
    assert released.status_code == 200, released.text

    # An agent account is not a person: it never takes over an unlabelled claim this way.
    assert _save(first, DOC_LAW2, 1).status_code == 200
    claude = _browser(app, "claude@example.org")
    assert _save(claude, DOC_LAW2, 2).json()["code"] == "claim_held"


def test_under_warn_a_save_on_anothers_work_does_not_carry_the_holders_label(env, monkeypatch):
    store, _, app = env
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "warn")
    claude, codex = _browser(app, "claude@example.org"), _browser(app, "codex@example.org")
    assert _claim(claude, WORK_LAW2, "claude-m1-07").status_code == 200

    assert _save(codex, DOC_LAW2, 1).status_code == 200  # let through, and logged
    with store.engine.connect() as db:
        saved = db.execute(select(page_revision).where(page_revision.c.act_id == DOC_LAW2)).one()
        would = db.execute(select(audit_event).where(audit_event.c.action == "claim_would_refuse")).one()
    assert (saved.author, saved.label, saved.task) == ("codex@example.org", None, None)
    assert would.detail["holder_label"] == "claude-m1-07" and would.detail["label"] is None


def test_labels_are_one_case_and_a_paused_label_hands_nothing_in(env):
    store, _, app = env
    claude = _browser(app, "claude@example.org")
    token = _claim(claude, WORK_LAW2, "CLAUDE-M1-02").json()["token"]
    with store.engine.connect() as db:
        assert db.execute(select(work_claim.c.label)).scalar() == "claude-m1-02"

    claims.block_label(store, "claude@example.org", "Claude-M1-02", blocked_by="owner@example.org")
    done = claude.post(f"/api/claims/{WORK_LAW2}/release", json={"outcome": "done", "token": token})
    assert done.status_code == 403 and done.json()["code"] == "label_blocked"
    back = claude.post(f"/api/claims/{WORK_LAW2}/release", json={"outcome": "abandoned", "token": token})
    assert back.status_code == 200, back.text
    assert claims.unblock_label(store, "claude@example.org", "CLAUDE-M1-02", actor="owner@example.org")


# --- the audit ------------------------------------------------------------------------------------


def test_an_audit_is_never_the_reviewers_own_and_needs_claims_enforced(env, monkeypatch):
    store, _, app = env
    claude, codex = _browser(app, "claude@example.org"), _browser(app, "codex@example.org")
    token = _claim(claude, WORK_LAW2, "claude-m1-03").json()["token"]
    _save_all_pages(claude, DOC_LAW2, headers={"X-Claim-Token": token})
    released = claude.post(f"/api/claims/{WORK_LAW2}/release", json={"outcome": "done", "token": token})
    assert released.status_code == 200 and released.json()["audit"]["drawn"] >= 2

    own = _claim(claude, WORK_LAW2, "claude-m1-09", "audit")  # the reviewer's account, another label
    assert own.status_code == 404 and own.json()["code"] == "claim_none"
    assert "pages_drawn" not in own.text
    other = _claim(codex, WORK_LAW2, "codex-m1-01", "audit")
    assert other.status_code == 200 and other.json()["documents"][0]["pages_drawn"]
    assert codex.post(f"/api/claims/{WORK_LAW2}/release", json={"outcome": "abandoned"}).status_code == 403
    assert (
        codex.post(
            f"/api/claims/{WORK_LAW2}/release", json={"outcome": "abandoned", "token": other.json()["token"]}
        ).status_code
        == 200
    )

    # Under "warn" a save is not held to its claim, so nothing is audited and nothing is drawn.
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "warn")
    refused = codex.post("/api/queue/claim-next", json={"label": "codex-m1-01", "task": "audit"})
    assert refused.status_code == 409 and refused.json()["code"] == "audit_needs_enforce"
    token = _claim(claude, WORK_LAW, "claude-m1-03").json()["token"]
    released = claude.post(f"/api/claims/{WORK_LAW}/release", json={"outcome": "done", "token": token})
    assert released.json()["audit"] == {"drawn": 0, "skipped": "claims_not_enforced"}
    with store.engine.connect() as db:
        skipped = db.execute(select(audit_event).where(audit_event.c.action == "audit_draw_skipped")).one()
    assert skipped.detail["reason"] == "claims_not_enforced" and skipped.detail["work_id"] == WORK_LAW


def test_audit_diff_counts_whatever_changes_the_published_text():
    def para(words: str, **more) -> dict:
        return {"kind": "paragraph", "text": words, **more}

    base = [para("The Minister may make regulations.", marker="12.")]
    assert audit.audit_diff(base, base)["verdict"] == "clean"
    # A list or heading number is printed text: a changed number.
    assert audit.audit_diff(base, [{**base[0], "marker": "13."}])["verdict"] == "material"
    # Joining to the page before moves the paragraph's boundary (and can remove a hyphen).
    assert audit.audit_diff(base, [{**base[0], "continues_previous": True}])["verdict"] == "material"
    joined = [{**base[0], "continues_previous": True}]
    assert audit.audit_diff(joined, [{**joined[0], "dehyphenate": True}])["verdict"] == "material"
    # How a letter is typed, spacing, alignment, and a heading's rank change no word.
    typed = [para(unicodedata.normalize("NFD", "Itegeko ryo ku wa 03/09/2026 ryemera détaché"))]
    assert (
        audit.audit_diff(typed, [para(unicodedata.normalize("NFC", typed[0]["text"]))])["verdict"] == "minor"
    )
    assert audit.audit_diff(base, [{**base[0], "align": "center"}])["verdict"] == "minor"
    assert (
        audit.audit_diff(base, [para("The Minister may make regulations.", marker="12.")])["verdict"]
        == "minor"
    )
    # A paragraph made a heading changes the published structure; a heading's rank does not.
    assert audit.audit_diff(base, [{**base[0], "kind": "heading", "rank": 2}])["verdict"] == "material"
    heading = [{"kind": "heading", "rank": 2, "text": "CHAPTER ONE"}]
    assert audit.audit_diff(heading, [{**heading[0], "rank": 3}])["verdict"] == "minor"
    # A table's merged cells are its shape.
    table = [{"kind": "table", "caption": "", "header_rows": 1, "rows": [["a", "b"], ["c", "d"]]}]
    assert audit.audit_diff(table, [{**table[0], "spans": [[0, 0, 1, 2]]}])["verdict"] == "material"
    assert audit.audit_diff(table, [{**table[0], "rows": [["a", "b"], ["c", "e"]]}])["verdict"] == "material"


def test_the_pass_mark_is_more_than_two_material_pages_in_the_last_hundred():
    def run(total: int, material: int) -> str:
        return audit.evaluate_window(["material"] * material + ["clean"] * (total - material))["status"]

    assert run(30, 1) == "pass" and run(60, 2) == "pass" and run(100, 2) == "pass"
    assert run(30, 3) == "breached" and run(100, 3) == "breached"
    assert run(10, 2) == "insufficient_sample" and run(10, 3) == "breached"  # three in the first 25
    # Only the last hundred audited pages count.
    assert audit.evaluate_window(["material"] * 3 + ["clean"] * 100)["status"] == "pass"


def test_a_page_decided_for_the_reviewer_does_not_count_against_them(env):
    store, _, app = env
    owner = _browser(app, "owner@example.org")
    decisions = ["kept_reviewer", "kept_reviewer", "accepted_auditor", None]  # four material pages
    for page, decision in enumerate(decisions, start=1):
        drawn = store.create_audit_draw(
            work_id=WORK_LAW2,
            act_id=DOC_LAW2,
            page=page,
            release_id=1,
            reviewer="claude@example.org",
            reviewer_label="claude-m1-03",
            reviewer_revision=1,
        )
        with store.engine.begin() as db:
            db.execute(
                update(review_audit)
                .where(review_audit.c.id == drawn)
                .values(
                    auditor="codex@example.org",
                    auditor_label="codex-m1-01",
                    audited_at=now(),
                    verdict="material",
                    owner_decision=decision,
                )
            )
    report = owner.get("/api/agents/report").json()
    (window,) = [w for w in report["audit_windows"] if w["label"] == "claude-m1-03"]
    assert window["material"] == 2 and window["total_audited"] == 2  # the two the owner kept are left out
    assert window["material_undecided"] == 1


def test_accepting_an_auditor_is_refused_once_the_page_has_moved_on(env):
    store, _, _ = env
    units = [{"kind": "paragraph", "text": "As reviewed."}]
    values = {
        "reading_sha256": "0" * 64,
        "units": units,
        "status": "checked",
        "resolutions": {},
        "comment": "",
    }
    assert store.save_page(DOC_LAW2, 1, 0, "claude@example.org", **values) == 1
    drawn = store.create_audit_draw(
        work_id=WORK_LAW2,
        act_id=DOC_LAW2,
        page=1,
        release_id=1,
        reviewer="claude@example.org",
        reviewer_label="claude-m1-03",
        reviewer_revision=1,
    )
    store.save_audit_submission(
        drawn,
        auditor="codex@example.org",
        auditor_label="codex-m1-01",
        units=[{"kind": "paragraph", "text": "As audited."}],
        resolutions={},
        status="checked",
        verdict="material",
    )
    assert store.save_page(DOC_LAW2, 1, 1, "human@example.org", **{**values, "comment": "a later edit"}) == 2
    with pytest.raises(Conflict, match="has been changed since this audit was drawn"):
        store.resolve_audit(drawn, decision="accepted_auditor", decided_by="owner@example.org")
    assert store.latest_pages(DOC_LAW2)[1]["revision"] == 2  # the later edit stands
    assert store.resolve_audit(drawn, decision="kept_reviewer", decided_by="owner@example.org")


# --- flags and settings ---------------------------------------------------------------------------


def test_an_operators_agent_flags_must_be_true_or_false(env):
    store, _, app = env
    owner = _with_passkey(app, store, "owner@example.org")
    body = {"email": "human@example.org", "name": "Human", "roles": ["reviewer"]}
    for flags in ({"agent": "no"}, {"may_change_approved": "false"}, {"agent": 1}):
        refused = owner.post("/api/operators", json={**body, **flags})
        assert refused.status_code == 400 and refused.json()["code"] == "invalid_input", refused.text
    assert store.operator("human@example.org")["agent"] is False
    assert owner.post("/api/operators", json={**body, "agent": False}).status_code == 200


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ALVARY_REVIEW_APPROVED_GUARD", "on"),  # read as "off" before: a guard that guarded nothing
        ("ALVARY_REVIEW_CLAIMS", "enforced"),
        ("ALVARY_REVIEW_STATUS_GATE", "enforce"),
        ("ALVARY_REVIEW_AUDIT_RATE", "abc"),
        ("ALVARY_REVIEW_AUDIT_RATE", "2"),
        ("ALVARY_REVIEW_AUDIT_MINIMUM", "-1"),
        ("ALVARY_REVIEW_AGENT_MAX_HOURS", "7d"),
        ("ALVARY_REVIEW_CLAIM_MINUTES", "0"),
    ],
)
def test_a_setting_that_cannot_be_right_stops_the_start(monkeypatch, name, value):
    for known in ("CLAIMS", "APPROVED_GUARD", "STATUS_GATE", "AUDIT_RATE", "AUDIT_MINIMUM", "CLAIM_MINUTES"):
        monkeypatch.delenv(f"ALVARY_REVIEW_{known}", raising=False)
    claims.check_settings()  # nothing set: every default is fine
    monkeypatch.setenv("ALVARY_REVIEW_CLAIMS", "Enforce ")  # case and spaces are not a mistake
    monkeypatch.setenv("ALVARY_REVIEW_AUDIT_RATE", "0")
    claims.check_settings()
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=name):
        claims.check_settings()


# --- the database at start-up ---------------------------------------------------------------------


def test_starting_on_an_up_to_date_database_issues_no_change_to_it(tmp_path):
    engine = connect(f"sqlite:///{tmp_path / 'review.db'}")
    issued: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def keep(conn, cursor, statement, parameters, context, executemany):
        issued.append(" ".join(statement.split()).upper())

    workbench_store._make_schema(engine)
    changes = [s for s in issued if s.startswith(("ALTER", "CREATE", "DROP"))]
    assert changes == []  # on PostgreSQL each would take a lock on its table before looking


def test_the_release_before_can_still_add_an_operator_to_a_database_this_code_made(tmp_path):
    engine = connect(f"sqlite:///{tmp_path / 'review.db'}")
    with engine.begin() as db:  # as the release before writes it: it does not know the two new columns
        db.execute(
            text(
                "INSERT INTO operator (email, name, roles, active, created_at) "
                "VALUES ('new@example.org', 'New', 'reviewer', 1, :now)"
            ),
            {"now": now()},
        )
        row = db.execute(text("SELECT agent, may_change_approved FROM operator")).one()
    assert (bool(row[0]), bool(row[1])) == (False, False)


# --- the three simple fixes (owner, 2026-11-20) ----------------------------------------------------


def _quiet_for(store, work_id: str, minutes: int) -> None:
    """As if the holder last opened or saved something ``minutes`` ago, its claim ending an hour after."""
    then = now() - timedelta(minutes=minutes)
    with store.engine.begin() as db:
        db.execute(
            update(work_claim)
            .where(work_claim.c.work_id == work_id)
            .values(active_at=then, renewed_at=then, expires_at=then + timedelta(minutes=60))
        )


def test_the_heartbeat_only_checks_and_real_work_keeps_the_claim(env):
    store, _, app = env
    claude = _browser(app, "claude@example.org")
    token = _claim(claude, WORK_LAW2, "claude-m1-04").json()["token"]
    _quiet_for(store, WORK_LAW2, 30)
    with store.engine.connect() as db:
        before = db.execute(select(work_claim.c.expires_at)).scalar()

    beat = claude.post(f"/api/claims/{WORK_LAW2}/renew", headers={"X-Claim-Token": token})
    assert beat.status_code == 200
    with store.engine.connect() as db:
        assert db.execute(select(work_claim.c.expires_at)).scalar() == before  # the heartbeat extends nothing

    # Opening a page is real work: the claim now runs an hour from this moment.
    assert (
        claude.get(f"/api/documents/{DOC_LAW2}/pages/1", headers={"X-Claim-Token": token}).status_code == 200
    )
    with store.engine.connect() as db:
        after = db.execute(select(work_claim.c.expires_at)).scalar()
    assert after > before and abs((after - now().replace(tzinfo=None)).total_seconds() - 3600) < 60


def test_a_tab_left_open_keeps_the_work_an_hour_at_most_and_is_told_so(env):
    store, _, app = env
    claude = _browser(app, "claude@example.org")
    token = _claim(claude, WORK_LAW2, "claude-m1-05").json()["token"]

    _quiet_for(store, WORK_LAW2, 61)  # opened or saved nothing for an hour; the claim lapsed a minute ago
    gone = claude.post(f"/api/claims/{WORK_LAW2}/renew", headers={"X-Claim-Token": token})
    assert gone.status_code == 409 and gone.json()["code"] == "claim_idle"
    assert "has gone back to the queue" in gone.json()["message"]

    codex = _browser(app, "codex@example.org")  # so another agent can have it
    assert _claim(codex, WORK_LAW2, "codex-m1-05").status_code == 200

    # The first agent coming back asks to take it up again, and is told who has it now.
    again = _claim(claude, WORK_LAW2, "claude-m1-05")
    assert again.status_code == 409 and again.json()["code"] == "claim_held"


def test_quiet_for_the_hour_while_still_held_says_when_it_goes_back_and_can_be_kept(env):
    store, _, app = env
    claude = _browser(app, "claude@example.org")
    token = _claim(claude, WORK_LAW2, "claude-m1-06").json()["token"]
    with store.engine.begin() as db:  # quiet an hour, though something kept the claim a few minutes more
        db.execute(
            update(work_claim)
            .where(work_claim.c.work_id == WORK_LAW2)
            .values(active_at=now() - timedelta(minutes=61), expires_at=now() + timedelta(minutes=4))
        )
    idle = claude.post(f"/api/claims/{WORK_LAW2}/renew", headers={"X-Claim-Token": token})
    assert idle.status_code == 409 and idle.json()["code"] == "claim_idle"
    assert "goes back to the queue at" in idle.json()["message"] and " UTC" in idle.json()["message"]

    kept = _claim(claude, WORK_LAW2, "claude-m1-06")  # "Take it up again"
    assert kept.status_code == 200 and kept.json()["rotated"] is True
    beat = claude.post(f"/api/claims/{WORK_LAW2}/renew", headers={"X-Claim-Token": kept.json()["token"]})
    assert beat.status_code == 200


def test_each_save_keeps_its_word_count_and_the_report_reads_counts_not_text(env):
    store, _, app = env
    human, owner = _browser(app, "human@example.org"), _browser(app, "owner@example.org")
    assert _save(human, DOC_LAW2, 1).status_code == 200
    with store.engine.connect() as db:
        words = db.execute(select(page_revision.c.words)).scalar()
    assert words == len(
        "Article 1. English Law 2 provision text on page 1 with enough words to check.".split()
    )
    assert (
        workbench_store.unit_word_count(
            [{"kind": "table", "caption": "Rates", "rows": [["VAT", "18 %"], ["", None]]}]
        )
        == 4
    )

    report = owner.get("/api/agents/report").json()
    (row,) = [r for r in report["pace"] if r["author"] == "human@example.org"]
    assert row["pages_saved"] == 1 and row["intervals"] == []  # only fast full pages, ten at most
    statement: list[str] = []

    @event.listens_for(store.engine, "before_cursor_execute")
    def keep(conn, cursor, text_, parameters, context, executemany):
        statement.append(text_)

    assert owner.get("/api/agents/report?days=30").status_code == 200  # a week at most is read
    reads = [s for s in statement if "FROM page_revision" in s and "page_revision.created_at >=" in s]
    assert reads and all("page_revision.units" not in s for s in reads)


def test_the_workbench_error_alert_is_in_terraform():
    from pathlib import Path

    tf = (Path(__file__).resolve().parents[1] / "deployment/gcp-terraform/env/monitoring.tf").read_text()
    block = tf[tf.index('resource "google_monitoring_alert_policy" "workbench_errors"') :]
    block = block[: block.index("\n}\n")]
    assert "count        = local.workbench_enabled ? 1 : 0" in block
    assert 'metric.labels.response_code_class = \\"5xx\\"' in block
    assert "threshold_value = 9" in block and 'alignment_period     = "600s"' in block
    assert "google_monitoring_notification_channel.operator.id" in block
