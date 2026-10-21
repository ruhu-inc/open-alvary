"""Publishing approved works as catalogue records: the owner's decision gates it, and a validated
record maps to a catalogue entry without any text (operations.publish)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from operations import publish
from schema.models import RegionalEffect, Source, StatedDate
from schema.record import (
    Absent,
    Body,
    Classification,
    Dated,
    DocumentRecord,
    Event,
    Evidence,
    Expression,
    Identity,
    LanguageRule,
    LegislationProfile,
    Number,
    Place,
    Publication,
    Reference,
    RegionalProfile,
    Relation,
    Status,
    TextInfo,
    TreatyParty,
    TreatyProfile,
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


def test_every_form_a_work_is_cited_by_becomes_an_alias(decisions):
    decision = publish.decide("rw", "Ijidai Lassa", 12, [], "", "")
    identity = rwanda_law().identity.model_copy(
        update={
            "citations": (
                RecordCitation(kind="official", text="Law n° 055/2024"),
                RecordCitation(kind="popular", text="IP Law 2024"),
                RecordCitation(kind="other", text="LAW N° 055/2024"),  # the same form, written apart
            ),
            "alternative_titles": ("Intellectual Property Law",),
        }
    )
    item = publish.Approved("rw-law-055-2024", rwanda_law(identity=identity), ["en"], "A", datetime.now(UTC))
    found = publish.catalogue_entries(item, decision)["citations"]
    assert [(c.id, c.value) for c in found] == [
        ("citation-rw-law-055-2024", "Law n° 055/2024"),  # the ID the first alias always had
        ("citation-rw-law-055-2024-ip-law-2024", "IP Law 2024"),
        ("citation-rw-law-055-2024-intellectual-property-law", "Intellectual Property Law"),
    ]


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
        ("rw-law-5-2015", "cites", "rw-law-31-2009", True),
    ]
    assert all(str(a.evidence_url) == "https://www.minijust.gov.rw/official-gazette" for a in links)


def example_law(**changes) -> DocumentRecord:
    """A fictional law, to carry details no real one is said to have."""
    identity = Identity(
        family="legislation", type="law", work_uri="/akn/rw/act/2026/9", title="Example Law n° 009/2026"
    )
    return rwanda_law(identity=identity, **changes)


def test_each_versions_recorded_authority_and_the_language_rule_reach_the_catalogue(decisions):
    decision = publish.decide("rw", "Ijidai Lassa", 12, [], "", "")
    stated = Evidence(
        source="document", page=2, quote="This Law was drafted in English and adopted in Ikinyarwanda."
    )
    record = example_law(
        expressions=(
            Expression(language="rw", authority="authoritative", authority_evidence=stated, this_text=True),
            Expression(language="en", authority="official_translation"),
            Expression(language="fr"),  # nothing states it: unknown
        ),
        language_rule=LanguageRule(
            rule="Where the versions differ, the Kinyarwanda version prevails.",
            prevails="rw",
            provision="Article 9",
            evidence=stated,
        ),
    )

    def source(record):
        item = publish.Approved("rw-example-law", record, ["rw"], "Ijidai Lassa", datetime.now(UTC))
        return publish.catalogue_entries(item, decision)["source"]

    entry = source(record)
    assert [(e.language, e.authority) for e in entry.editions] == [
        ("rw", "authoritative"),
        ("en", "official_translation"),
    ]  # French is left out: the catalogue answers it as unknown
    assert entry.editions[0].evidence.page == 2 and entry.editions[1].evidence is None
    rule = entry.language_rule
    assert (rule.status, rule.prevails, rule.provision, rule.evidence.quote) == (
        "stated",
        "rw",
        "Article 9",
        stated.quote,
    )
    assert Source.model_validate(entry.model_dump(mode="json")) == entry
    # A copy that states no rule, a rule not yet known, and a record that states nothing.
    no_rule = source(example_law(absent={"language_rule": Absent(reason="not_in_source")}))
    assert no_rule.language_rule.status == "not_stated" and no_rule.language_rule.rule is None
    assert source(example_law(absent={"language_rule": Absent(reason="unknown")})).language_rule is None
    silent = source(example_law(expressions=(Expression(language="rw", this_text=True),)))
    assert silent.editions == [] and silent.language_rule is None
    # Not recorded, neither is in the row: rows stored before them read the same (DAT-08).
    assert not {"editions", "language_rule"} & set(silent.model_dump(mode="json"))


def test_reviewed_vocabulary_links_are_published_from_either_end_and_judicial_ones_reported(decisions):
    decision = publish.decide("ng", "Ijidai Lassa", 12, [], "", "")

    def item(work, *relations):
        return publish.Approved(
            work, rwanda_law(relations=relations), ["en"], "Ijidai Lassa", datetime.now(UTC)
        )

    def link(kind, work):
        return Relation(kind=kind, target=Reference(citation=f"Instrument {work}", work_uri=work))

    approved = [
        item("ng-act", link("transposes", "dir"), link("ratifies", "treaty"), link("cites", "order")),
        item("dir", link("implemented_by", "order")),
        item("treaty", link("domesticated_by", "ng-act")),
        item("order", link("commences", "ng-act"), link("overrules", "dir"), link("related", "treaty")),
        item("judgment", link("follows", "judgment-2"), link("appeal_from", "order")),
    ]
    sources = {a.work: publish.catalogue_entries(a, decision)["source"] for a in approved}
    links = publish.catalogue_links(approved, sources)
    assert [(a.source_id, a.kind, a.target_source_id) for a in links] == [
        ("ng-act", "cites", "order"),
        ("ng-act", "domesticates", "treaty"),  # stated by the treaty's record
        ("ng-act", "ratifies", "treaty"),
        ("ng-act", "transposes", "dir"),
        ("order", "commences", "ng-act"),
        ("order", "implements", "dir"),
    ]
    # Judicial treatment and appeal links wait for case law; nothing a record says is dropped unreported.
    withheld = {(w["work"], w["kind"], w["other"]): w["reason"] for w in publish.withheld_relations(approved)}
    assert set(withheld) == {
        ("order", "overrules", "dir"),
        ("order", "related", "treaty"),
        ("judgment", "follows", "judgment-2"),
        ("judgment", "appeal_from", "order"),
    }
    assert "case law" in withheld[("order", "overrules", "dir")]
    assert withheld[("order", "related", "treaty")] == publish.NOT_PUBLISHED


def regional_record(family, type_, place, uri, profile, events=()) -> DocumentRecord:
    """A fictional instrument of a regional body, to carry a legal effect or a party list."""
    return rwanda_law(
        identity=Identity(family=family, type=type_, work_uri=uri, title="Example instrument"),
        place=Place(code=place, name=place, level="regional"),
        events=events,
        classification=Classification(hierarchy="regional"),
        profile=profile,
    )


def test_a_regional_instruments_effect_and_a_treatys_parties_reach_the_catalogue(decisions):
    decision = publish.decide("rw", "Ijidai Lassa", 12, [], "", "")

    def source(work, record):
        item = publish.Approved(work, record, ["en"], "Ijidai Lassa", datetime.now(UTC))
        return publish.catalogue_entries(item, decision)["source"]

    directive = regional_record(
        "regional_instrument", "directive", "aa-ecowas", "/akn/aa-ecowas/act/directive/2008/1",
        RegionalProfile(
            family="regional_instrument", organ=Body(name="Council of Ministers", kind="regional_body"),
            legal_effect="requires_transposition", member_states=("sn", "ml"),
            transposition_deadline=date(2010, 12, 31),
        ),
    )  # fmt: skip
    found = source("ecowas-directive", directive)
    assert found.jurisdiction_id == "aa-ecowas" and found.treaty is None
    assert found.regional == RegionalEffect(
        organ="Council of Ministers", legal_effect="requires_transposition", member_states=["sn", "ml"],
        transposition_deadline=date(2010, 12, 31),
    )  # fmt: skip
    treaty = regional_record(
        "treaty", "charter", "aa-au", "/akn/aa-au/act/charter/2003/1",
        TreatyProfile(
            family="treaty", adopted_by=Body(name="Assembly of the Union", kind="international_organisation"),
            depositary="AU Commission", authentic_languages=("en", "fr"),
            parties=(
                TreatyParty(party="Kenya", place="ke", signed=Dated(value=date(2003, 1, 1)),
                            ratified=Dated(value=date(2005, 3, 1)), reservations="Article 4"),
                TreatyParty(party="Mali", place="ml", ratified=Dated(value=date(2004, 1, 1)),
                            denounced=Dated(value=date(2024, 1, 1), printed="1er janvier 2024")),
            ),
        ),
        events=(Event(kind="entered_into_force", when=Dated(value=date(2005, 1, 1))),),
    )  # fmt: skip
    status = source("au-charter", treaty).treaty
    assert status.adopted_by == "Assembly of the Union" and status.depositary == "AU Commission"
    assert status.entered_into_force == StatedDate(value=date(2005, 1, 1))
    assert [(p.place, p.ratified.value, p.reservations) for p in status.parties] == [
        ("ke", date(2005, 3, 1), "Article 4"), ("ml", date(2004, 1, 1), None),
    ]  # fmt: skip
    assert status.parties[1].denounced == StatedDate(value=date(2024, 1, 1), printed="1er janvier 2024")
    assert Source.model_validate(source("au-charter", treaty).model_dump(mode="json")).treaty == status
    # Two different entry-into-force dates for the whole treaty: none is chosen.
    later = Event(kind="entered_into_force", when=Dated(value=date(2006, 1, 1)))
    assert (
        source(
            "au-charter", treaty.model_copy(update={"events": (*treaty.events, later)})
        ).treaty.entered_into_force
        is None
    )
    # Any other record's row is unchanged: neither field is written (DAT-08).
    law = source("rw-example-law", example_law())
    assert law.regional is None and law.treaty is None
    assert not {"regional", "treaty"} & set(law.model_dump(mode="json"))


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
    built = publication.build({"id": "ng-example-act-2023", "jurisdiction": "ng", "type": "act"}, pages)
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
