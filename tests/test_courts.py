"""The court hierarchy register (CPR-04, JUR-03, JUR-04): synthetic registers only; a real court is entered
from a reviewed source, never from a test. Reviewed registers here are test data, reviewed by
operations.courts as a reviewer would."""

import json
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import courts
from api.v1 import create_v1_app
from operations import courts as review_cli

SOURCES = [
    {"id": "constitution", "title": "Constitution of Xland", "url": "https://example.org/constitution.pdf"},
    {"id": "practice", "title": "Practice Direction 3", "url": "https://example.org/pd3.pdf"},
]
REVIEWED_ON = date(2026, 10, 1)


def reviewed(data: dict) -> dict:
    return review_cli.review(data, "Test Reviewer", REVIEWED_ON)


def register(review=True, **changes) -> dict:
    data = {
        "jurisdiction": "xx",
        "review_state": "unreviewed",
        "precedent": {"binds": True, "note": "Higher courts bind lower ones.", "sources": ["constitution"]},
        "courts": [
            {"id": "final", "name": "Privy Council", "level": "apex", "jurisdiction": "aa-pc",
             "valid_until": "1963-10-01", "sources": ["constitution"]},
            {"id": "apex", "name": "Supreme Court", "level": "apex", "rank": 1, "sources": ["constitution"]},
            {"id": "appeal", "name": "Court of Appeal", "level": "appellate", "rank": 2, "appeals_to": ["apex"],
             "sources": ["constitution"]},
            {"id": "high", "name": "High Court", "level": "superior", "rank": 3, "appeals_to": ["appeal"],
             "sources": ["constitution"]},
            {"id": "tax", "name": "Tax Appeal Tribunal", "kind": "tribunal", "level": "specialised",
             "sources": ["constitution"]},
        ],
        "relations": [
            {"court": "apex", "bound": "high", "relation": "binding", "sources": ["constitution"]},
            {"court": "high", "bound": "appeal", "relation": "persuasive", "sources": ["practice"]},
            {"court": "high", "bound": "tax", "relation": "not_applicable", "sources": ["practice"]},
            {"court": "apex", "relation": "binding", "sources": ["constitution"]},
            {"court": "final", "relation": "binding", "valid_until": "1963-10-01", "sources": ["constitution"]},
            {"court": "final", "relation": "persuasive", "valid_from": "1963-10-01", "sources": ["constitution"]},
        ],
        "sources": SOURCES,
    }  # fmt: skip
    data |= changes
    return reviewed(data) if review else data


# A regional body's register: its court hears cassation on its uniform law in place of the member states'
# highest courts, and binds their courts on that matter only. Fictional, like Xland.
TREATY = {"id": "treaty", "title": "Treaty of the Union", "url": "https://example.org/treaty.pdf"}
REGIONAL = {
    "jurisdiction": "aa-xu",
    "review_state": "unreviewed",
    "precedent": {"binds": True, "note": "The treaty gives its court's rulings that force.", "sources": ["treaty"]},
    "courts": [{"id": "ucj", "name": "Union Court of Justice", "level": "regional", "sources": ["treaty"]}],
    "matters": [{"id": "uniform-acts", "name": "The Union's uniform acts", "sources": ["treaty"]}],
    "relations": [
        {"court": "ucj", "members": True, "matter": "uniform-acts", "relation": "binding", "sources": ["treaty"]}
    ],
    "members": [
        {"state": "xa", "valid_from": "1995-01-01", "sources": ["treaty"]},
        {"state": "xb", "valid_from": "1995-01-01", "valid_until": "2025-01-29", "withdrawal_notified": "2024-01-28",
         "sources": ["treaty"]},
    ],
    "member_routes": [{"court": "ucj", "kind": "cassation", "matter": "uniform-acts", "sources": ["treaty"]}],
    "declarations": [
        {"state": "xa", "court": "ucj", "valid_from": "2000-06-01", "valid_until": "2021-06-01",
         "withdrawal_deposited": "2020-06-01", "sources": ["treaty"]}
    ],
    "sources": [TREATY],
}  # fmt: skip


