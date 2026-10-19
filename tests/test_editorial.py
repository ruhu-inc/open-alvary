"""CPR-02 and CPR-03 in the workbench: legal events are proposed from a document's text, edited, and
reviewed by an approver, and the reviewed ones export as an events file to publish; an amending
document's consolidation instructions are reviewed by one editor and confirmed by a second, and only
then export for the consolidation applier."""

import json
from datetime import date

import pytest
from test_workbench import ACT, client, node, signed_in

from operations import events
from workbench import consolidation, content, publication
from workbench.store import Store, connect

TARGET = "ng-cybercrimes-act-2015"
SOURCE = "https://placng.org/i/wp-content/uploads/2024/05/Cybercrimes-Prohibition-Prevention-etc-Amendment-Act-2024.pdf"


def reading():
    texts = [
        ("1. Section 3 of the principal Act is amended by substituting “two percent” for “one percent”.", "Amendment"),
        ("2. This Act shall come into force on the 12th day of June, 2024.", "Commencement"),
        ("3. The Old Loans Act is hereby repealed.", "Repeal"),
    ]  # fmt: skip
    nodes = [node(text, notes=[note]) for text, note in texts]
    page = {"page": 1, "nodes": nodes, "text": "\n".join(t for t, _ in texts), "tables": [], "furniture": [],
            "flags": [], "checks": {"mineru": "", "azure": ""}}  # fmt: skip
    return {
        "input_sha256": "0" * 64,
        "primary": {"reader": "docai", "version": {"version": "test"}},
        "checks": {"mineru": {}, "azure": {}},
        "summary": {"pages": 1, "flags": {}, "restored_markers": 0, "missing_checks": []},
        "pages": [page],
    }


@pytest.fixture
def world(tmp_path):
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    store.add_operator("approver@example.org", "App", {"reviewer", "approver"})
    store.add_operator("second@example.org", "Second", {"reviewer", "approver"})
    return store, content.Library(tmp_path)


def listed(browser):
    return browser.get(f"/api/documents/{ACT}/events").json()


def save_event(browser, event, status="proposed", expected=0, event_id=None):
    body = {"expected_revision": expected, "status": status, "event": event}
    if event_id:
        body["event_id"] = event_id
    return browser.post(f"/api/documents/{ACT}/events", json=body)


# --- legal events ----------------------------------------------------------------------------------


def test_proposals_are_read_from_a_text_and_its_nodes_without_a_database():
    pages = [{"page": 1, "units": content.units_from(reading()["pages"][0])}]
    text = publication.build({"id": ACT, "jurisdiction": "ng"}, pages)
    found = events.propose_from(text.serialized.text.encode(), text.structure.canonical(), ACT, SOURCE)
    commenced = next(p for p in found if p["type"] == "commenced")
    assert commenced["date_from"] == "2024-06-12" and commenced["evidence"]["locator"] == "sec_2"
    assert commenced["evidence"]["url"] == SOURCE and commenced["affected_work"] == ACT
    repealed = next(p for p in found if p["type"] == "repealed")
    assert repealed["affected_title"] == "Old Loans Act" and repealed["asserting_work"] == ACT


def test_a_commencement_on_a_day_that_does_not_exist_is_proposed_without_a_date():
    words = b"5. This Act shall come into force on the 31st day of June, 2024."
    nodes = [{"kind": "section", "locator": "sec_5", "start_byte": 0, "end_byte": len(words)}]
    [found] = events.propose_from(words, nodes, ACT, SOURCE)
    assert found["date_from"] is None and found["precision"] == "unknown"
    assert "does not exist" in found["conditions"]


def test_a_reviewer_records_the_texts_proposals_once_each(world):
    reviewer = signed_in(world, "reviewer@example.org")
    added = reviewer.post(f"/api/documents/{ACT}/events/proposals").json()["added"]
    assert len(added) == 2
    assert reviewer.post(f"/api/documents/{ACT}/events/proposals").json() == {"added": []}
    shown = listed(reviewer)
    assert shown["work"] == ACT and shown["source_url"] == SOURCE
    assert [p["locator"] for p in shown["provisions"]] == ["sec_1", "sec_2", "sec_3"]
    by_type = {e["event"]["type"]: e for e in shown["events"]}
    commenced = by_type["commenced"]
    assert commenced["status"] == "proposed" and commenced["event"]["date_from"] == "2024-06-12"
    repeal = by_type["repealed"]
    assert repeal["event"]["affected_work"] is None and "Old Loans Act" in repeal["comment"]
    assert shown["names"] == {"reviewer@example.org": "Rev"}


