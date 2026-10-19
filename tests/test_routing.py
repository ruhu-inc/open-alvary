"""CPR-01: pages are routed by the evidence of their readings; under an owner's verification-routing
decision only the risky pages and an audit sample must be checked, and a material defect in the sample
escalates the document; without the decision every page is checked; calibration reports what reviews
changed on each route."""

import json

import pytest
from test_workbench import ACT, approve, confirm_details, node, save, signed_in

from workbench import content, routing
from workbench.store import Store, connect

POLICY = {
    "decision": "verification-routing-test",
    "decided_by": "Test",
    "decided_at": "2026-10-19T09:00:00+00:00",
    "audit_rate": 0.5,
    "minimum_per_document": 1,
}


def page(number, text, flags=(), tables=()):
    return {
        "page": number, "nodes": [node(text, page=number)], "text": text, "tables": list(tables),
        "furniture": [], "flags": list(flags), "checks": {"mineru": text, "azure": text},
    }  # fmt: skip


def flag(primary, check, severity="low"):
    return {"severity": severity, "kind": "differs", "primary": primary, "checks": {"mineru": check},
            "tokens": [0, 1], "readers_disagreeing": ["mineru"]}  # fmt: skip


def reading(*pages):
    return {
        "input_sha256": "0" * 64,
        "primary": {"reader": "docai", "version": {"version": "test"}},
        "checks": {"mineru": {}, "azure": {}},
        "summary": {"pages": len(pages), "flags": {}, "restored_markers": 0, "missing_checks": []},
        "pages": list(pages),
    }


def test_each_page_gets_one_route_from_its_evidence():
    first = routing.route(page(1, "1. Short title."), first=True, structure_issues=0)
    assert first[0] == "full" and "the first page" in first[1]
    assert routing.route(page(2, "2. Plain words."), first=False, structure_issues=0)[0] == "agreed"
    assert routing.route(page(2, "2. Plain words."), first=False, structure_issues=1)[0] == "full"
    assert (
        routing.route(page(3, "3. Words.", [flag("colour", "color")]), first=False, structure_issues=0)[0]
        == "flagged"
    )
    for risky in (
        flag("198", "199"),
        flag("shall", "shall not"),
        flag("section", "secton"),
        flag("a", "b", "high"),
    ):
        assert routing.route(page(3, "3. Words.", [risky]), first=False, structure_issues=0)[0] == "full"
    assert (
        routing.route(page(4, "4. Words.", tables=[{"rows": []}]), first=False, structure_issues=0)[0]
        == "full"
    )
    assert routing.route(page(5, "FIRST SCHEDULE"), first=False, structure_issues=0)[0] == "full"
    assert routing.route(page(5, "ARRANGEMENT OF SECTIONS"), first=False, structure_issues=0)[0] == "full"


def test_the_audit_sample_is_fixed_by_the_decision_and_has_a_minimum():
    agreed = list(range(2, 22))
    chosen = routing.sample("doc", agreed, POLICY)
    assert len(chosen) == 10 and chosen == routing.sample("doc", agreed, POLICY)  # 50%, deterministic
    assert routing.sample("doc", agreed, {**POLICY, "decision": "other"}) != chosen  # bound to the decision
    assert routing.sample("doc", [7], {**POLICY, "audit_rate": 0.01}) == [7]  # the minimum
    assert routing.sample("doc", [], POLICY) == []


def test_a_change_is_none_layout_or_material():
    machine = [{"kind": "paragraph", "text": "1. The Fund shall lend.", "notes": ["Loans"]}]
    assert routing.change(machine, [dict(machine[0])]) == "none"
    assert routing.change(machine, [{**machine[0], "text": "1. The Fund  shall lend."}]) == "none"  # spacing
    assert routing.change(machine, [{**machine[0], "kind": "heading"}]) == "layout"
    assert routing.change(machine, [{**machine[0], "notes": []}]) == "layout"
    assert routing.change(machine, [{**machine[0], "text": "1. The Fund shall not lend."}]) == "material"