def test_a_relation_is_the_one_the_register_records_and_unknown_otherwise():
    xx = courts.CourtRegister.model_validate(register())
    assert courts.relation(xx, "apex", "xx", before="high") == "binding"
    assert courts.relation(xx, "high", "xx", before="appeal") == "persuasive"
    assert courts.relation(xx, "high", "xx", before="tax") == "not_applicable"
    assert courts.relation(xx, "apex", "xx") == "binding"  # on the jurisdiction's courts generally
    # Never inferred from rank or appeal routes: the Court of Appeal's bearing on the High Court is unrecorded.
    assert courts.relation(xx, "appeal", "xx", before="high") == "unknown"
    assert courts.relation(xx, "nowhere", "xx") == "unknown"
    assert courts.relation(xx, "apex", "xx", before="nowhere") == "unknown"
    assert courts.relation(xx, "apex", "yy") == "unknown"  # the register answers for its own jurisdiction


def test_a_relation_applies_on_its_dates():
    xx = courts.CourtRegister.model_validate(register())
    assert courts.relation(xx, "final", "xx", on=date(1960, 1, 1)) == "binding"
    assert courts.relation(xx, "final", "xx", on=date(1963, 10, 1)) == "persuasive"
    assert courts.relation(xx, "apex", "xx", before="final", on=date(1970, 1, 1)) == "unknown"  # no role then


def test_an_unreviewed_register_or_one_without_a_precedent_rule_answers_unknown():
    draft = courts.CourtRegister.model_validate(register(review=False))
    assert courts.relation(draft, "apex", "xx", before="high") == "unknown"
    silent = courts.CourtRegister.model_validate(register(precedent=None))
    assert courts.relation(silent, "apex", "xx", before="high") == "unknown"
    assert courts.bearings(draft, "apex", date(2026, 1, 1)) == ()


def test_a_civil_law_rule_that_precedent_does_not_bind_is_recorded_and_kept():
    rule = {"binds": False, "note": "Decisions bind only the parties.", "sources": ["constitution"]}
    persuasive = [{"court": "apex", "bound": "high", "relation": "persuasive", "sources": ["constitution"]}]
    xx = courts.CourtRegister.model_validate(register(precedent=rule, relations=persuasive))
    assert courts.relation(xx, "apex", "xx", before="high") == "persuasive"
    with pytest.raises(ValidationError, match="precedent does not bind here"):
        courts.CourtRegister.model_validate(register(precedent=rule))


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"sources": SOURCES[:1]}, "unknown sources"),
        ({"relations": [{"court": "apex", "bound": "mars", "relation": "binding", "sources": ["practice"]}]},
         "does not list"),
        ({"relations": [{"court": "apex", "relation": "binding", "sources": []}]}, "at least 1"),
        ({"relations": [{"court": "apex", "relation": "binding", "sources": ["practice"]},
                        {"court": "apex", "relation": "persuasive", "valid_from": "2000-01-01",
                         "sources": ["practice"]}]}, "apply at once"),
        ({"relations": [{"court": "apex", "members": True, "relation": "binding", "sources": ["practice"]}]},
         "member states' courts"),  # a national register lists no member states
        ({"relations": [{"court": "apex", "matter": "tax", "relation": "binding", "sources": ["practice"]}]},
         "matters the register does not list"),
        ({"courts": [{"id": "apex", "name": "Supreme Court", "level": "apex", "valid_from": "2010-01-01",
                      "valid_until": "2000-01-01", "sources": ["constitution"]}], "relations": []},
         "not after valid_from"),
    ],
)  # fmt: skip
def test_an_entry_without_a_source_or_a_court_or_with_a_conflict_is_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        courts.CourtRegister.model_validate(register(**changes))