def test_an_approver_reviews_an_event_and_the_reviewed_events_export_as_an_events_file(world):
    reviewer = signed_in(world, "reviewer@example.org")
    approver = signed_in(world, "approver@example.org")
    reviewer.post(f"/api/documents/{ACT}/events/proposals")
    by_type = {e["event"]["type"]: e for e in listed(reviewer)["events"]}
    commenced, repeal = by_type["commenced"], by_type["repealed"]
    key = commenced["event_id"]
    partial = {**commenced["event"], "scope": "explicit_nodes", "locators": ["sec_1"]}
    assert save_event(reviewer, partial, expected=1, event_id=key).json() == {"event_id": key, "revision": 2}
    refused = save_event(reviewer, partial, "reviewed", expected=2, event_id=key)
    assert refused.status_code == 403 and "approver" in refused.json()["message"]
    assert save_event(approver, partial, "reviewed", expected=1, event_id=key).status_code == 409  # stale
    assert save_event(approver, partial, "reviewed", expected=2, event_id=key).status_code == 200

    unnamed = save_event(approver, repeal["event"], "reviewed", expected=1, event_id=repeal["event_id"])
    assert unnamed.status_code == 422 and "affected_work" in unnamed.json()["message"]  # it names its law
    rejected = save_event(approver, {}, "rejected", expected=1, event_id=repeal["event_id"])
    assert rejected.json()["revision"] == 2

    exported = approver.get(f"/api/documents/{ACT}/events/export")
    assert ACT in exported.headers["content-disposition"]
    [event] = events.EventFile.model_validate(exported.json()).events  # what publishing reads
    assert event.id == key and event.type == "commenced" and event.locators == ("sec_1",)
    assert event.affected_work == ACT and event.reviewed_by == "App" and event.reviewed_at.tzinfo is not None
    assert str(event.evidence.url) == SOURCE and event.evidence.locator == "sec_2"
    shown = {e["event_id"]: e for e in listed(reviewer)["events"]}
    assert [h["status"] for h in shown[key]["history"]] == ["reviewed", "proposed", "proposed"]
    actions = [e["action"] for e in approver.get(f"/api/audit?act={ACT}").json()]
    assert actions.count("legal_event_saved") == 5  # two proposals, an edit, a review and a rejection


def test_an_event_added_by_hand_defaults_to_this_work_and_names_only_its_provisions(world):
    approver = signed_in(world, "approver@example.org")
    made = save_event(approver, {"type": "commenced", "date_from": "2024-06-12"}).json()
    [shown] = listed(approver)["events"]
    assert shown["event_id"] == made["event_id"] and made["event_id"].startswith("evt_")
    assert shown["event"]["affected_work"] == ACT and shown["event"]["evidence"]["url"] == SOURCE
    assert shown["event"]["precision"] == "day" and shown["event"]["scope"] == "whole"
    missing = save_event(approver, {"type": "repealed", "scope": "explicit_nodes", "locators": ["sec_99"]})
    assert missing.status_code == 422 and "sec_99" in missing.json()["message"]
    whole = save_event(approver, {"type": "repealed", "scope": "whole", "locators": ["sec_1"]})
    assert whole.status_code == 422 and "names no provisions" in whole.json()["message"]
    other = {"type": "repealed", "affected_work": "ng-old-act", "scope": "explicit_nodes"}
    other["locators"] = ["sec_99"]  # another work's provision
    assert save_event(approver, other, "reviewed").status_code == 200  # checked when published
    assert save_event(approver, {"type": "moved"}).status_code == 422
    assert approver.post(f"/api/documents/{ACT}/events", json={"status": "done"}).status_code == 422
    assert save_event(approver, {"type": "commenced"}, event_id="evt_not-a-uuid").status_code == 422


# --- amendment instructions -------------------------------------------------------------------------

INSTRUCTION = {
    "operation": "amend_words",
    "target": "sec_3",
    "words_delete": "one percent",
    "words_insert": "two percent",
    "effective_date": "2024-06-12",
    "amending": {"locator": "sec_1", "citation": "Cybercrimes (Amendment) Act, 2024, s. 1"},
}


def save_instruction(browser, status, expected=0, key=None, instruction=INSTRUCTION, target=TARGET):
    body = {"expected_revision": expected, "status": status, "target_work": target}
    body["instruction"] = instruction
    if key:
        body["instruction_id"] = key
    return browser.post(f"/api/documents/{ACT}/instructions", json=body)


