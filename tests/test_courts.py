"""The court hierarchy register (CPR-04, JUR-03, JUR-04): synthetic registers only; a real court is entered
from a reviewed source, never from a test. Reviewed entries here are test data, reviewed by
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
from retrieval.ranking import Hit, apply_signals, fuse

SOURCES = [
    {"id": "constitution", "title": "Constitution of Xland", "url": "https://example.org/constitution.pdf"},
    {"id": "practice", "title": "Practice Direction 3", "url": "https://example.org/pd3.pdf"},
]
REVIEWED_ON = date(2026, 10, 1)
OWNER = ("A. Owner", "owner")
ADVOCATE = ("A. Advocate", "Xland advocate", "Advocate of the High Court of Xland")


def says(source: str, words: str = "Synthetic words of the source.") -> list[dict]:
    return [{"source": source, "provision": "s. 1", "quote": words}]


def reviewed(data: dict, doctrinal: bool = True) -> dict:
    """The register reviewed as its reviewers would: every structural entry by the owner, then (with
    ``doctrinal``) every relation and the precedent rule by an advocate qualified there."""
    register = courts.CourtRegister.model_validate(data)
    if any(e.LEVEL == "structural" and not register.reviewed(e) for e in register.entries()):
        data = review_cli.review_structural(data, *OWNER, REVIEWED_ON)
    if doctrinal:
        by, role, qualification = ADVOCATE
        for entry in register.entries():
            if entry.LEVEL == "doctrinal" and not register.reviewed(entry):
                data = review_cli.review(data, entry.id, by, role, "doctrinal", REVIEWED_ON, qualification)
    return data


def register(review=True, **changes) -> dict:
    data = {
        "jurisdiction": "xx",
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
            {"id": "apex-on-high", "court": "apex", "bound": "high", "relation": "binding",
             "rests_on": says("constitution"), "sources": ["constitution"]},
            {"id": "high-on-appeal", "court": "high", "bound": "appeal", "relation": "persuasive",
             "rests_on": says("practice"), "sources": ["practice"]},
            {"id": "high-on-tax", "court": "high", "bound": "tax", "relation": "not_applicable",
             "rests_on": says("practice"), "sources": ["practice"]},
            {"id": "apex-on-courts", "court": "apex", "relation": "binding", "rests_on": says("constitution"),
             "sources": ["constitution"]},
            {"id": "final-on-courts-until-1963", "court": "final", "relation": "binding",
             "valid_until": "1963-10-01", "rests_on": says("constitution"), "sources": ["constitution"]},
            {"id": "final-on-courts-from-1963", "court": "final", "relation": "persuasive",
             "valid_from": "1963-10-01", "rests_on": says("constitution"), "sources": ["constitution"]},
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
    "precedent": {"binds": True, "note": "The treaty gives its court's rulings that force.", "sources": ["treaty"]},
    "courts": [{"id": "ucj", "name": "Union Court of Justice", "level": "regional", "sources": ["treaty"]}],
    "matters": [{"id": "uniform-acts", "name": "The Union's uniform acts", "sources": ["treaty"]}],
    "relations": [
        {"id": "ucj-on-members-uniform-acts", "court": "ucj", "members": True, "matter": "uniform-acts",
         "relation": "binding", "rests_on": says("treaty"), "sources": ["treaty"]}
    ],
    "members": [
        {"id": "member-xa", "state": "xa", "valid_from": "1995-01-01", "sources": ["treaty"]},
        {"id": "member-xb", "state": "xb", "valid_from": "1995-01-01", "valid_until": "2025-01-29",
         "withdrawal_notified": "2024-01-28", "sources": ["treaty"]},
    ],
    "member_routes": [{"id": "route-cassation", "court": "ucj", "kind": "cassation", "matter": "uniform-acts",
                       "sources": ["treaty"]}],
    "declarations": [
        {"id": "declaration-xa", "state": "xa", "court": "ucj", "valid_from": "2000-06-01",
         "valid_until": "2021-06-01", "withdrawal_deposited": "2020-06-01", "sources": ["treaty"]}
    ],
    "sources": [TREATY],
}  # fmt: skip
# A community court whose decisions on its treaty take precedence over national courts' decisions on a
# similar matter, and whose preliminary rulings bind the court that referred the question: neither is
# stare decisis, and the register records no precedent rule. Fictional.
PRECEDENCE = "Decisions of the Court on the interpretation of this Treaty shall have precedence."
COMMUNITY = {
    "jurisdiction": "aa-xc",
    "courts": [{"id": "xcj", "name": "Community Court of Justice", "level": "regional", "sources": ["treaty"]}],
    "matters": [{"id": "treaty", "name": "Interpretation of the Treaty", "sources": ["treaty"]}],
    "relations": [
        {"id": "xcj-prevails", "court": "xcj", "members": True, "matter": "treaty",
         "relation": "prevails_on_matter", "rests_on": says("treaty", PRECEDENCE), "sources": ["treaty"]},
        {"id": "xcj-referred", "court": "xcj", "members": True, "matter": "treaty",
         "relation": "binding_in_referred_case", "rests_on": says("treaty"), "sources": ["treaty"]},
    ],
    "members": [{"id": "member-xa", "state": "xa", "valid_from": "2000-07-07", "sources": ["treaty"]}],
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


def test_an_unreviewed_relation_or_one_without_a_precedent_rule_answers_unknown():
    draft = courts.CourtRegister.model_validate(register(review=False))
    assert courts.relation(draft, "apex", "xx", before="high") == "unknown"
    assert courts.bearings(draft, "apex", date(2026, 1, 1)) == ()
    silent = courts.CourtRegister.model_validate(register(precedent=None))
    assert courts.relation(silent, "apex", "xx", before="high") == "unknown"
    # A precedent rule not yet reviewed doesn't support a binding relation either.
    data = register()
    data["precedent"]["note"] = "Higher courts bind lower ones, as the Constitution says."
    assert (
        courts.relation(courts.CourtRegister.model_validate(data), "apex", "xx", before="high") == "unknown"
    )


def test_a_civil_law_rule_that_precedent_does_not_bind_is_recorded_and_kept():
    rule = {"binds": False, "note": "Decisions bind only the parties.", "sources": ["constitution"]}
    persuasive = [{"id": "apex-on-high", "court": "apex", "bound": "high", "relation": "persuasive",
                   "rests_on": says("constitution"), "sources": ["constitution"]}]  # fmt: skip
    xx = courts.CourtRegister.model_validate(register(precedent=rule, relations=persuasive))
    assert courts.relation(xx, "apex", "xx", before="high") == "persuasive"
    with pytest.raises(ValidationError, match="precedent does not bind here"):
        courts.CourtRegister.model_validate(register(review=False, precedent=rule))


def relation(**fields) -> dict:
    return {"id": "r", "court": "apex", "relation": "binding", "rests_on": says("practice"),
            "sources": ["practice"]} | fields  # fmt: skip


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"sources": SOURCES[:1]}, "unknown sources"),
        ({"relations": [relation(bound="mars")]}, "does not list"),
        ({"relations": [relation(sources=[])]}, "at least 1"),
        ({"relations": [relation(rests_on=[])]}, "at least 1"),  # every relation quotes what it rests on
        ({"relations": [relation(rests_on=says("constitution"))]}, "quotes sources it does not cite"),
        ({"relations": [relation(), relation(id="r2", relation="persuasive", valid_from="2000-01-01")]},
         "apply at once"),
        ({"relations": [relation(members=True)]}, "member states' courts"),  # a national register lists none
        ({"relations": [relation(matter="tax")]}, "matters the register does not list"),
        ({"relations": [relation(relation="prevails_on_matter")]}, "prevails on a matter, yet names none"),
        ({"relations": [relation(id="apex")]}, "entry ids must be unique"),
        ({"courts": [{"id": "apex", "name": "Supreme Court", "level": "apex", "valid_from": "2010-01-01",
                      "valid_until": "2000-01-01", "sources": ["constitution"]}], "relations": []},
         "not after valid_from"),
    ],
)  # fmt: skip
def test_an_entry_without_a_source_or_a_court_or_with_a_conflict_is_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        courts.CourtRegister.model_validate(register(review=False, **changes))


def test_a_doctrinal_review_without_a_qualification_is_refused():
    data = register(review=False)
    by, role, qualification = ADVOCATE
    for blank in (None, "", "   "):
        with pytest.raises(review_cli.Refused, match="state the reviewer's legal qualification"):
            review_cli.review(data, "apex-on-high", "A. Owner", "owner", "doctrinal", REVIEWED_ON, blank)
    # Whatever the role: an owner recorded as qualified there may review a relation.
    done = review_cli.review(
        data, "apex-on-high", "A. Owner", "owner", "doctrinal", REVIEWED_ON, qualification
    )
    assert done["relations"][0]["review"]["reviewer_qualification"] == qualification
    # Written by hand, a doctrinal review without one doesn't load.
    written = json.loads(json.dumps(done))
    written["relations"][0]["review"]["reviewer_qualification"] = " "
    with pytest.raises(ValidationError, match="states the reviewer's legal qualification"):
        courts.CourtRegister.model_validate(written)
    # A relation is never reviewed at the structural level, nor a court at the doctrinal one.
    with pytest.raises(review_cli.Refused, match="apex-on-high is a doctrinal entry"):
        review_cli.review(data, "apex-on-high", *OWNER, "structural", REVIEWED_ON)
    with pytest.raises(review_cli.Refused, match="high is a structural entry"):
        review_cli.review(data, "high", by, role, "doctrinal", REVIEWED_ON, qualification)
    written["relations"][0]["review"] |= {"review_level": "structural", "reviewer_qualification": None}
    with pytest.raises(ValidationError, match="doctrinal entry, reviewed at another level"):
        courts.CourtRegister.model_validate(written)


def test_structural_entries_reviewed_by_the_owner_are_served_reviewed_while_relations_stay_unknown():
    owned = reviewed(register(review=False), doctrinal=False)
    xx = courts.CourtRegister.model_validate(owned)
    assert all(xx.reviewed(c) for c in xx.courts) and not any(xx.reviewed(r) for r in xx.relations)
    view = courts.public(xx)
    assert view.review_state == "partly_reviewed"
    court = view.courts[1]
    assert (court.review_state, court.review_level, court.reviewed_by, court.reviewer_role) == (
        "reviewed", "structural", "A. Owner", "owner",
    )  # fmt: skip
    assert court.reviewed_on == REVIEWED_ON and court.reviewer_qualification is None
    assert {(r.review_state, r.review_level, r.relation) for r in view.relations} == {
        ("unreviewed", "doctrinal", "unknown")
    }
    assert view.precedent.review_state == "unreviewed"
    assert courts.relation(xx, "apex", "xx", before="high") == "unknown"
    assert courts.evidence_court(xx, "high", date(2026, 10, 21)).review_state == "reviewed"
    assert courts.evidence_court(xx, "high", date(2026, 10, 21)).bears_on == ()
    # The relations answer once each has its own doctrinal review, and the precedent rule its own.
    advocate = courts.CourtRegister.model_validate(reviewed(owned))
    assert courts.relation(advocate, "apex", "xx", before="high") == "binding"
    assert courts.public(advocate).review_state == "reviewed"
    assert courts.summary(advocate).reviewed_entries == courts.summary(advocate).entries == 12


def test_editing_one_entry_unreviews_only_that_entry():
    data = register()
    data["relations"][0]["note"] = "Added after the review."
    xx = courts.CourtRegister.model_validate(data)  # the register still loads
    assert [e.id for e in xx.changed()] == ["apex-on-high"]
    assert courts.relation(xx, "apex", "xx", before="high") == "unknown"
    assert courts.relation(xx, "high", "xx", before="appeal") == "persuasive"  # the others stand
    assert courts.relation(xx, "apex", "xx") == "binding"
    view = courts.public(xx)
    assert view.relations[0].review_state == "unreviewed" and view.relations[0].reviewed_by is None
    assert view.relations[1].review_state == "reviewed" and view.review_state == "partly_reviewed"
    # A court renamed is unreviewed, and every relation to it answers unknown until it is reviewed again.
    renamed = register()
    renamed["courts"][2]["name"] = "Court of Appeal of Xland"
    xx = courts.CourtRegister.model_validate(renamed)
    assert [e.id for e in xx.changed()] == ["appeal"]
    assert courts.relation(xx, "high", "xx", before="appeal") == "unknown"
    assert courts.relation(xx, "high", "xx", before="tax") == "not_applicable"
    # What an entry rests on is part of what was reviewed: a source changed unreviews each entry citing it.
    moved = register()
    moved["sources"][1]["url"] = "https://example.org/pd3-revised.pdf"
    assert [e.id for e in courts.CourtRegister.model_validate(moved).changed()] == [
        "high-on-appeal", "high-on-tax"
    ]  # fmt: skip
    # Reviewed again, it answers.
    again = courts.CourtRegister.model_validate(reviewed(data))
    assert not again.changed() and courts.relation(again, "apex", "xx", before="high") == "binding"


def test_a_review_names_its_reviewer_and_role():
    data = register(review=False)
    with pytest.raises(review_cli.Refused, match="name the reviewer"):
        review_cli.review(data, "high", "  ", "owner", "structural", REVIEWED_ON)
    with pytest.raises(review_cli.Refused, match="name the reviewer's role"):
        review_cli.review(data, "high", "A. Owner", " ", "structural", REVIEWED_ON)
    with pytest.raises(review_cli.Refused, match="has no entry nowhere"):
        review_cli.review(data, "nowhere", *OWNER, "structural", REVIEWED_ON)
    with pytest.raises(review_cli.Refused, match="no structural entry awaits review"):
        review_cli.review_structural(reviewed(data), *OWNER, REVIEWED_ON)
    review = reviewed(data)["courts"][1]["review"]
    assert review == {"review_level": "structural", "reviewed_by": "A. Owner", "reviewer_role": "owner",
                      "reviewed_on": "2026-10-01", "reviewed_sha256": review["reviewed_sha256"]}  # fmt: skip


def test_the_review_command_shows_records_and_withdraws_reviews(tmp_path, capsys):
    path = tmp_path / "xx.json"
    path.write_text(json.dumps(register(review=False), indent=2))
    assert review_cli.main(["show", str(path)]) == 0
    sheet = capsys.readouterr().out
    assert sheet.startswith("xx: 0 of 12 entries reviewed")
    assert "relation apex-on-high [doctrinal, unreviewed]: apex → high: binding" in sheet
    assert "“Synthetic words of the source.” (Constitution of Xland, s. 1)" in sheet
    assert "Constitution of Xland: https://example.org/constitution.pdf" in sheet
    assert review_cli.main(["review-structural", str(path), "--by", "A. Owner", "--role", "owner",
                            "--on", "2026-10-21"]) == 0  # fmt: skip
    written = json.loads(path.read_text())
    assert list(written)[:3] == ["jurisdiction", "precedent", "courts"]  # its keys stay in order
    assert (
        written["courts"][1]["review"]["reviewer_role"] == "owner" and "review" not in written["relations"][0]
    )
    by, role, qualification = ADVOCATE
    refused = ["review", str(path), "apex-on-high", "--by", by, "--role", role, "--level", "doctrinal"]
    assert review_cli.main(refused) == 1
    assert "state the reviewer's legal qualification" in capsys.readouterr().err
    for entry in ("precedent", "apex-on-high"):
        assert review_cli.main([*refused[:2], entry, *refused[3:], "--qualification", qualification]) == 0
    assert courts.relation(courts.load("xx", tmp_path), "apex", "xx", before="high") == "binding"
    written = json.loads(path.read_text())
    written["relations"][0]["note"] = "Changed after its review."
    path.write_text(json.dumps(written))
    review_cli.main(["show", str(path)])
    assert (
        "relation apex-on-high [doctrinal, reviewed, then changed: review it again]"
        in capsys.readouterr().out
    )
    assert review_cli.main(["withdraw", str(path), "apex-on-high"]) == 0
    withdrawn = courts.load("xx", tmp_path)
    assert not withdrawn.relations[0].review and withdrawn.reviewed(withdrawn.courts[0])
    assert review_cli.main(["withdraw", str(path)]) == 0
    assert courts.state(courts.load("xx", tmp_path)) == "unreviewed"


def test_a_register_is_loaded_by_its_place_code_and_none_written_is_empty(tmp_path):
    (tmp_path / "xx.json").write_text(json.dumps(register()))
    assert courts.relation(courts.load("xx", tmp_path), "apex", "xx", before="high") == "binding"
    empty = courts.load("zz", tmp_path)
    assert courts.state(empty) == "unreviewed" and empty.courts == ()
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
    # A membership not yet reviewed answers nothing for that state.
    data = reviewed(REGIONAL)
    data["members"][0]["note"] = "Changed after its review."
    assert courts.relation(courts.CourtRegister.model_validate(data), "ucj", "xa", matter="uniform-acts",
                           on=on) == "unknown"  # fmt: skip


def test_a_rule_of_precedence_answers_only_a_question_of_its_matter_and_needs_no_precedent_rule():
    xc = courts.CourtRegister.model_validate(reviewed(COMMUNITY))
    assert xc.precedent is None  # not stare decisis: it rests on its own provision
    assert courts.relation(xc, "xcj", "xa", matter="treaty") == "prevails_on_matter"
    assert courts.relation(xc, "xcj", "xa") == "unknown"  # no matter: no answer
    assert courts.relation(xc, "xcj", "xa", matter="land") == "unknown"  # outside it
    # Its bearing travels with a decision of the court, with the words it rests on.
    bearing = courts.evidence_court(xc, "xcj", date.today()).bears_on[0]
    assert (bearing.id, bearing.relation, bearing.matter) == ("xcj-prevails", "prevails_on_matter", "treaty")
    assert bearing.rests_on[0].quote == PRECEDENCE and bearing.rests_on[0].url == TREATY["url"]


def test_a_ruling_binding_only_in_the_referred_case_answers_after_a_relation_with_general_effect():
    xc = courts.CourtRegister.model_validate(reviewed(COMMUNITY))
    assert courts.relation(xc, "xcj", "xa", matter="treaty") == "prevails_on_matter"
    referred = COMMUNITY | {"relations": COMMUNITY["relations"][1:]}
    alone = courts.CourtRegister.model_validate(reviewed(referred))
    assert courts.relation(alone, "xcj", "xa", matter="treaty") == "binding_in_referred_case"
    two = [*COMMUNITY["relations"], COMMUNITY["relations"][1] | {"id": "xcj-referred-again"}]
    with pytest.raises(ValidationError, match="apply at once"):
        courts.CourtRegister.model_validate(COMMUNITY | {"relations": two})


def judgments(relations: dict[str, str | None]) -> list[str]:
    """The order of an Act and decisions in one band, each decision with its court's relation."""
    hits = [Hit(version_id=v, start_byte=0, end_byte=10, work_id=v, node_id="n", unit="provision",
                kind="paragraph", locator="para_1", pinpoint=None, heading=None, language="en",
                config="english", content_hash="0" * 64, legal_status="current", replaced=False,
                jurisdiction_id="xa", document_type="act" if v == "act" else "judgment")
            for v in ["persuasive", "act", *relations]]  # fmt: skip
    fused = fuse({"lexical": hits})
    for candidate in fused:
        candidate.court_relation = {"persuasive": "persuasive", **relations}.get(candidate.hit.version_id)
    return [c.hit.version_id for c in apply_signals(fused, set())]