def test_a_review_names_its_reviewer_and_covers_the_register_as_it_was_reviewed():
    with pytest.raises(ValidationError, match="names its reviewer"):
        courts.CourtRegister.model_validate(register() | {"reviewed_by": None})
    data = register()
    assert data["reviewed_by"] == "Test Reviewer" and data["reviewed_on"] == "2026-10-01"
    assert data["reviewed_sha256"] == courts.digest(courts.CourtRegister.model_validate(data))
    # An entry added after the review is not reviewed: the register is refused until reviewed again.
    data["relations"] = [*data["relations"], {"court": "appeal", "bound": "high", "relation": "binding",
                                              "sources": ["constitution"]}]  # fmt: skip
    with pytest.raises(ValidationError, match="changed after its review"):
        courts.CourtRegister.model_validate(data)
    again = courts.CourtRegister.model_validate(reviewed(data))
    assert courts.relation(again, "appeal", "xx", before="high") == "binding"
    with pytest.raises(review_cli.Refused, match="name the reviewer"):
        review_cli.review(data, "  ", REVIEWED_ON)
    with pytest.raises(review_cli.Refused, match="nothing to review"):
        review_cli.review({"jurisdiction": "zz"}, "Test Reviewer", REVIEWED_ON)


def test_the_review_command_shows_records_and_withdraws_a_review(tmp_path, capsys):
    path = tmp_path / "xx.json"
    path.write_text(json.dumps(register(review=False), indent=2))
    assert review_cli.main(["show", str(path)]) == 0
    sheet = capsys.readouterr().out
    assert sheet.startswith("xx: unreviewed") and "relation apex → high: binding" in sheet
    assert "Constitution of Xland: https://example.org/constitution.pdf" in sheet
    assert review_cli.main(["review", str(path), "--by", "A. Reviewer", "--on", "2026-10-21"]) == 0
    written = json.loads(path.read_text())
    assert list(written)[:3] == ["jurisdiction", "review_state", "precedent"]  # its keys stay in order
    assert (written["review_state"], written["reviewed_by"], written["reviewed_on"]) == (
        "reviewed", "A. Reviewer", "2026-10-21",
    )  # fmt: skip
    assert courts.relation(courts.load("xx", tmp_path), "apex", "xx", before="high") == "binding"
    written["courts"][1]["name"] = "Supreme Court of Xland"
    path.write_text(json.dumps(written))
    review_cli.main(["show", str(path)])
    assert capsys.readouterr().out.startswith("xx: reviewed, then changed: review it again")
    assert review_cli.main(["withdraw", str(path)]) == 0
    assert courts.load("xx", tmp_path).review_state == "unreviewed"


def test_a_register_is_loaded_by_its_place_code_and_none_written_is_empty(tmp_path):
    (tmp_path / "xx.json").write_text(json.dumps(register()))
    assert courts.relation(courts.load("xx", tmp_path), "apex", "xx", before="high") == "binding"
    empty = courts.load("zz", tmp_path)
    assert empty.review_state == "unreviewed" and empty.courts == ()
    assert courts.relation(empty, "apex", "zz") == "unknown"
    with pytest.raises(ValidationError):
        courts.load("../xx", tmp_path)
    (tmp_path / "yy.json").write_text(json.dumps(register()))
    with pytest.raises(ValueError, match="register of xx"):
        courts.load("yy", tmp_path)


