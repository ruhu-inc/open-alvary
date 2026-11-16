"""Names as Nigerian cases and laws are cited (ingestion.cited_names): the convention agreed in review on
2026-11-16, tried on the names reviewed that day."""

import pytest

from ingestion import cited_names
from ingestion.cited_names import case_name, cited, cited_document


@pytest.mark.parametrize(
    ("printed", "as_cited"),
    [
        ("ABUGH TERSEER OTHNIEL", "Abugh Terseer Othniel"),
        ("THE INSPECTOR GENERAL OF POLICE", "The Inspector General of Police"),
        ("I.M.M. SAULAWA, JSC", "I.M.M. Saulawa, JSC"),
        ("O. F. OGBUINYA,JSC", "O. F. Ogbuinya,JSC"),
        ("J. K. AKERIGBA Esq", "J. K. Akerigba Esq"),
        ("GUARANTY TRUST BANK PLC", "Guaranty Trust Bank Plc"),
        ("CAR MARKET & ENERGY LTD", "Car Market & Energy Ltd"),
        ("MR. YINKA YUSUF", "Mr. Yinka Yusuf"),
        ("ATTORNEY-GENERAL OF THE FEDERATION", "Attorney-General of the Federation"),
        (
            "THE ABAJA-NGWO NATIVE AUTHORITY (PUBLIC SERVICES RATING) RULES, 1954",
            "The Abaja-Ngwo Native Authority (Public Services Rating) Rules, 1954",
        ),
        (
            "NATIONAL UNION OF PETROLEUM AND NATURAL GAS WORKERS (NUPENG)",
            "National Union of Petroleum and Natural Gas Workers (NUPENG)",
        ),
        ("NNPC LIMITED", "NNPC Limited"),  # no vowel: an acronym
        ("MCDONALD O'NEIL", "McDonald O'Neil"),
        ("THE COMPANY'S 2ND DEFENDANT", "The Company's 2nd Defendant"),
        ("CHIEF OKON EDET II", "Chief Okon Edet II"),
    ],
)
def test_a_name_printed_in_capitals_is_written_as_cases_are_cited(printed, as_cited):
    assert cited(printed) == (as_cited, [])


def test_a_name_typed_in_mixed_case_keeps_the_capitals_it_was_given():
    assert cited("Mr. Onyekachi Orji of ABC Ventures") == ("Mr. Onyekachi Orji of ABC Ventures", [])
    assert cited("") == ("", [])


def test_a_short_word_that_may_be_an_acronym_or_a_name_keeps_its_capitals_and_is_named():
    assert cited("ECM TERMINALS LIMITED") == ("ECM Terminals Limited", ["ECM"])
    # Known as words or names, they raise no doubt.
    assert cited("OJI RIVER OIL MILLS") == ("Oji River Oil Mills", [])


@pytest.mark.parametrize(
    ("printed", "as_cited"),
    [
        ("Demo Oseni Versus The State", "Demo Oseni v. The State"),
        (
            "OVERLAND AIRWAYS LIMITED V CAPTAIN JOSEPH GAMRA & ANOR",
            "Overland Airways Limited v. Captain Joseph Gamra & Anor",
        ),
        (
            "BARR. V. C. CHUKWUANI -VS- ATTORNEY-GENERAL OF ENUGU STATE",
            "Barr. V. C. Chukwuani v. Attorney-General of Enugu State",
        ),
        (
            "MR. TAOFEEK SULAIMON & 3 ORS-vs- OSUN STATE GOVERNMENT & 5 ORS",
            "Mr. Taofeek Sulaimon & 3 Ors v. Osun State Government & 5 Ors",
        ),
        (
            "Hon. Justice Idris Habib Shall -VS-National Judicial Council and 6 Oth",
            "Hon. Justice Idris Habib Shall v. National Judicial Council & 6 Ors",
        ),
        (
            "Umar Mukasa and 10 Others vs Federal Road Safety Commission and Another",
            "Umar Mukasa & 10 Ors v. Federal Road Safety Commission & Anor",
        ),
    ],
)
def test_a_case_name_has_its_sides_joined_by_v_and_its_other_parties_as_the_reports_write_them(
    printed, as_cited
):
    assert case_name(printed)[0] == as_cited


