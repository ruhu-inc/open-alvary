"""CPR-03: the consolidation applier substitutes, inserts, repeals, renumbers and amends words by locator,
in effective-date order up to the date asked, records every unit's provenance, and refuses what does
not match exactly."""

from datetime import date

import pytest

from workbench import publication
from workbench.consolidation import Instruction, Refused, apply

DOCUMENT = {"id": "ng-example-act-2023", "jurisdiction": "ng", "type": "act"}
FINAL = [
    {
        "page": 1,
        "units": [
            {"kind": "heading", "text": "EXAMPLE ACT, 2023"},
            {"kind": "paragraph", "text": "1. The Fund shall lend to students.", "notes": ["Loans"]},
            {"kind": "paragraph", "text": "2. A loan is repaid from salary.", "notes": ["Repayment"]},
            {"kind": "paragraph", "text": "3. The levy is payable monthly at the rate of one percent.", "notes": ["Levy"]},
            {"kind": "paragraph", "text": "4. The Minister may make regulations.", "notes": ["Regulations"]},
        ],
    }
]  # fmt: skip
BY = {
    "work": "ng-example-amendment-act-2024",
    "locator": "sec_1",
    "citation": "Example (Amendment) Act, 2024, s. 1",
}


def instruction(id_, operation, target, day="2024-03-01", **values):
    return Instruction.model_validate(
        {"id": id_, "operation": operation, "target": target, "effective_date": day, "amending": BY, **values}
    )


def text_of(result):
    return publication.build(DOCUMENT, result.pages).serialized.text


def locators(result):
    return [
        n.locator for n in publication.build(DOCUMENT, result.pages).structure.nodes if n.kind == "section"
    ]


def test_each_operation_by_locator_with_provenance():
    result = apply(
        DOCUMENT,
        FINAL,
        [
            instruction("i1", "substitute", "sec_2", units=[{"kind": "paragraph", "text": "2. A loan is repaid from wages.", "notes": ["Repayment"]}]),
            instruction("i2", "insert", "sec_2", position="after", units=[{"kind": "paragraph", "text": "2A. A loan may be deferred.", "notes": ["Deferral"]}]),
            instruction("i3", "amend_words", "sec_3", words_delete="one percent", words_insert="two percent"),
            instruction("i4", "repeal", "sec_4"),
        ],
        date(2024, 6, 1),
    )  # fmt: skip
    words = text_of(result)
    assert "repaid from wages" in words and "repaid from salary" not in words
    assert "2A. A loan may be deferred." in words and "two percent" in words and "one percent" not in words
    assert "4. [Repealed by Example (Amendment) Act, 2024, s. 1.]" in words
    assert locators(result) == ["sec_1", "sec_2", "sec_2a", "sec_3", "sec_4"]
    origins = {p["unit"]: p for p in result.provenance}
    assert origins[1]["origin"] == "original" and origins[2]["origin"] == "substituted"
    assert (
        origins[3]["origin"] == "inserted"
        and origins[4]["origin"] == "amended"
        and origins[5]["origin"] == "repealed"
    )
    assert origins[5]["by"] == BY and result.applied == ["i1", "i2", "i3", "i4"]


def test_only_instructions_in_force_on_the_date_apply_in_date_order():
    later = instruction(
        "i9", "amend_words", "sec_1", day="2025-01-01", words_delete="students", words_insert="learners"
    )
    earlier = instruction("i5", "renumber", "sec_4", day="2024-01-01", label_from="4.", label_to="5.")
    before = apply(DOCUMENT, FINAL, [later, earlier], date(2023, 12, 31))
    assert (
        before.applied == []
        and before.pending == ["i5", "i9"]
        and text_of(before) == text_of(apply(DOCUMENT, FINAL, [], date(2023, 1, 1)))
    )
    middle = apply(DOCUMENT, FINAL, [later, earlier], date(2024, 6, 1))
    assert middle.applied == ["i5"] and "sec_5" in locators(middle) and "learners" not in text_of(middle)
    assert "learners" in text_of(apply(DOCUMENT, FINAL, [later, earlier], date(2025, 6, 1)))


def test_what_does_not_match_exactly_is_refused():
    with pytest.raises(Refused, match="no provision sec_9"):
        apply(DOCUMENT, FINAL, [instruction("x1", "repeal", "sec_9")], date(2025, 1, 1))
    with pytest.raises(Refused, match="occurs 1 times in sec_1, not 2"):
        apply(
            DOCUMENT,
            FINAL,
            [instruction("x2", "amend_words", "sec_1", words_delete="Fund", occurrences=2)],
            date(2025, 1, 1),
        )
    with pytest.raises(Refused, match="not numbered"):
        apply(
            DOCUMENT,
            FINAL,
            [instruction("x3", "renumber", "sec_1", label_from="7.", label_to="8.")],
            date(2025, 1, 1),
        )
    shared = [{"page": 1, "units": [{"kind": "paragraph", "text": "5. (1) The Board is established.", "notes": ["Board"]},
                                    {"kind": "paragraph", "text": "(2) It has a seal."}]}]  # fmt: skip
    with pytest.raises(Refused, match="edit the units apart first"):
        apply(DOCUMENT, shared, [instruction("x4", "repeal", "sec_5__subsec_1")], date(2025, 1, 1))
    with pytest.raises(ValueError, match="missing what it needs"):
        instruction("x5", "insert", "sec_1", units=[{"kind": "paragraph", "text": "x"}])
