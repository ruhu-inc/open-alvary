"""CPR-06: one work queue lists, across documents, the pages to review, the flags to resolve and the
proposals awaiting review, each with its document, kind, priority and age; audit pages are listed as
pages to review like any other."""

from datetime import UTC, datetime, timedelta

from test_routing import flagged_world as flagged_world  # noqa: F401 - a fixture
from test_routing import keys_in, readiness
from test_workbench import ACT, flag_id, save, signed_in
from test_workbench import world as world  # noqa: F401 - a fixture

from workbench import content, queue


def tasks(browser, **query):
    return browser.get("/api/queue", params=query).json()


def test_pages_with_high_flags_come_first_and_each_task_says_how_long_it_has_waited(world):  # noqa: F811
    reviewer = signed_in(world, "reviewer@example.org")
    found = tasks(reviewer)
    assert [(t["kind"], t["priority"], t["pages"]) for t in found["tasks"]] == [
        ("review", "high", [1]),
        ("review", "full", [2]),
    ]
    assert not found["tasks"][0]["started"] and found["tasks"][0]["since"] is None  # nobody has opened it
    assert found["tasks"][0]["id"] == ACT and found["tasks"][0]["title"].startswith("Cybercrimes")
    save(reviewer, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    [high] = tasks(reviewer)["tasks"]
    assert high["started"] and high["pages"] == [1] and high["since"] and 0 <= high["age_days"] < 0.01
    save(reviewer, page=1)  # checked, but its high flag is still open
    [left] = tasks(reviewer)["tasks"]
    assert (left["kind"], left["priority"], left["pages"]) == ("flags", "high", [1])
    save(reviewer, page=1, expected=1, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    assert tasks(reviewer) == {"total": 0, "offset": 0, "limit": 100, "counts": dict.fromkeys(
        ("review", "flags", "events", "instructions", "citations"), 0), "tasks": []}  # fmt: skip


def test_proposals_awaiting_review_or_a_second_review_are_listed_after_pages(world):  # noqa: F811
    reviewer = signed_in(world, "reviewer@example.org")
    for day in ("2024-06-12", "2024-07-01"):
        event = {"type": "commenced", "date_from": day}
        body = {"expected_revision": 0, "status": "proposed", "event": event}
        assert reviewer.post(f"/api/documents/{ACT}/events", json=body).status_code == 200
    instruction = {
        "operation": "repeal", "target": "sec_5", "effective_date": "2024-06-12",
        "amending": {"locator": "sec_1", "citation": "Cybercrimes (Amendment) Act, 2024, s. 1"},
    }  # fmt: skip
    body = {"expected_revision": 0, "status": "reviewed", "target_work": "ng-cybercrimes-act-2015"}
    body["instruction"] = instruction
    assert reviewer.post(f"/api/documents/{ACT}/instructions", json=body).status_code == 200
    found = tasks(reviewer)
    assert found["counts"] == {"review": 2, "flags": 0, "events": 1, "instructions": 1, "citations": 0}
    assert [t["kind"] for t in found["tasks"]] == ["review", "review", "events", "instructions"]
    events, instructions = found["tasks"][2:]
    assert (events["priority"], events["step"], events["count"]) == ("proposal", "review", 2)
    assert events["id"] == ACT
    assert (instructions["step"], instructions["count"]) == ("second_review", 1)
    assert [t["kind"] for t in tasks(reviewer, kind="events")["tasks"]] == ["events"]
    assert tasks(reviewer, limit=1, offset=1)["tasks"][0]["priority"] == "full"


def test_audit_pages_are_queued_as_pages_to_review_with_the_full_ones(flagged_world):  # noqa: F811
    store, library, found = flagged_world
    approver = signed_in((store, library), "approver@example.org")
    sampled = readiness(approver)["routing"]["audit_sample"]
    reviewer = signed_in((store, library), "reviewer@example.org")
    listed = tasks(reviewer)
    assert [(t["kind"], t["priority"], t["pages"]) for t in listed["tasks"]] == [
        ("review", "full", sorted([1, *sampled])),  # one task: the sample is not told apart
        ("flags", "flags", [2]),
    ]
    assert not {"routing", "audit_sample", "routes", "reasons"} & keys_in(listed)
    resolved = {f["id"]: {"decision": "keep"} for f in content.page_flags(found["pages"][1])}
    save(reviewer, page=2, units=content.units_from(found["pages"][1]), status="edited", resolutions=resolved)
    assert [t["kind"] for t in tasks(reviewer)["tasks"]] == ["review"]


def test_work_already_started_comes_before_documents_nobody_has_opened():
    now = datetime(2026, 10, 19, tzinfo=UTC)
    started = queue.task({"id": "ng-a", "title": "A"}, "flags", "flags", 3, now - timedelta(days=2))
    older = queue.task({"id": "ng-b", "title": "B"}, "flags", "flags", 1, now - timedelta(days=5))
    untouched = queue.not_started({"id": "ng-c", "title": "C", "page_count": 40, "flag_counts": {"high": 2}})
    assert (untouched["kind"], untouched["priority"], untouched["count"]) == ("review", "high", 40)
    assert [t["id"] for t in queue.ordered([untouched, started, older])] == ["ng-b", "ng-a", "ng-c"]
    assert queue.shown(older, now)["age_days"] == 5.0 and queue.shown(untouched, now)["age_days"] is None


def test_documents_nobody_has_opened_come_by_importance_with_a_works_languages_together():
    def doc(id, kind, work, currency=None, year=None, high=0):
        entry = {
            "id": id,
            "title": id,
            "page_count": 2,
            "type": kind,
            "work": work,
            "flag_counts": {"high": high},
        }
        return queue.not_started(
            entry | {"currency": {"status": currency} if currency else None, "year": year}
        )

    tasks = [
        doc("rw-ministerial-order-1-en", "ministerial_order", "mo-1", high=3),  # high flags, still after laws
        doc("rw-law-old-fr", "law", "law-old", "repealed", 1999),
        doc("rw-law-12-fr", "law", "law-12", "in_force", 2018),
        doc("rw-law-12-en", "law", "law-12", "in_force", 2018),
        doc("rw-organic-law-1-rw", "organic_law", "ol-1", None, 2013),
        doc("rw-law-20-en", "law", "law-20", "in_force", 2021),
        doc("rw-other-1-fr", "other", "other-1"),
        doc("rw-law-unknown-en", "law", "law-unknown", "unknown", 2020),
    ]
    started = queue.task({"id": "ng-a", "title": "A"}, "flags", "flags", 1, datetime(2026, 10, 1, tzinfo=UTC))
    assert [t["id"] for t in queue.ordered([*tasks, started])] == [
        "ng-a",  # work already started
        "rw-organic-law-1-rw",
        "rw-law-20-en",  # in force, newest first
        "rw-law-12-en",  # a work's language versions together
        "rw-law-12-fr",
        "rw-law-unknown-en",  # currency unknown
        "rw-law-old-fr",  # repealed
        "rw-ministerial-order-1-en",
        "rw-other-1-fr",  # a kind not listed
    ]
