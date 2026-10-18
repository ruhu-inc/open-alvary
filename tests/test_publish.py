"""Publishing approved works as catalogue records: the owner's decision gates it, and a validated
record maps to a catalogue entry without any text (operations.publish)."""

from datetime import UTC, datetime, timedelta

import pytest

from operations import publish
from schema.record import (
    Body,
    Classification,
    DocumentRecord,
    Expression,
    Identity,
    LegislationProfile,
    Number,
    Place,
    Publication,
    Reference,
    Relation,
    Status,
    TextInfo,
    Version,
)
from schema.record import Citation as RecordCitation


def rwanda_law(**changes) -> DocumentRecord:
    fields = dict(
        identity=Identity(
            family="legislation",
            type="law",
            work_uri="/akn/rw/act/2024/55",
            title="Law n° 055/2024 on the protection of intellectual property",
            numbers=(Number(scheme="law", value="055/2024", printed="Law n° 055/2024"),),
            year=2024,
            citations=(RecordCitation(kind="official", text="Law n° 055/2024"),),
        ),
        place=Place(code="rw", name="Rwanda", level="national"),
        issued_by=(Body(name="Parliament", kind="legislature", id="parliament"),),
        expressions=(
            Expression(language="rw", authority="authoritative", this_text=True),
            Expression(language="en", authority="official_translation"),
            Expression(language="fr", authority="official_translation"),
        ),
        publications=(
            Publication(
                kind="gazette",
                name="Official Gazette of the Republic of Rwanda",
                url="https://www.minijust.gov.rw/official-gazette",
            ),
        ),
        status=Status(legal="in_force"),
        text=TextInfo(form="gazette_publication", version=Version(label="As published")),
        classification=Classification(hierarchy="primary"),
        profile=LegislationProfile(family="legislation"),
    )
    return DocumentRecord(**(fields | changes))


@pytest.fixture
def decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "DECISIONS", tmp_path)
    return tmp_path


def test_nothing_is_published_without_a_current_decision_from_a_named_person(decisions):
    with pytest.raises(publish.Refused, match="No catalogue decision"):
        publish.current_decision("rw")
    with pytest.raises(publish.Refused, match="a name, not an email"):
        publish.decide("rw", "owner@example.org", 12, [], "", "")
    first = publish.decide("rw", "Ijidai Lassa", 12, ["https://www.minijust.gov.rw/official-gazette"], "", "")
    assert first["revision"] == 1 and first["text"] == "not_included" and "055/2024" in first["legal_note"]
    assert publish.current_decision("rw")["decided_by"] == "Ijidai Lassa"
    assert publish.decide("rw", "Ijidai Lassa", 12, [], "", "")["revision"] == 2
    with pytest.raises(publish.Refused, match="expired"):
        publish.current_decision("rw", now=datetime.now(UTC) + timedelta(days=400))


def test_a_validated_record_maps_to_a_catalogue_entry_and_never_to_text(decisions):
    decision = publish.decide("rw", "Ijidai Lassa", 12, [], "", "")
    item = publish.Approved("rw-law-055-2024", rwanda_law(), ["en", "rw"], "Ijidai Lassa", datetime.now(UTC))
    entry = publish.catalogue_entries(item, decision)
    source, rights = entry["source"], entry["rights"]
    assert (
        source.id == "rw-law-055-2024" and source.document_type == "act" and source.legal_status == "current"
    )
    assert source.citation == "Law n° 055/2024" and source.language == "en" and source.year == 2024
    assert source.catalogue_note.startswith("Official texts: Kinyarwanda, English, French.")
    assert (
        entry["authority"].id == "rw-parliament"
        and str(entry["authority"].official_url) == "https://www.parliament.gov.rw/"
    )
    assert (
        rights.metadata_public and rights.approved_content_hashes == [] and rights.reviewer == "Ijidai Lassa"
    )
    assert rights.expires_at == datetime.fromisoformat(decision["expires_at"])
    without_link = publish.Approved("rw-x", rwanda_law(publications=()), ["rw"], "A", datetime.now(UTC))
    with pytest.raises(publish.Refused, match="no official publication link"):
        publish.catalogue_entries(without_link, decision)


