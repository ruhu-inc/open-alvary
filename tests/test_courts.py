"""The court hierarchy register (CPR-04): synthetic registers only; a real court is entered from a
reviewed source, never from a test."""

import json
import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from api import courts

SOURCES = [
    {"id": "constitution", "title": "Constitution of Xland", "url": "https://example.org/constitution.pdf"},
    {"id": "practice", "title": "Practice Direction 3", "url": "https://example.org/pd3.pdf"},
]


def register(**changes) -> dict:
    data = {
        "jurisdiction": "xx",
        "review_state": "reviewed",
        "reviewed_by": "Test Reviewer",
        "reviewed_on": "2026-10-01",
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
    return data | changes


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
    draft = courts.CourtRegister.model_validate(register(review_state="unreviewed"))
    assert courts.relation(draft, "apex", "xx", before="high") == "unknown"
    silent = courts.CourtRegister.model_validate(register(precedent=None))
    assert courts.relation(silent, "apex", "xx", before="high") == "unknown"


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
        ({"reviewed_by": None}, "names its reviewer"),
        ({"sources": SOURCES[:1]}, "unknown sources"),
        ({"relations": [{"court": "apex", "bound": "mars", "relation": "binding", "sources": ["practice"]}]},
         "does not list"),
        ({"relations": [{"court": "apex", "relation": "binding", "sources": []}]}, "at least 1"),
        ({"relations": [{"court": "apex", "relation": "binding", "sources": ["practice"]},
                        {"court": "apex", "relation": "persuasive", "valid_from": "2000-01-01",
                         "sources": ["practice"]}]}, "apply at once"),
    ],
)  # fmt: skip
def test_an_entry_without_a_source_or_a_court_or_with_a_conflict_is_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        courts.CourtRegister.model_validate(register(**changes))


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


def test_every_register_written_and_the_readmes_example_are_valid():
    folder = Path(courts.COURTS)
    for path in folder.glob("*.json"):
        courts.load(path.stem)
    example = re.search(r"```json\n(.*?)```", (folder / "README.md").read_text(), re.S).group(1)
    example = courts.CourtRegister.model_validate(json.loads(example))
    assert example.review_state == "unreviewed" and courts.relation(example, "apex", "xx") == "unknown"