def test_the_other_parties_follow_how_many_each_side_has_where_the_parties_are_known():
    printed = "ABUGH TERSEER OTHNIEL -VS- THE NIGERIA POLICE FORCE & ORS"
    two = [{"role": "claimant"}, {"role": "defendant"}, {"role": "defendant"}]
    three = [*two, {"role": "defendant"}]
    assert case_name(printed, two)[0] == "Abugh Terseer Othniel v. The Nigeria Police Force & Anor"
    assert case_name(printed, three)[0] == "Abugh Terseer Othniel v. The Nigeria Police Force & Ors"
    assert case_name(printed)[0] == "Abugh Terseer Othniel v. The Nigeria Police Force & Ors"  # as printed


def nicn_document(**over):
    printed = "ABUGH TERSEER OTHNIEL -VS- THE NIGERIA POLICE FORCE & ORS"
    parties = [
        {
            "name": "ABUGH TERSEER OTHNIEL",
            "role": "claimant",
            "anonymised": "no",
            "represented_by": "J. K. AKERIGBA Esq",
        },
        {"name": "THE NIGERIA POLICE FORCE", "role": "defendant", "anonymised": "no"},
        {"name": "THE INSPECTOR GENERAL OF POLICE", "role": "defendant", "anonymised": "no"},
    ]
    full = "ABUGH TERSEER OTHNIEL (Claimant) v. THE NIGERIA POLICE FORCE and THE INSPECTOR GENERAL OF POLICE (Defendants)"
    values = {
        "identity.family": "judgment",
        "identity.title": printed,
        "identity.full_title": full,
        "profile.parties": parties,
        "profile.bench": [{"name": "E.N. AGBAKOBA", "role": "sole"}],
    }
    evidence = {
        "identity.title": {"value": printed, "page": None, "evidence": f"The page's title: {printed}"},
        "profile.parties": {"value": parties, "page": 1, "evidence": "BETWEEN …"},
        "profile.bench": {"value": values["profile.bench"], "page": 1, "evidence": "BEFORE HIS LORDSHIP"},
    }
    return {
        "id": "x",
        "jurisdiction": "ng",
        "language": "en",
        "suggested": values,
        "suggested_evidence": evidence,
        **over,
    }


def test_a_nigerian_documents_suggestions_are_read_as_cited_and_the_full_case_name_as_printed():
    document = cited_document(nicn_document())
    values, evidence = document["suggested"], document["suggested_evidence"]
    assert values["identity.title"] == "Abugh Terseer Othniel v. The Nigeria Police Force & Anor"
    assert [p["name"] for p in values["profile.parties"]] == [
        "Abugh Terseer Othniel",
        "The Nigeria Police Force",
        "The Inspector General of Police",
    ]
    assert values["profile.parties"][0]["represented_by"] == "J. K. Akerigba Esq"
    assert values["profile.bench"] == [{"name": "E.N. Agbakoba", "role": "sole"}]
    assert values["identity.full_title"].startswith("ABUGH TERSEER OTHNIEL (Claimant)")
    # The hint offers the cited form; its words stay as printed.
    assert evidence["identity.title"]["value"] == values["identity.title"]
    assert evidence["identity.title"]["evidence"].endswith("THE NIGERIA POLICE FORCE & ORS")
    assert evidence["profile.bench"]["value"] == values["profile.bench"]
    # Read again, nothing changes: a collection prepared since holds them as cited.
    assert cited_document(document) == document


def test_doubtful_words_are_named_on_their_field():
    document = nicn_document()
    document["suggested"]["profile.bench"] = [{"name": "ECM ADEBAYO", "role": "sole"}]
    document["suggested_evidence"]["profile.bench"]["value"] = document["suggested"]["profile.bench"]
    assert cited_document(document)["suggested_evidence"]["profile.bench"]["doubtful"] == ["ECM"]


def test_rwanda_and_other_languages_keep_their_own_rules():
    for over in ({"jurisdiction": "rw"}, {"language": "fr"}):
        document = nicn_document(**over)
        assert cited_document(document) == document


def test_the_acronyms_are_capitals_only():
    assert all(word.isupper() and word.isalpha() for word in cited_names.ACRONYMS)