def test_a_rule_of_precedence_ranks_a_decision_only_on_a_question_within_its_matter():
    xc = courts.CourtRegister.model_validate(reviewed(COMMUNITY))
    within = courts.relation(xc, "xcj", "xa", matter="treaty")
    assert judgments({"xcj": within}) == ["xcj", "act", "persuasive"]  # authoritative, as binding
    for outside in (courts.relation(xc, "xcj", "xa"), courts.relation(xc, "xcj", "xa", matter="land")):
        assert judgments({"xcj": outside}) == ["persuasive", "act", "xcj"]  # no rank from it


def test_a_ruling_binding_only_in_the_referred_case_never_affects_ranking():
    alone = courts.CourtRegister.model_validate(
        reviewed(COMMUNITY | {"relations": COMMUNITY["relations"][1:]})
    )
    referred = courts.relation(alone, "xcj", "xa", matter="treaty")
    assert referred == "binding_in_referred_case"
    assert judgments({"xcj": referred}) == judgments({"xcj": "unknown"}) == ["persuasive", "act", "xcj"]
    assert judgments({"xcj": referred, "other": "unknown"}) == judgments(
        {"xcj": "unknown", "other": "unknown"}
    )


def test_a_relation_limited_to_a_matter_answers_before_one_that_is_not():
    data = register(relations=[
        {"id": "apex-on-courts", "court": "apex", "relation": "persuasive", "rests_on": says("constitution"),
         "sources": ["constitution"]},
        {"id": "apex-on-courts-tax", "court": "apex", "matter": "tax-law", "relation": "binding",
         "rests_on": says("constitution"), "sources": ["constitution"]},
    ], matters=[{"id": "tax-law", "name": "Tax", "sources": ["practice"]}])  # fmt: skip
    xx = courts.CourtRegister.model_validate(data)
    assert courts.relation(xx, "apex", "xx", matter="tax-law") == "binding"
    assert courts.relation(xx, "apex", "xx", matter="land") == "persuasive"
    assert courts.relation(xx, "apex", "xx") == "persuasive"
    # The matter's words are part of what the relation's reviewer saw: widened, the relation is unreviewed.
    data["matters"][0]["name"] = "Tax and customs"
    changed = courts.CourtRegister.model_validate(data)
    assert {e.id for e in changed.changed()} == {"tax-law", "apex-on-courts-tax"}
    assert courts.relation(changed, "apex", "xx", matter="tax-law") == "unknown"


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
    registers["aa-xu"] = courts.CourtRegister.model_validate(REGIONAL)  # its memberships unreviewed
    assert courts.answering("ucj", "aa-xu", "xa", registers.get, on) is None


