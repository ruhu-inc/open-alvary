"""Publishing in the workbench: approvers and administrators see where each approved document stands in
public view and what the next run will do; an administrator asks for a run now, after a passkey check;
the workbench only asks for the run and starts the publish job, never writing the public catalogue."""

from datetime import UTC, datetime, timedelta

import pytest
from test_passkeys import ORIGIN
from test_workbench_operators import browser, with_passkey

from workbench import content, publishing
from workbench.app import create_app
from workbench.passkeys import RelyingParty
from workbench.store import Store, connect

JOB = "projects/p/locations/europe-west1/jobs/open-alvary-staging-publish"


@pytest.fixture
def bench(tmp_path):
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("boss@example.org", "Boss", {"reviewer", "approver", "admin"})
    store.add_operator("app@example.org", "App", {"reviewer", "approver"})
    store.add_operator("rev@example.org", "Rev", {"reviewer"})
    app = create_app(store, content.Library(tmp_path), dev_login=True, relying=RelyingParty(ORIGIN))
    return store, app


def test_approvers_see_publishing_and_only_an_administrator_with_a_passkey_starts_a_run(bench, monkeypatch):
    store, app = bench
    reviewer = browser(app, "rev@example.org")
    assert reviewer.get("/api/me").json()["publishing"] is False
    assert reviewer.get("/api/publishing").status_code == 403
    approver = browser(app, "app@example.org")
    seen = approver.get("/api/publishing").json()
    assert seen["configured"] is False and seen["can_run"] is False and seen["runs"] == []
    assert "every night" in seen["schedule"]
    assert approver.post("/api/publishing/run").status_code == 403  # an approver may not start one

    started = []
    monkeypatch.setattr(publishing, "start_job", started.append)
    boss = browser(app, "boss@example.org")
    off = boss.post("/api/publishing/run")
    assert off.status_code == 403 and off.json()["code"] == "passkey_required"
    boss = with_passkey(app, store, "boss@example.org")
    unset = boss.post("/api/publishing/run")
    assert unset.status_code == 503 and unset.json()["code"] == "publishing_off"
    monkeypatch.setenv("ALVARY_PUBLISH_JOB", JOB)
    assert boss.get("/api/publishing").json()["can_run"] is True
    asked = boss.post("/api/publishing/run")
    assert asked.status_code == 200 and started == [JOB]
    waiting = boss.get("/api/publishing").json()["waiting"]
    assert waiting["status"] == "requested" and waiting["requested_by_name"] == "Boss"
    again = boss.post("/api/publishing/run")
    assert again.status_code == 409 and again.json()["code"] == "run_waiting" and started == [JOB]
    assert "publication_requested" in {e["action"] for e in store.events()}


def test_a_run_that_cannot_be_started_stays_asked_for_the_nightly_run(bench, monkeypatch):
    store, app = bench

    def refused(name):
        raise RuntimeError("Cloud Run refused to start the publish job (403)")

    monkeypatch.setattr(publishing, "start_job", refused)
    monkeypatch.setenv("ALVARY_PUBLISH_JOB", JOB)
    boss = with_passkey(app, store, "boss@example.org")
    failed = boss.post("/api/publishing/run")
    assert failed.status_code == 502 and "next nightly run takes it up" in failed.json()["message"]
    assert [r["status"] for r in store.publication_runs()] == ["requested"]


DECIDED = {
    "catalogue": {"until": "2027-10-18", "by": "Owner"},
    "text": {"refused": "No text decision for rw."},
}
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def state(approved=True, at=NOW, family="legislation"):
    return {"approved": approved, "head": {"created_at": at}, "view": {"family": family}}


def row(details="public", text="no_decision", updated=NOW + timedelta(hours=1), **more):
    return {
        "details": details,
        "text": text,
        "details_since": NOW,
        "text_since": None,
        "updated_at": updated,
        **more,
    }


def test_where_a_document_stands_is_said_in_words():
    act = {"id": "rw-law-1", "jurisdiction": "rw"}
    waiting = publishing.standing(act, state(), None, DECIDED)
    assert waiting["state"] == "next_run" and "goes public at the next run" in waiting["words"]
    assert waiting["text"] == "Text not published: No text decision for rw."
    public = publishing.standing(act, state(), row(), DECIDED)
    assert public["state"] == "public" and public["words"] == "Public since 2026-10-26."
    # Approved again after the latest run: the change goes public at the next one.
    changed = publishing.standing(act, state(at=NOW + timedelta(hours=2)), row(), DECIDED)
    assert changed["state"] == "next_run"
    lapsed = publishing.standing(act, state(approved=False), row(), DECIDED)
    assert lapsed["state"] == "taken_down_next" and "takes it down" in lapsed["words"]
    assert publishing.standing(act, state(approved=False), None, DECIDED)["state"] == "not_approved"
    blocked = publishing.standing(
        act, state(), None, {**DECIDED, "catalogue": {"refused": "No catalogue decision."}}
    )
    assert blocked["state"] == "blocked" and "No catalogue decision." in blocked["words"]
    held = publishing.standing({**act, "text_held": {"note": "masked"}}, state(), row(), DECIDED)
    assert held["text"].startswith("Text held")
    texts = {**DECIDED, "text": {"until": "2027-10-19", "by": "Owner", "types": ["act", "constitution"]}}
    judgment = publishing.standing(act, state(family="judgment"), row(), texts)
    assert judgment["text"] == "Text not published: the text decision covers act, constitution only."
    published = publishing.standing(act, state(), row(text="public", text_since=NOW), texts)
    assert published["text"] == "Text public since 2026-10-26."
    assert publishing.short("approved", row(), DECIDED) == "public"
    assert publishing.short("approved", None, DECIDED) == "next_run"
    assert publishing.short("out_of_date", row(), DECIDED) == "taken_down_next"
    assert publishing.short(None, None, DECIDED) is None