def test_reviewed_links_between_published_works_become_checked_catalogue_links(decisions):
    decision = publish.decide("rw", "Ijidai Lassa", 12, [], "", "")

    def item(work, *relations):
        return publish.Approved(
            work, rwanda_law(relations=relations), ["en"], "Ijidai Lassa", datetime.now(UTC)
        )

    def link(kind, work):
        return Relation(kind=kind, target=Reference(citation=f"Law of {work}", work_uri=work))

    approved = [
        item(
            "rw-law-055-2024", link("repeals", "rw-law-31-2009"), link("made_under", "rw-law-not-published")
        ),
        item("rw-law-31-2009", link("repealed_by", "rw-law-055-2024"), link("amended_by", "rw-law-5-2015")),
        item("rw-law-5-2015", link("cites", "rw-law-31-2009")),
    ]
    sources = {a.work: publish.catalogue_entries(a, decision)["source"] for a in approved}
    links = publish.catalogue_links(approved, sources)
    assert [(a.source_id, a.kind, a.target_source_id, a.verified) for a in links] == [
        ("rw-law-055-2024", "repeals", "rw-law-31-2009", True),  # said by both: one link
        ("rw-law-5-2015", "amends", "rw-law-31-2009", True),
    ]
    assert all(str(a.evidence_url) == "https://www.minijust.gov.rw/official-gazette" for a in links)


# --- texts (TXP-07) ----------------------------------------------------------------------------------

TEXT_DECISION = dict(
    types=["act"],
    rights_holder="Federal Republic of Nigeria",
    licence="Not eligible for copyright (Copyright Act 2022, s. 3(b))",
    licence_url=None,
    attribution=None,
    database_rights="",
    basis="Official legislative texts are not eligible for copyright in Nigeria.",
    privacy_checked=True,
)
EVIDENCE = ["https://www.copyright.gov.ng/copyright-act-2022"]


def test_no_text_is_published_without_a_complete_current_text_decision(decisions):
    with pytest.raises(publish.Refused, match="No text decision"):
        publish.current_text_decision("ng")
    with pytest.raises(publish.Refused, match="a name, not an email"):
        publish.decide_text("ng", "owner@example.org", 12, EVIDENCE, **TEXT_DECISION)
    with pytest.raises(publish.Refused, match="--evidence"):
        publish.decide_text("ng", "Ijidai Lassa", 12, [], **TEXT_DECISION)
    with pytest.raises(publish.Refused, match="personal data"):
        publish.decide_text(
            "ng", "Ijidai Lassa", 12, EVIDENCE, **(TEXT_DECISION | {"privacy_checked": False})
        )
    with pytest.raises(publish.Refused, match="kinds of document"):
        publish.decide_text("ng", "Ijidai Lassa", 12, EVIDENCE, **(TEXT_DECISION | {"types": ["novel"]}))
    decision = publish.decide_text("ng", "Ijidai Lassa", 12, EVIDENCE, **TEXT_DECISION)
    assert decision["operation"] == "publish_full_text" and decision["document_types"] == ["act"]
    assert publish.current_text_decision("ng")["decided_by"] == "Ijidai Lassa"
    with pytest.raises(publish.Refused, match="expired"):
        publish.current_text_decision("ng", now=datetime.now(UTC) + timedelta(days=400))


def test_a_decision_with_placeholders_is_refused_when_recorded_and_when_read(decisions):
    with pytest.raises(publish.Refused, match="placeholder"):
        publish.decide_text("ng", "Your Full Name", 12, EVIDENCE, **TEXT_DECISION)
    for link in ("https://LINK-TO-THE-COPYRIGHT-ACT", "https://example.org/act", "http://www.gov.ng/act"):
        with pytest.raises(publish.Refused, match="real https address"):
            publish.decide_text("ng", "Ijidai Lassa", 12, [link], **TEXT_DECISION)
    with pytest.raises(publish.Refused, match="--licence"):
        publish.decide_text("ng", "Ijidai Lassa", 12, EVIDENCE, **(TEXT_DECISION | {"licence": "<licence>"}))
    with pytest.raises(publish.Refused, match="placeholder"):
        publish.decide("ng", "Full Name", 12, [], "", "")
    # A decision written before these checks, or edited by hand, is refused when publishing reads it.
    publish.decide_text("ng", "Ijidai Lassa", 12, EVIDENCE, **TEXT_DECISION)
    path = publish.text_decision_path("ng")
    path.write_text(path.read_text().replace("Ijidai Lassa", "Your Full Name"))
    with pytest.raises(publish.Refused, match="not a usable decision"):
        publish.current_text_decision("ng")


def catalogue_with_record(store):
    from schema.models import Authority, Corpus, Jurisdiction, Language, RightsRecord, Source

    store.save(
        Corpus(
            jurisdictions=[Jurisdiction(id="ng", name="Nigeria", country_code="NG", languages=["en"])],
            authorities=[
                Authority(
                    id="ng-nass",
                    jurisdiction_id="ng",
                    name="National Assembly",
                    kind="legislature",
                    official_url="https://nass.gov.ng/",
                )
            ],
            languages=[Language(id="en", name="English", tag="en")],
            sources=[
                Source(
                    id="ng-example-act-2023",
                    jurisdiction_id="ng",
                    authority_id="ng-nass",
                    title="Example Act, 2023",
                    document_type="act",
                    canonical_url="https://example.org/example-act.pdf",
                    citation="Example Act, 2023",
                    catalogue_note="Text not republished.",
                )
            ],
            rights=[
                RightsRecord(
                    id="rights-ng-example-act-2023",
                    source_id="ng-example-act-2023",
                    metadata_public=True,
                    metadata_basis="Independently authored catalogue entry",
                    reviewer="Ijidai Lassa",
                    reviewed_at=datetime.now(UTC) - timedelta(days=1),
                )
            ],
        )
    )