def test_memberships_and_declarations_are_dated_and_never_overlap():
    xu = courts.CourtRegister.model_validate(REGIONAL)
    [declaration] = xu.declarations
    assert declaration.applies(date(2020, 12, 1)) and not declaration.applies(date(2021, 6, 1))
    twice = [*REGIONAL["declarations"], {"id": "declaration-xa-2021", "state": "xa", "court": "ucj",
                                         "valid_from": "2021-01-01", "sources": ["treaty"]}]  # fmt: skip
    with pytest.raises(ValidationError, match="two declarations of xa"):
        courts.CourtRegister.model_validate(REGIONAL | {"declarations": twice})
    with pytest.raises(ValidationError, match="does not list"):
        courts.CourtRegister.model_validate(REGIONAL | {"member_routes": [
            {"id": "route", "court": "elsewhere", "kind": "reference", "sources": ["treaty"]}]})  # fmt: skip


def test_the_public_view_gives_every_entry_its_review_and_evidence_and_answers_unknown_unreviewed():
    view = courts.public(courts.CourtRegister.model_validate(REGIONAL))
    assert view.review_state == "unreviewed"
    [relation] = view.relations
    assert (relation.id, relation.relation, relation.recorded, relation.members) == (
        "ucj-on-members-uniform-acts", "unknown", "binding", True,
    )  # fmt: skip
    assert relation.review_state == "unreviewed" and relation.evidence[0].url == TREATY["url"]
    assert relation.rests_on[0].quote == "Synthetic words of the source."
    assert (relation.rests_on[0].provision, relation.rests_on[0].title) == ("s. 1", TREATY["title"])
    assert view.members[1].withdrawal_notified == date(2024, 1, 28) and view.members[1].id == "member-xb"
    assert view.declarations[0].valid_until == date(2021, 6, 1)
    assert view.member_routes[0].kind == "cassation" and view.matters[0].id == "uniform-acts"
    answered = courts.public(courts.CourtRegister.model_validate(reviewed(REGIONAL)))
    assert answered.relations[0].relation == "binding" and answered.courts[0].review_state == "reviewed"
    assert answered.relations[0].reviewer_qualification == ADVOCATE[2]
    assert answered.precedent.id == "precedent" and answered.precedent.reviewed_by == ADVOCATE[0]
    assert answered.precedent.evidence[0].title == TREATY["title"]