def exported(browser, target=TARGET):
    return browser.get(f"/api/instructions/export?target={target}").json()


def test_an_instruction_exports_only_after_a_second_review_by_another_editor(world):
    reviewer = signed_in(world, "reviewer@example.org")
    approver = signed_in(world, "approver@example.org")
    second = signed_in(world, "second@example.org")
    made = save_instruction(reviewer, "proposed").json()
    key = made["instruction_id"]
    early = save_instruction(approver, "second_reviewed", 1, key)
    assert early.status_code == 409 and "reviewed" in early.json()["message"]  # nobody has reviewed it yet
    assert save_instruction(approver, "reviewed", 1, key).json()["revision"] == 2
    assert exported(approver) == {"instructions": []}
    alone = save_instruction(approver, "second_reviewed", 2, key)
    assert alone.status_code == 403 and "other than" in alone.json()["message"]  # EDT-04: two editors
    assert save_instruction(reviewer, "second_reviewed", 2, key).status_code == 403  # needs the approver role
    other = {**INSTRUCTION, "target": "sec_2"}
    changed = save_instruction(second, "second_reviewed", 2, key, instruction=other)
    assert changed.status_code == 409 and "exactly as reviewed" in changed.json()["message"]
    assert save_instruction(second, "second_reviewed", 2, key).json()["revision"] == 3

    found = exported(reviewer)
    [instruction] = [consolidation.Instruction.model_validate(i) for i in found["instructions"]]
    assert instruction.id == key and instruction.amending.work == ACT
    assert instruction.effective_date == date(2024, 6, 12)
    base = [{"page": 1, "units": [{"kind": "paragraph", "text": "1. Short title."},
                                  {"kind": "paragraph", "text": "3. The levy is one percent.", "notes": ["Levy"]}]}]  # fmt: skip
    result = consolidation.apply({"id": TARGET, "jurisdiction": "ng"}, base, [instruction], date(2025, 1, 1))
    assert result.pages[0]["units"][1]["text"] == "3. The levy is two percent." and result.applied == [key]
    assert exported(reviewer, "ng-another-act") == {"instructions": []}

    shown = reviewer.get(f"/api/documents/{ACT}/instructions").json()
    [listed_one] = shown["instructions"]
    assert [h["status"] for h in listed_one["history"]] == ["second_reviewed", "reviewed", "proposed"]
    assert [h["author"] for h in listed_one["history"]][:2] == ["second@example.org", "approver@example.org"]
    assert shown["targets"] == {TARGET: None} and "amend_words" in shown["operations"]


def test_an_instruction_is_checked_when_saved_and_a_rejected_one_never_exports(world):
    approver = signed_in(world, "approver@example.org")
    second = signed_in(world, "second@example.org")

    def refused(instruction):
        return save_instruction(approver, "proposed", instruction=instruction).json()["message"]

    elsewhere = {**INSTRUCTION["amending"], "locator": "sec_9"}
    assert "amending provision" in refused({**INSTRUCTION, "amending": elsewhere})
    assert refused({**INSTRUCTION, "amending": {"locator": "sec_1"}}) == "Cite the amending provision"
    assert "missing what it needs" in refused({k: v for k, v in INSTRUCTION.items() if k != "words_delete"})
    assert save_instruction(approver, "proposed", target="").status_code == 422
    key = save_instruction(approver, "reviewed").json()["instruction_id"]
    assert save_instruction(second, "rejected", 1, key, instruction={}).json()["revision"] == 2
    assert save_instruction(second, "second_reviewed", 2, key).status_code == 409
    assert exported(approver) == {"instructions": []}


def test_events_instructions_and_the_queue_need_a_signed_in_operator(world):
    stranger = client(world, dev=False)
    for path in (
        f"/api/documents/{ACT}/events",
        f"/api/documents/{ACT}/events/export",
        f"/api/documents/{ACT}/instructions",
        f"/api/instructions/export?target={TARGET}",
        "/api/queue",
    ):
        assert stranger.get(path).status_code == 401
    assert stranger.post(f"/api/documents/{ACT}/events/proposals").status_code == 401
    reviewer = signed_in(world, "reviewer@example.org")
    del reviewer.headers["X-CSRF-Token"]
    assert reviewer.post(f"/api/documents/{ACT}/events/proposals").status_code == 403