def approved_text(words="The Fund shall lend to students.", raw="7" * 64):
    from workbench import publication

    pages = [
        {
            "page": 1,
            "units": [
                {"kind": "heading", "text": "EXAMPLE ACT, 2023"},
                {"kind": "paragraph", "text": "Arrangement of Sections"},
                {"kind": "list_item", "marker": "1.", "text": "Loans"},
                {"kind": "paragraph", "text": "ENACTED by the National Assembly —"},
                {"kind": "paragraph", "text": f"1. (1) {words}", "notes": ["Loans"]},
                {"kind": "paragraph", "text": "(2) Repayment starts after study."},
            ],
        }
    ]
    built = publication.build({"id": "ng-example-act-2023", "jurisdiction": "ng"}, pages)
    return publish.ApprovedText(
        document_id="ng-example-act-2023",
        work="ng-example-act-2023",
        language="en",
        document_type="act",
        text=built,
        approver="Ijidai Lassa",
        approved_at=datetime.now(UTC) - timedelta(minutes=5),
        provenance={
            "url": "https://example.org/example-act.pdf",
            "retrieved_at": "2026-10-09T06:41:17+00:00",
            "sha256": raw,
        },
    )


def test_an_approved_text_is_published_with_its_structure_rights_and_lineage(decisions, store):
    from rights.policy import decide, public_corpus

    catalogue_with_record(store)
    decision = publish.decide_text("ng", "Ijidai Lassa", 12, EVIDENCE, **TEXT_DECISION)
    first = publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text()]), decision)
    assert [entry["document"] for entry in first["published"]] == ["ng-example-act-2023"]
    corpus = store.load()
    (version,) = corpus.versions
    assert version.serialization_profile == "oa-text-1/workbench-units-1" and version.prior_version_id is None
    assert version.raw_hash == "7" * 64 and version.content_verifier == "Ijidai Lassa"
    rights = corpus.rights[0]
    assert decide(rights, version).full_text and rights.approved_content_hashes == [version.content_hash]
    assert public_corpus(corpus).versions == [version]
    nodes = {n.locator: n for n in corpus.structure}
    section = nodes["sec_1"]
    assert (section.label, section.heading, section.page) == ("1.", "Loans", 1)
    raw = version.canonical_text.encode()
    assert raw[nodes["sec_1__subsec_2"].start_byte : nodes["sec_1__subsec_2"].end_byte].startswith(b"(2)")
    assert nodes["contents__entry_1"].target_node_id == section.id
    assert corpus.sources[0].catalogue_note.startswith("Text published from the official file after review")
    # Nothing new: the same bytes and structure are left as they are.
    again = publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text()]), decision)
    assert again["unchanged"] == ["ng-example-act-2023"] and len(store.load().versions) == 1
    # A correction from the same original file is a new version naming the one it corrects.
    publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text("The Fund lends.")]), decision)
    corrected = next(v for v in store.load().versions if v.id != version.id)
    assert corrected.prior_version_id == version.id and corrected.raw_hash == version.raw_hash
    # A revoked approval withdraws the work's reviewed texts.
    revoked = publish.publish_texts(
        store, "ng", publish.Texts(revoked={("ng-example-act-2023", "en")}), decision
    )
    assert revoked["withdrawn"] == ["ng-example-act-2023 (en)"]
    corpus = store.load()
    assert corpus.rights[0].approved_content_hashes == [] and public_corpus(corpus).versions == []


def test_texts_outside_the_decision_or_without_a_record_are_skipped(decisions, store):
    decision = publish.decide_text("ng", "Ijidai Lassa", 12, EVIDENCE, **TEXT_DECISION)
    no_record = publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text()]), decision)
    assert "run `records` first" in no_record["skipped"]["ng-example-act-2023"]
    catalogue_with_record(store)
    judgment = approved_text()
    judgment.document_type = "judgment"
    outside = publish.publish_texts(store, "ng", publish.Texts(approved=[judgment]), decision)
    assert "does not cover judgments" in outside["skipped"]["ng-example-act-2023"]
    assert store.load().versions == []


def test_the_change_log_names_why_a_later_version_exists():
    from changes.record import change_reason

    assert change_reason({"raw_hash": "a"}, {"raw_hash": "a"}) == "extraction_correction"
    assert change_reason({"raw_hash": "b"}, {"raw_hash": "a"}) == "source_revision"
    assert change_reason({"raw_hash": "a"}, None) == "unknown"