def test_a_regional_court_binds_a_member_states_courts_only_on_its_matter_while_it_is_a_member():
    xu = courts.CourtRegister.model_validate(reviewed(REGIONAL))
    on = date(2026, 10, 21)
    # A ruling on a uniform act binds the courts of a member state on that matter, whichever court.
    assert courts.relation(xu, "ucj", "xa", matter="uniform-acts", on=on) == "binding"
    assert courts.relation(xu, "ucj", "xa-01", matter="uniform-acts", on=on) == "binding"  # its states too
    assert courts.relation(xu, "ucj", "xa", before="xa-hc", matter="uniform-acts", on=on) == "binding"
    # Not a blanket rank: on any other matter, or an unknown one, nothing is answered.
    assert courts.relation(xu, "ucj", "xa", on=on) == "unknown"
    assert courts.relation(xu, "ucj", "xa", matter="criminal-law", on=on) == "unknown"
    # Only while a member: xb's withdrawal took effect on 29 January 2025; xc never joined.
    assert courts.relation(xu, "ucj", "xb", matter="uniform-acts", on=date(2025, 1, 28)) == "binding"
    assert courts.relation(xu, "ucj", "xb", matter="uniform-acts", on=date(2025, 1, 29)) == "unknown"
    assert courts.relation(xu, "ucj", "xc", matter="uniform-acts", on=on) == "unknown"
    unreviewed = courts.CourtRegister.model_validate(REGIONAL)
    assert courts.relation(unreviewed, "ucj", "xa", matter="uniform-acts", on=on) == "unknown"


def test_a_relation_limited_to_a_matter_answers_before_one_that_is_not():
    data = register(relations=[
        {"court": "apex", "relation": "persuasive", "sources": ["constitution"]},
        {"court": "apex", "matter": "tax", "relation": "binding", "sources": ["constitution"]},
    ], matters=[{"id": "tax", "name": "Tax", "sources": ["practice"]}])  # fmt: skip
    xx = courts.CourtRegister.model_validate(data)
    assert courts.relation(xx, "apex", "xx", matter="tax") == "binding"
    assert courts.relation(xx, "apex", "xx", matter="land") == "persuasive"
    assert courts.relation(xx, "apex", "xx") == "persuasive"


def test_the_register_that_answers_is_the_jurisdictions_own_or_its_regional_bodys():
    registers = {"xa": courts.CourtRegister.model_validate(register(jurisdiction="xa")),
                 "aa-xu": courts.CourtRegister.model_validate(reviewed(REGIONAL))}  # fmt: skip
    on = date(2026, 10, 21)
    assert courts.answering("high", "xa", "xa", registers.get, on).jurisdiction == "xa"
    assert courts.answering("ucj", "aa-xu", "xa", registers.get, on).jurisdiction == "aa-xu"
    assert courts.answering("ucj", "aa-xu", "aa-xu", registers.get, on).jurisdiction == "aa-xu"
    assert courts.answering("ucj", "aa-xu", "xc", registers.get, on) is None  # not a member
    assert courts.answering("high", "xa", "xc", registers.get, on) is None  # another country's court
    assert courts.answering("nowhere", "xa", "xa", registers.get, on) is None


def test_memberships_and_declarations_are_dated_and_never_overlap():
    xu = courts.CourtRegister.model_validate(REGIONAL)
    [declaration] = xu.declarations
    assert declaration.applies(date(2020, 12, 1)) and not declaration.applies(date(2021, 6, 1))
    twice = [*REGIONAL["declarations"], {"state": "xa", "court": "ucj", "valid_from": "2021-01-01",
                                         "sources": ["treaty"]}]  # fmt: skip
    with pytest.raises(ValidationError, match="two declarations of xa"):
        courts.CourtRegister.model_validate(REGIONAL | {"declarations": twice})
    with pytest.raises(ValidationError, match="does not list"):
        courts.CourtRegister.model_validate(REGIONAL | {"member_routes": [
            {"court": "elsewhere", "kind": "reference", "sources": ["treaty"]}]})  # fmt: skip


def test_the_public_view_gives_every_entry_its_review_state_and_evidence_and_answers_unknown_unreviewed():
    view = courts.public(courts.CourtRegister.model_validate(REGIONAL))
    assert view.review_state == "unreviewed" and view.reviewed_by is None
    [relation] = view.relations
    assert (relation.relation, relation.recorded, relation.members) == ("unknown", "binding", True)
    assert relation.review_state == "unreviewed" and relation.evidence[0].url == TREATY["url"]
    assert view.members[1].withdrawal_notified == date(2024, 1, 28)
    assert view.declarations[0].valid_until == date(2021, 6, 1)
    assert view.member_routes[0].kind == "cassation" and view.matters[0].id == "uniform-acts"
    answered = courts.public(courts.CourtRegister.model_validate(reviewed(REGIONAL)))
    assert answered.relations[0].relation == "binding" and answered.courts[0].review_state == "reviewed"
    assert answered.reviewed_by == "Test Reviewer" and answered.precedent.evidence[0].title == TREATY["title"]


