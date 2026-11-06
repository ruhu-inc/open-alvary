"""Structure rules for Nigeria's colonial and regional legislation of 1908–1964, as the bound volumes print it
(TXP-03, ng-colonial-1): a Region's Law, a Protectorate's Proclamation, and the legal notices, bye-laws and
instruments made under them.

The excerpts are invented: each keeps the shape of a kind of text the volumes hold (its cover lines, long
title, numbering and closing words) with its words replaced, so no official text is reproduced here. Add only
invented text."""

import pytest
from test_structure_families import resolve, spans_nest
from test_text_publication import build, by_locator, codes, head, para, span

from operations import publish
from workbench import publication, record_view, structure

# A Region's Law: the assent, the regnal year, the Governor's name and style with the long title after it, the
# enacting words, parts titled after their separators, sections with their margin notes, the Clerk's
# certificate.
REGIONAL_LAW = [
    [
        para("Assented to in Her Majesty's name this 2nd day of Plindor, 1957. Q. QUELL, Governor, Quelborn Region"),
        head("Quelborn Region of Nigeria"),
        head("IN THE SIXTH YEAR OF THE REIGN OF"),
        head("HER MAJESTY QUEEN ELIZABETH II"),
        para("SIR QUINTUS QUELL, K.C.M.G. Governor, Quelborn Region A LAW TO PROVIDE FOR THE CARE OF VASKETS"),
        para("[1st Grenmor, 1957.] Date of commencement. BE IT ENACTED by the Legislature of the Quelborn Region."),
        head("PART I.—INTRODUCTORY"),
        para("1. This Law may be cited as the Vasket Care Law, 1957.", notes=["Short title."]),
        para("2. In this Law “vasket” means a vasket of any kind.", notes=["Interpretation."]),
    ],
    [
        head("PART II.—CARE"),
        para("3. (1) Every owner shall care for his vaskets.", notes=["Duty of care."]),
        para("(2) An owner who fails to do so commits an offence."),
        para("This printed impression has been carefully compared by me with the Bill which has passed the "
             "Legislature, and found by me to be a true copy. Q. ZORN, Clerk of the Legislature"),
    ],
]  # fmt: skip


def test_a_regional_laws_cover_long_title_parts_sections_and_certificate():
    text, result = build(REGIONAL_LAW, rules=structure.NG_COLONIAL)
    nodes = by_locator(result)
    # The assent, the regnal year and the Governor's style are front matter; the long title after the style
    # opens the preamble, with the enacting words.
    assert span(text, result.root).startswith("Assented to in Her Majesty's name")
    assert span(text, nodes["preamble"]).startswith("SIR QUINTUS QUELL, K.C.M.G. Governor")
    assert span(text, nodes["preamble"]).endswith("BE IT ENACTED by the Legislature of the Quelborn Region.")
    assert (nodes["part_I"].label, nodes["part_I"].heading) == ("PART I", "INTRODUCTORY")
    assert nodes["sec_1"].heading == "Short title." and nodes["sec_3"].parent is nodes["part_II"]
    assert span(text, nodes["sec_3__subsec_2"]) == "(2) An owner who fails to do so commits an offence."
    # The Clerk's certificate is a notice, not words of section 3.
    assert span(text, nodes["notice_1"]).startswith("This printed impression has been carefully compared")
    assert codes(result) == []
    assert resolve(result, "s. 3(2)").locator == "sec_3__subsec_2"
    spans_nest(text, result)


PROCLAMATION = [
    [
        para("NO. 4 1910."),
        head("A PROCLAMATION."),
        para("Enacted by the Governor of Quelborn. Q. QUELL, Governor. A PROCLAMATION to regulate vaskets."),
        para("1. This Proclamation may be cited as the Vasket Proclamation, 1910."),
        para("2. No person shall export a vasket without a licence."),
        para("Short title of the next. 3. Any person contravening section 2 is liable to a fine."),
    ],
]  # fmt: skip


def test_a_proclamations_enacting_words_and_a_margin_note_in_its_words():
    text, result = build(PROCLAMATION, rules=structure.NG_COLONIAL)
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).startswith("A PROCLAMATION.\n\nEnacted by the Governor of Quelborn.")
    assert [n.locator for n in result.root.children if n.kind == "section"] == ["sec_1", "sec_2", "sec_3"]
    assert nodes["sec_3"].heading == "Short title of the next."
    assert codes(result) == ["margin_note_in_words"]


# A legal notice made under a Law: its number, the Law it is made under, its title, its commencement and the
# powers it is made under, its paragraphs, a schedule and the place and date it was given.
NOTICE = [
    [
        head("Q.R.L.N. 31 of 1957"),
        head("The Vasket Care Law, 1957 (No. 4 of 1957)"),
        head("VASKET CARE (EXEMPTION) NOTICE, 1957"),
        para("DATE OF COMMENCEMENT : 7TH PLINDOR, 1957 In exercise of the powers conferred by section 3 of the "
             "Vasket Care Law, 1957, the Minister makes the following Notice:—"),
        para("1. This Notice may be cited as the Vasket Care (Exemption) Notice, 1957."),
        para("2. The vaskets in the Schedule are exempt from section 3 of the Law."),
        head("SCHEDULE"),
        para("Every vasket of the Quelborn Museum."),
        para("GIVEN at Quelborn this 2nd day of Plindor, 1957. Q. ZORN, Secretary to the Executive Council"),
    ],
]  # fmt: skip


def test_a_legal_notices_title_commencement_schedule_and_date():
    text, result = build(NOTICE, rules=structure.NG_COLONIAL)
    nodes = by_locator(result)
    assert result.root.heading == "VASKET CARE (EXEMPTION) NOTICE, 1957"
    assert span(text, nodes["preamble"]).startswith("DATE OF COMMENCEMENT : 7TH PLINDOR, 1957 In exercise")
    assert [n.locator for n in result.root.children if n.kind == "section"] == ["sec_1", "sec_2"]
    assert span(text, nodes["sch_1"]) == "SCHEDULE\n\nEvery vasket of the Quelborn Museum."
    assert span(text, nodes["notice_1"]).startswith("GIVEN at Quelborn")
    assert codes(result) == []


@pytest.mark.parametrize(
    "kind, family, filed, document_type",
    [
        ("law", "legislation", "law", "act"),
        ("ordinance", "legislation", "ordinance", "act"),
        ("proclamation", "legislation", "proclamation", "act"),
        ("notice", "subsidiary_legislation", "legal_notice", "regulation"),
        ("bye_laws", "subsidiary_legislation", "by_law", "regulation"),
        ("instrument", "subsidiary_legislation", "statutory_instrument", "regulation"),
        ("declaration", "subsidiary_legislation", "legal_notice", "regulation"),
    ],
)
def test_a_volumes_kinds_have_rules_a_record_family_and_a_text_type(kind, family, filed, document_type):
    document = {"id": f"ng-qr-{kind}-1-1957", "jurisdiction": "ng", "type": kind}
    pages = [{"page": n, "units": units} for n, units in enumerate(REGIONAL_LAW, start=1)]
    built = publication.build(document, pages)
    assert built.rules_id == "ng-colonial-1" and not built.errors
    # A first draft of its details files it in its record family, and it publishes under that family's type.
    values, _ = record_view.from_legacy({"document_type": kind}, {})
    assert (values["identity.family"], values["identity.type"]) == (family, filed)
    assert publish.document_type(family, filed) == document_type