def test_requirements_under_the_policy_and_escalation():
    found = reading(
        page(1, "1. Short title."), page(2, "2. Plain."), page(3, "3. Plain."), page(4, "4. Plain.")
    )
    plan = routing.requirement(ACT, found, {}, POLICY)
    assert plan["routes"] == {1: "full", 2: "agreed", 3: "agreed", 4: "agreed"}
    assert len(plan["audit_sample"]) == 2 and plan["required_pages"] == sorted([1, *plan["audit_sample"]])
    sampled = plan["audit_sample"][0]
    machine = content.units_from(found["pages"][sampled - 1])
    defect = {"status": "checked", "units": [{**machine[0], "text": "2. Changed words."}]}
    escalated = routing.requirement(ACT, found, {sampled: defect}, POLICY)
    assert escalated["escalated"] and escalated["audit_defects"] == [sampled]
    assert escalated["required_pages"] == [1, 2, 3, 4]


@pytest.fixture
def routed_world(tmp_path, monkeypatch):
    found = reading(
        page(1, "1. Accounts are kept.", []), page(2, "2. Plain words."), page(3, "3. More words.")
    )
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(found))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("approver@example.org", "App", {"reviewer", "approver"})
    library = content.Library(tmp_path)
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(library, "image", lambda act_id, page: image)
    policy = tmp_path / "verification-routing.json"
    policy.write_text(json.dumps(POLICY))
    monkeypatch.setattr(routing, "POLICY", policy)
    return store, library, found


def test_under_the_decision_only_routed_and_sampled_pages_wait_and_the_approval_records_them(routed_world):
    store, library, found = routed_world
    approver = signed_in((store, library), "approver@example.org")
    refused = approve(approver).json()["readiness"]
    plan = refused["routing"]
    assert plan["routes"] == {"1": "full", "2": "agreed", "3": "agreed"} or plan["routes"] == {
        1: "full",
        2: "agreed",
        3: "agreed",
    }
    required = plan["required_pages"]
    assert refused["unchecked_pages"] == required and len(required) == 2  # page 1 and one sampled page
    for number in required:
        units = content.units_from(found["pages"][number - 1])
        assert save(approver, page=number, units=units).status_code == 200
    confirm_details(approver)
    assert approve(approver).json() == {"revision": 1}
    [head] = store.approvals(ACT)
    assert head["routing"]["required_pages"] == required and head["routing"]["decision"] == POLICY["decision"]


def test_without_the_decision_every_page_waits(routed_world, monkeypatch, tmp_path):
    store, library, found = routed_world
    monkeypatch.setattr(routing, "POLICY", tmp_path / "absent.json")
    approver = signed_in((store, library), "approver@example.org")
    readiness = approve(approver).json()["readiness"]
    assert readiness["unchecked_pages"] == [1, 2, 3] and "routing" not in readiness


class FakeLibrary:
    def __init__(self, found):
        self.found = found

    def reading(self, document_id):
        return self.found


class FakeStore:
    def __init__(self, latest):
        self.latest = latest

    def latest_pages(self, document_id):
        return self.latest


def test_calibration_reports_what_reviews_changed_on_each_route():
    found = reading(page(1, "1. Short title."), page(2, "2. Plain."), page(3, "3. Plain."))
    units = {p["page"]: content.units_from(p) for p in found["pages"]}
    latest = {
        1: {"status": "checked", "units": units[1], "revision": 1},
        2: {"status": "checked", "units": units[2], "revision": 1},
        3: {"status": "checked", "units": [{**units[3][0], "text": "3. Changed."}], "revision": 1},
    }
    report = routing.calibrate(FakeLibrary(found), FakeStore(latest), [ACT])
    agreed = report["routes"]["agreed"]
    assert agreed["pages"] == 2 and agreed["material"] == 1 and agreed["material_rate"] == 0.5
    assert 0.5 < agreed["material_upper_95"] <= 1.0
