"""A judgment's citations in the workbench (EDT-01, EDT-02): proposals from its published paragraphs are
imported only where its text holds them; an approver confirms or rejects each, stating their role; a
treatment needs a doctrinal review with the reviewer's legal qualification and the paragraph's own words;
and only the reviewed ones export, as the citations file operations.citations publishes."""

import json

import pytest
from test_citations import ACT, CITED, CITING, CITING_UNITS, case_law
from test_workbench import client, node

from operations import citations
from workbench import content
from workbench.store import Store, connect

ADVOCATE = "Barrister and Solicitor of the Supreme Court of Nigeria"


def reading():
    nodes = [node(unit["text"], kind=unit["kind"]) for unit in CITING_UNITS]
    page = {"page": 1, "nodes": nodes, "text": "\n".join(n["text"] for n in nodes), "tables": [], "furniture": [],
            "flags": [], "checks": {"mineru": "", "azure": ""}}  # fmt: skip
    return {
        "input_sha256": "0" * 64,
        "primary": {"reader": "docai", "version": {"version": "test"}},
        "checks": {"mineru": {}, "azure": {}},
        "summary": {"pages": 1, "flags": {}, "restored_markers": 0, "missing_checks": []},
        "pages": [page],
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    judgments = tmp_path / "judgments"
    judgments.mkdir()
    manifest = {"jurisdiction": "ng", "collection": "ng-judgments-test", "documents": [
        {"id": CITING, "type": "judgment", "title": "Adeyemi v. Example Bank", "jurisdiction": "ng"}]}  # fmt: skip
    (judgments / "manifest.json").write_text(json.dumps(manifest))
    (judgments / f"{CITING}.reading.json").write_text(json.dumps(reading()))
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", str(judgments))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    store.add_operator("approver@example.org", "App", {"reviewer", "approver"})
    store.add_operator("advocate@example.org", "Test Advocate", {"reviewer", "approver"})
    return store, content.Library(tmp_path)


def signed_in(world, email):
    browser = client(world)
    assert browser.post("/auth/dev", json={"email": email}).status_code == 200
    browser.headers["X-CSRF-Token"] = browser.get("/api/me").json()["csrf"]
    return browser


@pytest.fixture
def proposed(store):
    """The proposals operations.citations reads from the judgment's published text."""
    case_law(store)
    with store.engine.begin() as conn:
        return citations.propose(conn, CITING)


def listed(browser):
    return browser.get(f"/api/documents/{CITING}/citations").json()


def by_cited(browser):
    return {c["citation"]["cited_work"]: c for c in listed(browser)["citations"]}


def test_proposals_are_imported_once_and_only_where_the_text_holds_their_paragraph(world, proposed):
    reviewer = signed_in(world, "reviewer@example.org")
    shown = listed(reviewer)
    assert shown["judgment"] and shown["citations"] == []
    assert [p["locator"] for p in shown["paragraphs"]] == ["para_1", "para_2", "para_3", "para_4"]
    assert shown["treatments"] == ["follows", "applies", "distinguishes", "doubts", "overrules"]
    other = reviewer.post(
        f"/api/documents/{CITING}/citations/proposals", json={**proposed, "work": "ng-other"}
    )
    assert other.status_code == 422 and "not this judgment" in other.json()["message"]
    changed = dict(proposed, proposals=[*proposed["proposals"], {**proposed["proposals"][0], "citing_locator": "para_9"},
                                        {**proposed["proposals"][1], "citing_locator": "para_1"}])  # fmt: skip
    imported = reviewer.post(f"/api/documents/{CITING}/citations/proposals", json=changed).json()
    assert len(imported["added"]) == 2
    assert sorted(s["reason"] for s in imported["skipped"]) == [
        "its words are not this paragraph's as the text now stands",
        "no paragraph para_9 in this text",
    ]
    again = reviewer.post(f"/api/documents/{CITING}/citations/proposals", json=proposed).json()
    assert again["added"] == [] and {s["reason"] for s in again["skipped"]} == {"already recorded"}
    cited = by_cited(reviewer)
    assert set(cited) == {ACT, CITED} and all(c["status"] == "proposed" for c in cited.values())
    assert (
        cited[ACT]["citation"]["cited_locator"] == "sec_2__subsec_1"
        and cited[ACT]["citation"]["review"] is None
    )
    # Nothing proposed is exported.
    assert reviewer.get(f"/api/documents/{CITING}/citations/export").json() == {"citations": []}


def test_an_approver_confirms_with_a_role_and_a_treatment_needs_a_qualified_doctrinal_review(world, proposed):
    reviewer = signed_in(world, "reviewer@example.org")
    approver = signed_in(world, "approver@example.org")
    advocate = signed_in(world, "advocate@example.org")
    reviewer.post(f"/api/documents/{CITING}/citations/proposals", json=proposed)
    cited = by_cited(reviewer)
    to_act, to_judgment = cited[ACT], cited[CITED]

    def decide(browser, item, status, expected=1, **values):
        body = {"citation_id": item["citation_id"], "expected_revision": expected, "status": status, **values}
        return browser.post(f"/api/documents/{CITING}/citations", json=body)

    refused = decide(reviewer, to_act, "confirmed", reviewer_role="owner")
    assert refused.status_code == 403 and "approver" in refused.json()["message"]
    assert decide(approver, to_act, "confirmed").status_code == 422  # a structural review states the role
    assert decide(approver, to_act, "confirmed", reviewer_role="owner").json()["revision"] == 2
    assert decide(approver, to_act, "rejected", expected=2).status_code == 409  # confirmed, not proposed
    assert decide(approver, to_judgment, "confirmed", reviewer_role="owner").status_code == 200

    def treat(browser, item, expected=2, **values):
        body = {"expected_revision": expected, "action": "record", "review_level": "doctrinal", "kind": "overrules",
                "opinion": "majority", "quote": "That decision is overruled.", "reviewer_role": "Nigerian advocate",
                "reviewer_qualification": ADVOCATE, **values}  # fmt: skip
        return browser.post(f"/api/documents/{CITING}/citations/{item['citation_id']}/treatment", json=body)

    unqualified = treat(advocate, to_judgment, reviewer_qualification="  ")
    assert unqualified.status_code == 422 and "legal qualification for ng" in unqualified.json()["message"]
    assert treat(advocate, to_judgment, review_level="structural").status_code == 422
    assert treat(advocate, to_act).status_code == 422  # treatment is of a judgment
    assert treat(advocate, to_judgment, quote="Words never written.").status_code == 422
    assert treat(advocate, to_judgment, kind="good_law").status_code == 422
    assert treat(advocate, to_judgment).json()["revision"] == 3
    assert (
        treat(advocate, to_judgment, expected=3).status_code == 409
    )  # one live treatment: withdraw it first

    exported = approver.get(f"/api/documents/{CITING}/citations/export")
    assert f"{CITING}.citations.json" in exported.headers["content-disposition"]
    file = citations.CitationFile.model_validate(exported.json())  # what publishing reads
    found = {c.cited_work: c for c in file.citations}
    act = found[ACT]
    assert (act.citing_work, act.citing_locator, act.cited_locator) == (CITING, "para_2", "sec_2__subsec_1")
    assert (act.reviewed_by, act.reviewer_role, act.review_state, act.treatments) == (
        "App",
        "owner",
        "reviewed",
        (),
    )
    [overruled] = found[CITED].treatments
    assert (overruled.kind, overruled.opinion, overruled.quote) == (
        "overrules",
        "majority",
        "That decision is overruled.",
    )
    assert (overruled.reviewed_by, overruled.reviewer_qualification) == ("Test Advocate", ADVOCATE)
    assert overruled.id.startswith("trt_") and overruled.reviewed_at.tzinfo is not None

    # Withdrawn, never edited: the treatment, then the citation (which withdraws its treatment too).
    withdraw = {"expected_revision": 3, "action": "withdraw"}
    assert advocate.post(f"/api/documents/{CITING}/citations/{to_judgment['citation_id']}/treatment",
                         json=withdraw).json()["revision"] == 4  # fmt: skip
    assert decide(approver, to_act, "withdrawn", expected=2).json()["revision"] == 3
    file = citations.CitationFile.model_validate(
        approver.get(f"/api/documents/{CITING}/citations/export").json()
    )
    states = {c.cited_work: (c.review_state, [t.review_state for t in c.treatments]) for c in file.citations}
    assert states == {ACT: ("withdrawn", []), CITED: ("reviewed", ["withdrawn"])}
    history = by_cited(approver)[CITED]["history"]
    assert [h["status"] for h in history] == ["confirmed", "confirmed", "confirmed", "proposed"]


def test_a_rejected_proposal_is_never_exported_nor_proposed_again(world, proposed):
    approver = signed_in(world, "approver@example.org")
    approver.post(f"/api/documents/{CITING}/citations/proposals", json=proposed)
    for item in listed(approver)["citations"]:
        body = {"citation_id": item["citation_id"], "expected_revision": 1, "status": "rejected"}
        assert approver.post(f"/api/documents/{CITING}/citations", json=body).status_code == 200
    assert approver.get(f"/api/documents/{CITING}/citations/export").json() == {"citations": []}
    again = approver.post(f"/api/documents/{CITING}/citations/proposals", json=proposed).json()
    assert again["added"] == []
