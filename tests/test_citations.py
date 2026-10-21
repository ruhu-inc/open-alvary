"""A judgment's citations (EDT-01, DAT-06): proposed from its published paragraphs with the catalogue's
own resolver, never guessed; a citations file holds only reviewed ones, and a treatment only from a
doctrinal review that states the reviewer's qualification."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_judgment_structure import JUDGMENT, head, para

from api.catalogue import LegacyCatalogue
from api.repositories import LegacyRepository
from operations import citations, publish
from schema.models import Authority, Corpus, Jurisdiction, Language, RightsRecord, Source
from workbench import publication

ACT = "ng-ndpa-2023"
CITING = "ng-sc-2024-7"
CITED = "ng-sc-2019-4"
REVIEWED = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
ACT_UNITS = [
    {"kind": "heading", "text": "NIGERIA DATA PROTECTION ACT, 2023"},
    {"kind": "paragraph", "text": "1. The objective of this Act is to protect personal data.", "notes": ["Objective"]},
    {"kind": "paragraph", "text": "2. (1) This Act applies to the processing of personal data.", "notes": ["Application"]},
    {"kind": "paragraph", "text": "(2) It does not apply to processing for household purposes."},
]  # fmt: skip
CITING_UNITS = [
    head("IN THE SUPREME COURT OF NIGERIA"),
    para("SC. 77/2020"),
    head("LEAD JUDGMENT DELIVERED BY JOHN DOE, JSC"),
    para("[1] This appeal concerns the processing of personal data by a bank."),
    para(
        "[2] Under section 2(1) of the Nigeria Data Protection Act 2023, the Act applies to the processing "
        "of personal data. The bank processed the appellant's data."
    ),
    para(
        "[3] In Bello v. The State [2019] NGSC 4 at [4], this court held that the first issue was resolved "
        "against the appellant. That decision is overruled."
    ),
    para("[4] The appeal is allowed. See also s. 9 of the Unknown Act 1999."),
]
COURT = "https://example.org/court/"


def published(store, work, title, citation, document_type, units):
    """A public record and its reviewed text (its units, or its pages' units), published through
    operations.publish."""
    authority = "ng-sc" if document_type == "judgment" else "ng-nass"
    store.save(
        Corpus(
            jurisdictions=[Jurisdiction(id="ng", name="Nigeria", country_code="NG", languages=["en"])],
            authorities=[
                Authority(
                    id=authority, jurisdiction_id="ng", kind="court" if authority == "ng-sc" else "legislature",
                    name="Example Supreme Court" if authority == "ng-sc" else "National Assembly",
                    official_url=COURT,
                )
            ],
            languages=[Language(id="en", name="English", tag="en")],
            sources=[
                Source(
                    id=work, jurisdiction_id="ng", authority_id=authority, title=title,
                    document_type=document_type, canonical_url=f"{COURT}{work}.pdf", citation=citation,
                    language="en",
                )
            ],
            rights=[
                RightsRecord(
                    id=f"rights-{work}", source_id=work, metadata_public=True,
                    metadata_basis="Independently authored catalogue entry",
                )
            ],
        )
    )  # fmt: skip
    now = datetime.now(UTC).replace(microsecond=0)
    decision = {
        "document_types": [document_type],
        "rights_holder": "Synthetic test rights holder",
        "licence": "Synthetic test decision",
        "licence_url": None,
        "attribution_required": False,
        "attribution_text": None,
        "database_rights_notes": "None claimed.",
        "privacy_status": "cleared",
        "basis": "Synthetic test text.",
        "evidence": ["https://example.org/decision"],
        "decided_by": "Test Reviewer",
        "decided_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=365)).isoformat(),
    }
    pages = units if isinstance(units[0], list) else [units]
    text = publication.build(
        {"id": work, "jurisdiction": "ng", "type": document_type},
        [{"page": n, "units": page} for n, page in enumerate(pages, start=1)],
    )
    item = publish.ApprovedText(
        document_id=work, work=work, language="en", document_type=document_type, text=text,
        approver="Test Reviewer", approved_at=now - timedelta(minutes=5),
        provenance={"url": f"{COURT}{work}.pdf", "retrieved_at": "2026-10-09T06:41:17+00:00", "sha256": "7" * 64},
    )  # fmt: skip
    report = publish.publish_texts(store, "ng", publish.Texts(approved=[item]), decision)
    assert report["published"], report
    return {n.locator: n for n in store.load().structure if n.source_id == work}


def case_law(store):
    """An Act, a judgment, and a later judgment citing both, each public with its text."""
    published(
        store, ACT, "Nigeria Data Protection Act 2023", "Nigeria Data Protection Act 2023", "act", ACT_UNITS
    )
    published(store, CITED, "Bello v. The State", "[2019] NGSC 4", "judgment", JUDGMENT)
    return published(store, CITING, "Adeyemi v. Example Bank", "[2024] NGSC 7", "judgment", CITING_UNITS)


def proposals(store):
    with store.engine.begin() as conn:
        return citations.propose(conn, CITING)


def test_a_paragraph_citing_a_section_of_an_act_proposes_the_section_it_resolves_to(store):
    case_law(store)
    found = proposals(store)
    assert found["work"] == CITING and found["rules"] == "citations-1"
    by_paragraph = {p["citing_locator"]: p for p in found["proposals"]}
    act = by_paragraph["para_2"]
    assert act["cited_work"] == ACT and act["cited_locator"] == "sec_2__subsec_1"
    assert act["cited_pinpoint"] == "s. 2(1)" and act["citing_pinpoint"] == "[2]"
    assert act["printed"] == "section 2(1) of the Nigeria Data Protection Act 2023"
    assert act["quote"].startswith("[2] Under section 2(1)") and act["cited_document_type"] == "act"
    assert act["evidence_url"] == f"{COURT}{CITING}.pdf" and act["unresolved_pinpoint"] is None
    # A neutral citation with its paragraph: the cited judgment's paragraph.
    judgment = by_paragraph["para_3"]
    assert judgment["cited_work"] == CITED and judgment["cited_locator"] == "para_4"
    assert judgment["printed"] == "[2019] NGSC 4 at [4]" and judgment["cited_document_type"] == "judgment"
    # A work the catalogue does not hold is never guessed; nothing is proposed for it.
    assert "para_4" not in by_paragraph and len(found["proposals"]) == 2


def test_a_pinpoint_that_names_no_part_proposes_the_work_and_says_why(store):
    case_law(store)
    words = b"[1] As s. 99 of the Nigeria Data Protection Act 2023 provides."
    nodes = [
        {"kind": "paragraph", "locator": "para_1", "start_byte": 0, "end_byte": len(words), "pinpoint": "[1]"}
    ]
    with store.engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        [found] = citations.propose_from(words, nodes, CITING, f"{COURT}{CITING}.pdf", service.citations)
    assert found["cited_work"] == ACT and found["cited_locator"] is None
    assert found["unresolved_pinpoint"] == "not_found"


def test_each_provision_a_sentence_cites_is_proposed_with_its_own_part(store):
    case_law(store)
    words = (
        b"[1] Section 1 of the Nigeria Data Protection Act 2023 states its objective and "
        b"Nigeria Data Protection Act 2023, s. 2(2) excludes household processing."
    )
    nodes = [
        {"kind": "paragraph", "locator": "para_1", "start_byte": 0, "end_byte": len(words), "pinpoint": "[1]"}
    ]
    with store.engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        found = citations.propose_from(words, nodes, CITING, f"{COURT}{CITING}.pdf", service.citations)
    assert [(p["cited_locator"], p["printed"]) for p in found] == [
        ("sec_1", "Section 1 of the Nigeria Data Protection Act 2023"),
        ("sec_2__subsec_2", "Nigeria Data Protection Act 2023, s. 2(2)"),
    ]


def test_a_long_paragraphs_proposal_quotes_the_sentences_around_its_citation():
    words = " ".join(["The facts are long."] * 80) + " Section 2(1) of the Act applies. " + "So it is. " * 40
    start = words.index("Section 2(1)")
    quote = citations._passage(words, start, start + len("Section 2(1) of the Act"))
    assert "Section 2(1) of the Act applies." in quote and len(quote) <= citations.MAX_QUOTE
    assert quote in words and quote == quote.strip()


def a_citation(**values):
    return {
        "id": "cit_00000000-0000-4000-8000-000000000001", "citing_work": CITING, "citing_locator": "para_3",
        "cited_work": CITED, "cited_locator": "para_4", "printed": "[2019] NGSC 4 at [4]",
        "quote": "That decision is overruled.", "evidence_url": f"{COURT}{CITING}.pdf",
        "reviewed_by": "Test Reviewer", "reviewer_role": "owner", "reviewed_at": REVIEWED.isoformat(), **values,
    }  # fmt: skip


def a_treatment(**values):
    return {
        "id": "trt_00000000-0000-4000-8000-000000000001", "kind": "overrules", "opinion": "majority",
        "quote": "That decision is overruled.", "reviewed_by": "Test Advocate", "reviewer_role": "Nigerian advocate",
        "reviewer_qualification": "Barrister and Solicitor of the Supreme Court of Nigeria",
        "reviewed_at": REVIEWED.isoformat(), **values,
    }  # fmt: skip


def test_a_treatment_without_the_reviewers_qualification_is_refused():
    citations.CitationFile.model_validate({"citations": [a_citation(treatments=[a_treatment()])]})
    for missing in ({"reviewer_qualification": ""}, {"reviewer_qualification": "  "}):
        with pytest.raises(ValidationError, match="qualification"):
            citations.CitationFile.model_validate(
                {"citations": [a_citation(treatments=[a_treatment(**missing)])]}
            )
    unqualified = a_treatment()
    del unqualified["reviewer_qualification"]
    with pytest.raises(ValidationError, match="reviewer_qualification"):
        citations.CitationFile.model_validate({"citations": [a_citation(treatments=[unqualified])]})
    with pytest.raises(ValidationError, match="kind"):
        citations.CitationFile.model_validate(
            {"citations": [a_citation(treatments=[a_treatment(kind="good_law")])]}
        )


def test_a_citations_file_holds_reviewed_citations_one_live_treatment_each():
    with pytest.raises(ValidationError, match="reviewer_role"):
        citations.CitationFile.model_validate({"citations": [a_citation(reviewer_role="")]})
    with pytest.raises(ValidationError, match="another work"):
        citations.CitationFile.model_validate({"citations": [a_citation(cited_work=CITING)]})
    second = a_treatment(id="trt_00000000-0000-4000-8000-000000000002", kind="doubts")
    with pytest.raises(ValidationError, match="one reviewed treatment"):
        citations.CitationFile.model_validate({"citations": [a_citation(treatments=[a_treatment(), second])]})
    withdrawn = a_citation(review_state="withdrawn", treatments=[a_treatment()])
    with pytest.raises(ValidationError, match="withdrawn too"):
        citations.CitationFile.model_validate({"citations": [withdrawn]})