def test_an_evidence_unit_gives_the_court_and_its_bearing_only_from_reviewed_entries():
    xx = courts.CourtRegister.model_validate(register())
    court = courts.evidence_court(xx, "high", date(2026, 10, 21))
    assert (court.id, court.name, court.jurisdiction, court.review_state, court.reviewed_on) == (
        "high", "High Court", "xx", "reviewed", REVIEWED_ON,
    )  # fmt: skip
    assert [(b.id, b.court, b.relation) for b in court.bears_on] == [
        ("high-on-appeal", "appeal", "persuasive"),
        ("high-on-tax", "tax", "not_applicable"),
    ]
    assert court.bears_on[0].evidence == ("https://example.org/pd3.pdf",)
    assert court.bears_on[0].rests_on[0].url == "https://example.org/pd3.pdf"
    draft = courts.evidence_court(
        courts.CourtRegister.model_validate(register(review=False)), "high", date.today()
    )
    assert draft.review_state == "unreviewed" and draft.bears_on == () and draft.reviewed_on is None


def test_the_api_serves_each_entrys_review_and_an_unreviewed_relation_answers_unknown(
    store, tmp_path, monkeypatch
):
    folder = tmp_path / "courts"
    folder.mkdir()
    (folder / "xx.json").write_text(json.dumps(register(review=False)))
    (folder / "aa-xu.json").write_text(json.dumps(reviewed(REGIONAL)))
    monkeypatch.setattr(courts, "COURTS", folder)
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    listing = client.get("/courts").json()["registers"]
    assert listing == [
        {"jurisdiction": "aa-xu", "review_state": "reviewed", "courts": 1, "relations": 1, "entries": 8,
         "reviewed_entries": 8},
        {"jurisdiction": "xx", "review_state": "unreviewed", "courts": 5, "relations": 6, "entries": 12,
         "reviewed_entries": 0},
    ]  # fmt: skip
    xx = client.get("/courts/xx").json()
    assert xx["review_state"] == "unreviewed" and "reviewed_by" not in xx
    assert {r["relation"] for r in xx["relations"]} == {"unknown"}
    first = xx["relations"][0]
    assert (first["id"], first["court"], first["bound"], first["recorded"]) == ("apex-on-high", "apex", "high",
                                                                               "binding")  # fmt: skip
    assert (first["review_state"], first["review_level"]) == ("unreviewed", "doctrinal")
    assert "reviewed_by" not in first and "reviewer_role" not in first
    assert first["rests_on"] == [
        {
            "quote": "Synthetic words of the source.",
            "provision": "s. 1",
            "title": "Constitution of Xland",
            "url": "https://example.org/constitution.pdf",
        }
    ]
    assert first["evidence"] == [
        {"title": "Constitution of Xland", "url": "https://example.org/constitution.pdf"}
    ]
    assert xx["courts"][2]["appeals_to"] == ["apex"] and xx["precedent"]["binds"] is True
    xu = client.get("/courts/aa-xu").json()
    assert xu["relations"][0]["relation"] == "binding" and xu["relations"][0]["matter"] == "uniform-acts"
    assert {k: xu["relations"][0][k] for k in ("reviewed_by", "reviewer_role", "reviewer_qualification",
                                                "reviewed_on", "review_level")} == {
        "reviewed_by": "A. Advocate", "reviewer_role": "Xland advocate",
        "reviewer_qualification": "Advocate of the High Court of Xland", "reviewed_on": "2026-10-01",
        "review_level": "doctrinal",
    }  # fmt: skip
    assert xu["courts"][0]["reviewer_role"] == "owner" and "reviewer_qualification" not in xu["courts"][0]
    assert xu["declarations"][0]["withdrawal_deposited"] == "2020-06-01"
    empty = client.get("/courts/zz").json()
    assert empty["review_state"] == "unreviewed" and empty["courts"] == [] and empty["precedent"] is None
    refused = client.get("/courts/XX")
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_input"
    # An entry changed after its review is served unreviewed, on its own; the rest stays reviewed.
    changed = reviewed(REGIONAL)
    changed["courts"][0]["name"] = "Renamed"
    (folder / "aa-xu.json").write_text(json.dumps(changed))
    stale = client.get("/courts/aa-xu").json()
    assert stale["review_state"] == "partly_reviewed" and stale["courts"][0]["review_state"] == "unreviewed"
    assert (
        stale["relations"][0]["review_state"] == "reviewed" and stale["relations"][0]["relation"] == "unknown"
    )
    # A register that doesn't load (here, a review at the wrong level) is unavailable, never stale.
    changed["courts"][0]["review"]["review_level"] = "doctrinal"
    (folder / "aa-xu.json").write_text(json.dumps(changed))
    broken = client.get("/courts/aa-xu")
    assert broken.status_code == 503 and broken.json()["code"] == "temporarily_unavailable"