def test_an_evidence_unit_gives_the_court_and_its_bearing_only_from_a_reviewed_register():
    xx = courts.CourtRegister.model_validate(register())
    court = courts.evidence_court(xx, "high", date(2026, 10, 21))
    assert (court.id, court.name, court.jurisdiction, court.review_state) == (
        "high",
        "High Court",
        "xx",
        "reviewed",
    )
    assert [(b.court, b.relation) for b in court.bears_on] == [
        ("appeal", "persuasive"),
        ("tax", "not_applicable"),
    ]
    assert court.bears_on[0].evidence == ("https://example.org/pd3.pdf",)
    draft = courts.evidence_court(
        courts.CourtRegister.model_validate(register(review=False)), "high", date.today()
    )
    assert draft.review_state == "unreviewed" and draft.bears_on == ()


def test_the_api_serves_a_register_and_an_unreviewed_one_answers_unknown(store, tmp_path, monkeypatch):
    folder = tmp_path / "courts"
    folder.mkdir()
    (folder / "xx.json").write_text(json.dumps(register(review=False)))
    (folder / "aa-xu.json").write_text(json.dumps(reviewed(REGIONAL)))
    monkeypatch.setattr(courts, "COURTS", folder)
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    listing = client.get("/courts").json()["registers"]
    assert [(r["jurisdiction"], r["review_state"]) for r in listing] == [
        ("aa-xu", "reviewed"),
        ("xx", "unreviewed"),
    ]
    xx = client.get("/courts/xx").json()
    assert xx["review_state"] == "unreviewed" and "reviewed_by" not in xx
    assert {r["relation"] for r in xx["relations"]} == {"unknown"}
    first = xx["relations"][0]
    assert (first["court"], first["bound"], first["recorded"]) == ("apex", "high", "binding")
    assert first["evidence"] == [
        {"title": "Constitution of Xland", "url": "https://example.org/constitution.pdf"}
    ]
    assert xx["courts"][2]["appeals_to"] == ["apex"] and xx["precedent"]["binds"] is True
    xu = client.get("/courts/aa-xu").json()
    assert xu["relations"][0]["relation"] == "binding" and xu["relations"][0]["matter"] == "uniform-acts"
    assert xu["declarations"][0]["withdrawal_deposited"] == "2020-06-01"
    empty = client.get("/courts/zz").json()
    assert empty["review_state"] == "unreviewed" and empty["courts"] == [] and empty["precedent"] is None
    refused = client.get("/courts/XX")
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_input"
    changed = reviewed(REGIONAL) | {"courts": [{"id": "ucj", "name": "Renamed", "level": "regional",
                                                "sources": ["treaty"]}]}  # fmt: skip
    (folder / "aa-xu.json").write_text(json.dumps(changed))
    stale = client.get("/courts/aa-xu")
    assert stale.status_code == 503 and stale.json()["code"] == "temporarily_unavailable"


def test_every_register_written_and_the_readmes_example_are_valid():
    folder = Path(courts.COURTS)
    for path in folder.glob("*.json"):
        courts.load(path.stem)
    raw = re.search(r"```json\n(.*?)```", (folder / "README.md").read_text(), re.S).group(1)
    example = courts.CourtRegister.model_validate(json.loads(raw))
    assert example.review_state == "unreviewed" and courts.relation(example, "xx-sc", "xx") == "unknown"
    # Once reviewed, the same register answers what it records.
    reviewed_example = courts.CourtRegister.model_validate(reviewed(json.loads(raw)))
    assert courts.relation(reviewed_example, "xx-sc", "xx", before="xx-ca") == "binding"