def test_every_register_written_and_the_readmes_example_are_valid_and_no_review_is_stale():
    folder = Path(courts.COURTS)
    for path in folder.glob("*.json"):
        written = courts.load(path.stem)
        # An entry edited after its review loses it: review it again, or withdraw the review.
        assert not written.changed(), f"{path.name}: changed after review {[e.id for e in written.changed()]}"
    raw = re.search(r"```json\n(.*?)```", (folder / "README.md").read_text(), re.S).group(1)
    example = courts.CourtRegister.model_validate(json.loads(raw))
    assert courts.state(example) == "unreviewed" and courts.relation(example, "xx-sc", "xx") == "unknown"
    # Once reviewed, the same register answers what it records.
    reviewed_example = courts.CourtRegister.model_validate(reviewed(json.loads(raw)))
    assert courts.relation(reviewed_example, "xx-sc", "xx", before="xx-ca") == "binding"


def test_the_east_african_court_prevails_on_the_treaty_and_binds_no_national_court():
    """The EAC register as proposed (unreviewed): EAC Treaty art. 33(2) is a rule of precedence on the
    Treaty's interpretation and application, quoted verbatim, never stare decisis."""
    eac = courts.load("aa-eac")
    assert {r.relation for r in eac.relations} == {"prevails_on_matter"}
    assert {r.matter for r in eac.relations} == {"eac-treaty"}
    for r in eac.relations:
        assert r.rests_on[0].provision == "art. 33(2)" and r.rests_on[0].quote == (
            "Decisions of the Court on the interpretation and application of this Treaty shall have precedence "
            "over decisions of national courts on a similar matter."
        )
    assert all(courts.relation(eac, r.court, "ke", matter="eac-treaty") == "unknown" for r in eac.relations)


def test_ghanas_houses_of_chiefs_are_bound_only_on_a_chieftaincy_question():
    """Courts Act s. 39(d) makes the Houses of Chiefs and Traditional Councils lower courts only for a cause
    or matter affecting chieftaincy: the Supreme Court's decisions bind them on that matter alone. The
    proposal stays unreviewed; a copy reviewed here as its reviewers would answers."""
    houses = ("gh-nhc", "gh-rhc", "gh-tc")
    proposal = courts.load("gh")
    scoped = [r for r in proposal.relations if r.bound in houses]
    assert {(r.court, r.bound, r.matter, r.relation) for r in scoped} == {
        ("gh-sc", house, "chieftaincy", "binding") for house in houses
    }
    assert {e.id for e in proposal.matters} == {"chieftaincy"}
    for r in scoped:
        assert ("s. 39(d)", "courts-act-39") in {(q.provision, q.source) for q in r.rests_on}
        assert not proposal.reviewed(r)
        assert courts.relation(proposal, "gh-sc", "gh", before=r.bound, matter="chieftaincy") == "unknown"
    gh = courts.CourtRegister.model_validate(reviewed(json.loads((courts.COURTS / "gh.json").read_text())))
    for house in houses:
        assert courts.relation(gh, "gh-sc", "gh", before=house, matter="chieftaincy") == "binding"
        assert courts.relation(gh, "gh-sc", "gh", before=house) == "unknown"  # no matter: no answer
        assert courts.relation(gh, "gh-sc", "gh", before=house, matter="land") == "unknown"
    bearings = {b.court: b.matter for b in courts.evidence_court(gh, "gh-sc", date(2026, 10, 21)).bears_on}
    assert {house: bearings[house] for house in houses} == dict.fromkeys(houses, "chieftaincy")
    assert bearings["gh-ca"] is None  # the Court of Appeal is bound on any matter
