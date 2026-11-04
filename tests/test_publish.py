"""Publishing approved works as catalogue records: the owner's decision gates it, and a validated
record maps to a catalogue entry without any text (operations.publish)."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from operations import publish
from schema.models import RegionalEffect, Source, StatedDate
from schema.record import (
    FAMILIES,
    Absent,
    Body,
    Classification,
    Court,
    Dated,
    DocumentRecord,
    Event,
    Evidence,
    ExecutiveProfile,
    Expression,
    Identity,
    JudgmentProfile,
    LanguageRule,
    LegislationProfile,
    Number,
    Party,
    Place,
    Publication,
    Reference,
    RegionalProfile,
    Relation,
    Status,
    Summary,
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
    first = publish.decide("rw", "Ada Reviewer", 12, ["https://www.minijust.gov.rw/official-gazette"], "", "")
    assert first["revision"] == 1 and first["text"] == "not_included" and "055/2024" in first["legal_note"]
    assert publish.current_decision("rw")["decided_by"] == "Ada Reviewer"
    assert publish.decide("rw", "Ada Reviewer", 12, [], "", "")["revision"] == 2
    with pytest.raises(publish.Refused, match="expired"):
        publish.current_decision("rw", now=datetime.now(UTC) + timedelta(days=400))


def test_a_validated_record_maps_to_a_catalogue_entry_and_never_to_text(decisions):
    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")
    item = publish.Approved("rw-law-055-2024", rwanda_law(), ["en", "rw"], "Ada Reviewer", datetime.now(UTC))
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
        rights.metadata_public and rights.approved_content_hashes == [] and rights.reviewer == "Ada Reviewer"
    )
    assert rights.expires_at == datetime.fromisoformat(decision["expires_at"])
    without_link = publish.Approved("rw-x", rwanda_law(publications=()), ["rw"], "A", datetime.now(UTC))
    with pytest.raises(publish.Refused, match="no official publication link"):
        publish.catalogue_entries(without_link, decision)


def test_each_family_has_its_catalogue_document_type(decisions):
    # Every family named is one a record has: a misspelt one would catalogue its records as "other".
    assert set(publish.DOCUMENT_TYPES) <= set(FAMILIES)
    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")
    order = rwanda_law(
        identity=Identity(
            family="executive_instrument", type="presidential_order", work_uri="/akn/rw/act/2026/9",
            title="Example Presidential Order n° 009/01",
        ),
        profile=ExecutiveProfile(family="executive_instrument", authority=Body(name="President", kind="head_of_state")),
    )  # fmt: skip
    item = publish.Approved("rw-example-order", order, ["rw"], "Ada Reviewer", datetime.now(UTC))
    assert publish.catalogue_entries(item, decision)["source"].document_type == "regulation"


def test_every_form_a_work_is_cited_by_becomes_an_alias(decisions):
    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")
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
    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")

    def item(work, *relations):
        return publish.Approved(
            work, rwanda_law(relations=relations), ["en"], "Ada Reviewer", datetime.now(UTC)
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
    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")
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
        item = publish.Approved("rw-example-law", record, ["rw"], "Ada Reviewer", datetime.now(UTC))
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
    decision = publish.decide("ng", "Ada Reviewer", 12, [], "", "")

    def item(work, *relations):
        return publish.Approved(
            work, rwanda_law(relations=relations), ["en"], "Ada Reviewer", datetime.now(UTC)
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
    # Judicial treatment comes from a judgment's reviewed citations, never from its details, and appeal
    # history waits for its own review; nothing a record says is dropped unreported.
    withheld = {(w["work"], w["kind"], w["other"]): w["reason"] for w in publish.withheld_relations(approved)}
    assert set(withheld) == {
        ("order", "overrules", "dir"),
        ("order", "related", "treaty"),
        ("judgment", "follows", "judgment-2"),
        ("judgment", "appeal_from", "order"),
    }
    assert "reviewed citations" in withheld[("order", "overrules", "dir")]
    assert "operations.citations" in withheld[("judgment", "follows", "judgment-2")]
    assert "appeal history" in withheld[("judgment", "appeal_from", "order")]
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
    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")

    def source(work, record):
        item = publish.Approved(work, record, ["en"], "Ada Reviewer", datetime.now(UTC))
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
            parties_complete_as_of=date(2026, 6, 1),
            parties_complete_evidence=Evidence(source="external", url="https://www.example.org/status-list.pdf"),
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
    assert status.parties_complete_as_of == date(2026, 6, 1)
    assert str(status.parties_complete_evidence.url) == "https://www.example.org/status-list.pdf"
    assert Source.model_validate(source("au-charter", treaty).model_dump(mode="json")).treaty == status
    # Two different entry-into-force dates for the whole treaty: none is chosen.
    later = Event(kind="entered_into_force", when=Dated(value=date(2006, 1, 1)))
    assert (
        source(
            "au-charter", treaty.model_copy(update={"events": (*treaty.events, later)})
        ).treaty.entered_into_force
        is None
    )
    # A party list not recorded as complete: neither field is in the row (DAT-08).
    open_list = treaty.model_copy(
        update={"profile": treaty.profile.model_copy(update={"parties_complete_as_of": None,
                                                             "parties_complete_evidence": None})}
    )  # fmt: skip
    written = source("au-charter", open_list).model_dump(mode="json")["treaty"]
    assert not {"parties_complete_as_of", "parties_complete_evidence"} & set(written)
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
        publish.decide_text("ng", "Ada Reviewer", 12, [], **TEXT_DECISION)
    with pytest.raises(publish.Refused, match="personal data"):
        publish.decide_text(
            "ng", "Ada Reviewer", 12, EVIDENCE, **(TEXT_DECISION | {"privacy_checked": False})
        )
    with pytest.raises(publish.Refused, match="kinds of document"):
        publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **(TEXT_DECISION | {"types": ["novel"]}))
    decision = publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    assert decision["operation"] == "publish_full_text" and decision["document_types"] == ["act"]
    assert publish.current_text_decision("ng")["decided_by"] == "Ada Reviewer"
    with pytest.raises(publish.Refused, match="expired"):
        publish.current_text_decision("ng", now=datetime.now(UTC) + timedelta(days=400))


def test_a_decision_with_placeholders_is_refused_when_recorded_and_when_read(decisions):
    with pytest.raises(publish.Refused, match="placeholder"):
        publish.decide_text("ng", "Your Full Name", 12, EVIDENCE, **TEXT_DECISION)
    for link in ("https://LINK-TO-THE-COPYRIGHT-ACT", "https://example.org/act", "http://www.gov.ng/act"):
        with pytest.raises(publish.Refused, match="real https address"):
            publish.decide_text("ng", "Ada Reviewer", 12, [link], **TEXT_DECISION)
    with pytest.raises(publish.Refused, match="--licence"):
        publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **(TEXT_DECISION | {"licence": "<licence>"}))
    with pytest.raises(publish.Refused, match="placeholder"):
        publish.decide("ng", "Full Name", 12, [], "", "")
    # A decision written before these checks, or edited by hand, is refused when publishing reads it.
    publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    path = publish.text_decision_path("ng")
    path.write_text(path.read_text().replace("Ada Reviewer", "Your Full Name"))
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
                    reviewer="Ada Reviewer",
                    reviewed_at=datetime.now(UTC) - timedelta(days=1),
                )
            ],
        )
    )


def approved_text(words="The Fund shall lend to students.", raw="7" * 64, after=()):
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
                *after,
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
        approver="Ada Reviewer",
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
    decision = publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    first = publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text()]), decision)
    assert [entry["document"] for entry in first["published"]] == ["ng-example-act-2023"]
    corpus = store.load()
    (version,) = corpus.versions
    assert version.serialization_profile == "oa-text-1/workbench-units-1" and version.prior_version_id is None
    assert version.raw_hash == "7" * 64 and version.content_verifier == "Ada Reviewer"
    rights = corpus.rights[0]
    assert decide(rights, version).full_text and rights.approved_content_hashes == [version.content_hash]
    assert public_corpus(corpus).versions == [version]
    nodes = {n.locator: n for n in corpus.structure}
    section = nodes["sec_1"]
    assert (section.label, section.heading, section.page) == ("1.", "Loans", 1)
    raw = version.canonical_text.encode()
    assert raw[nodes["sec_1__subsec_2"].start_byte : nodes["sec_1__subsec_2"].end_byte].startswith(b"(2)")
    assert nodes["contents__entry_1"].target_node_id == section.id
    assert corpus.sources[0].catalogue_note.startswith(publish.TEXT_NOTE)
    # Nothing new: the same bytes and structure are left as they are.
    again = publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text()]), decision)
    assert again["unchanged"] == ["ng-example-act-2023"] and len(store.load().versions) == 1
    # A correction from the same original file is a new version naming the one it corrects.
    publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text("The Fund lends.")]), decision)
    corrected = next(v for v in store.load().versions if v.id != version.id)
    assert corrected.prior_version_id == version.id and corrected.raw_hash == version.raw_hash
    # It replaces the reading it corrects; what is released no longer names that one.
    assert [(v.id, v.prior_version_id) for v in public_corpus(store.load()).versions] == [
        (corrected.id, None)
    ]
    # A revoked approval withdraws the work's reviewed texts.
    texts = publish.Texts(bound={("ng-example-act-2023", "en"): [publish.Bound(why="revoked")]})
    revoked = publish.publish_texts(store, "ng", texts, decision)
    assert revoked["withdrawn"] == ["ng-example-act-2023 (en)"]
    corpus = store.load()
    assert corpus.rights[0].approved_content_hashes == [] and public_corpus(corpus).versions == []


CERTIFIED = {
    "kind": "paragraph",
    "text": "I, CERTIFY, IN ACCORDANCE WITH SECTION 2 (1) OF THE ACTS AUTHENTICATION ACT.",
}
CERTIFICATE = {
    "kind": "table",
    "caption": "",
    "header_rows": 1,
    "rows": [["SHORT TITLE", "SUMMARY"], ["Example", ""]],
}


def test_a_marked_cell_in_a_notice_is_published_on_its_tables_node(decisions, store):
    catalogue_with_record(store)
    decision = publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    marked = approved_text(after=[CERTIFIED, {**CERTIFICATE, "cell_states": {"1,1": "unsupported"}}])
    assert not marked.text.errors and [w.code for w in marked.text.warnings] == ["cell_state"]
    publish.publish_texts(store, "ng", publish.Texts(approved=[marked]), decision)
    (version,) = store.load().versions
    assert version.serialization_profile == "oa-text-1/workbench-units-2"
    assert version.canonical_text.endswith("SHORT TITLE\tSUMMARY\nExample\t\n")  # no placeholder
    tables = [n for n in store.load().structure if n.kind == "table"]
    assert [(n.locator, n.cell_states) for n in tables] == [("notice_1__table_1", {"1,1": "unsupported"})]
    again = publish.publish_texts(store, "ng", publish.Texts(approved=[marked]), decision)
    assert again["unchanged"] == ["ng-example-act-2023"]
    # The mark taken off, the cell left empty: the same bytes, but other structure, so a new version.
    publish.publish_texts(
        store, "ng", publish.Texts(approved=[approved_text(after=[CERTIFIED, CERTIFICATE])]), decision
    )
    unmarked = next(v for v in store.load().versions if v.id != version.id)
    assert unmarked.content_hash == version.content_hash and unmarked.prior_version_id == version.id
    assert unmarked.serialization_profile == "oa-text-1/workbench-units-1"
    assert not any(n.cell_states for n in store.load().structure if n.version_id == unmarked.id)


def test_texts_outside_the_decision_or_without_a_record_are_skipped(decisions, store):
    decision = publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
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


# --- what comes down: every published text no current approval binds --------------------------------


def hashes(store, work="ng-example-act-2023"):
    return next(r for r in store.load().rights if r.source_id == work).approved_content_hashes


def readable(store):
    from rights.policy import public_corpus

    return {
        (v.language, v.canonical_text.split("(1) ")[1].split("\n")[0])
        for v in public_corpus(store.load()).versions
    }


def test_a_correction_of_the_same_official_file_replaces_the_reading_it_corrects(decisions, store):
    from rights.policy import public_corpus

    catalogue_with_record(store)
    decision = publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text()]), decision)
    (first,) = store.load().versions
    # Read again from the same file: the earlier words were a misreading, so they leave in the same write.
    done = publish.publish_texts(
        store, "ng", publish.Texts(approved=[approved_text("The Fund lends.")]), decision
    )
    corrected = next(v for v in store.load().versions if v.id != first.id)
    assert corrected.prior_version_id == first.id
    assert done["superseded"] == [first.id] and done["withdrawn"] == []
    assert hashes(store) == [corrected.content_hash]
    assert [v.id for v in public_corpus(store.load()).versions] == [corrected.id]


def test_a_text_from_a_new_official_file_keeps_the_earlier_files_text_as_history(decisions, store):
    from rights.policy import public_corpus

    catalogue_with_record(store)
    decision = publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text()]), decision)
    (old,) = store.load().versions
    # A new gazette: the earlier text is still a true reading of the earlier file, and a citation to it holds.
    reprint = publish.publish_texts(
        store, "ng", publish.Texts(approved=[approved_text("The Fund may lend.", raw="8" * 64)]), decision
    )
    assert reprint["superseded"] == [] and reprint["withdrawn"] == []
    assert len(public_corpus(store.load()).versions) == 2
    new = next(v for v in store.load().versions if v.id != old.id)
    assert new.prior_version_id == old.id
    # A misreading of the new file is replaced; the earlier file's text stays.
    fixed = approved_text("The Fund may lend money.", raw="8" * 64)
    done = publish.publish_texts(store, "ng", publish.Texts(approved=[fixed]), decision)
    assert done["superseded"] == [new.id]
    assert sorted(v.raw_hash for v in public_corpus(store.load()).versions) == ["7" * 64, "8" * 64]
    # With no current approval of the language at all, nothing vouches for the history: it comes down too.
    lapsed = publish.Texts(bound={("ng-example-act-2023", "en"): [publish.Bound()]})
    assert publish.publish_texts(store, "ng", lapsed, decision)["withdrawn"] == ["ng-example-act-2023 (en)"]
    assert public_corpus(store.load()).versions == []


def test_a_language_whose_approval_lapsed_comes_down_while_its_sibling_stays(decisions, store):
    catalogue_with_record(store)
    corpus = store.load()
    corpus.languages.append(publish.Language(id="fr", name="French", tag="fr"))
    store.save(corpus)
    decision = publish.decide_text("ng", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    french = approved_text("Le Fonds prête.", raw="9" * 64)
    french.language = "fr"
    publish.publish_texts(store, "ng", publish.Texts(approved=[approved_text(), french]), decision)
    assert readable(store) == {("en", "The Fund shall lend to students."), ("fr", "Le Fonds prête.")}
    texts = publish.Texts(
        approved=[approved_text()], bound={("ng-example-act-2023", "fr"): [publish.Bound()]}
    )
    done = publish.take_down(store, "ng", set(), texts)
    assert done["withdrawn"] == ["ng-example-act-2023 (fr)"] and done["taken_down"] == []
    assert readable(store) == {("en", "The Fund shall lend to students.")}
    # Nothing left to do: a second look writes nothing.
    assert publish.take_down(store, "ng", set(), texts)["withdrawn"] == []


class Collection:
    """Stands in for the collections and the review database in a run: each document, its approval and
    the text that approval bound. Answers as workbench.content.Library, workbench.review.Review and
    workbench.store.Store do, for what publishing asks of them."""

    def __init__(self, tmp_path, jurisdiction="rw"):
        from workbench.store import connect

        self.docs = {}
        self.engine = connect(f"sqlite:///{tmp_path / 'review.db'}")
        self.jurisdiction = jurisdiction
        self.unreadable = set()
        self.asked = []  # (document, with its text) of each state read, in order

    def add(self, doc, work, language="en", words="The Fund shall lend.", raw="7" * 64, **state):
        built = approved_text(words, raw=raw).text
        self.docs[doc] = {
            "work": work, "language": language, "built": built, "raw": raw, "bound": built, "earlier": [],
            "approved": True, "decision": "approved", "anonymisation": "none", "text_held": None,
            "jurisdiction": self.jurisdiction, "record": None, **state,
        }  # fmt: skip

    def approve(self, doc, words, raw=None):
        """A reviewer approves another text of this document: its earlier approvals stay on record."""
        found = self.docs[doc]
        built = approved_text(words).text
        found["earlier"].append(found["bound"].text_sha256)
        found.update(built=built, bound=built, raw=raw or found["raw"], approved=True, decision="approved")

    # the library
    def documents(self):
        return [self.act(i) for i in self.docs]

    def act(self, doc):
        found = self.docs[doc]
        return {
            "id": doc, "work": found["work"], "language": found["language"],
            "jurisdiction": found["jurisdiction"], "text_held": found["text_held"],
        }  # fmt: skip

    def has(self, doc):
        return doc in self.docs

    def provenance(self, doc):
        return {"url": "https://example.org/file.pdf", "retrieved_at": "2026-10-09T06:41:17+00:00",
                "sha256": self.docs[doc]["raw"]}  # fmt: skip

    # the review
    def state(self, act, text=False):
        if act["id"] in self.unreadable:
            raise ConnectionError("the review database went away")
        self.asked.append((act["id"], text))
        found, bound = self.docs[act["id"]], self.docs[act["id"]]["bound"]
        approvals = [bound.text_sha256, *reversed(found["earlier"])]
        return {
            "head": {
                "decision": found["decision"], "author": "ada@example.org",
                "created_at": datetime.now(UTC) - timedelta(minutes=5), "text_sha256": bound.text_sha256,
                "structure_sha256": bound.structure_sha256, "serialization_profile": bound.profile_id,
                "structure_rules": bound.rules_id,
            },
            "approvals": [{"decision": "approved", "text_sha256": sha} for sha in approvals],
            "approved": found["approved"] and found["decision"] == "approved",
            "text": found["built"] if text else None,
            "view": {"values": {"profile.anonymisation": found["anonymisation"]}},
        }  # fmt: skip

    def built(self, act, view):
        return self.docs[act["id"]]["record"] or rwanda_law()

    # the review store
    def approved_ids(self):
        return set(self.docs)

    def names(self, emails):
        return {email.lower(): "Ada Reviewer" for email in emails}

    def run(self, public, **options):
        return publish.run(self, self, self, public, publish.PublicationLog(self.engine), **options)

    def runs(self):
        from sqlalchemy import select

        from workbench.store import publication_run

        with self.engine.connect() as db:
            rows = db.execute(select(publication_run).order_by(publication_run.c.id))
            return [dict(row._mapping) for row in rows]

    def statuses(self):
        return publish.PublicationLog(self.engine).statuses()


@pytest.fixture
def both_decisions(decisions):
    publish.decide("rw", "Ada Reviewer", 12, [], "", "")
    publish.decide_text("rw", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    return decisions


def public_texts(store):
    from rights.policy import public_corpus

    return sorted((v.source_id, v.language) for v in public_corpus(store.load()).versions)


def public_records(store):
    from rights.policy import public_corpus

    return sorted(s.id for s in public_corpus(store.load()).sources)


def snapshot(store):
    return store.load().model_dump(mode="json")


def test_a_run_publishes_and_a_lapsed_language_comes_down_while_the_record_and_its_sibling_stay(
    both_decisions, store, tmp_path
):
    collection = Collection(tmp_path)
    collection.add("rw-law-en", "rw-law", "en")
    collection.add("rw-law-fr", "rw-law", "fr", words="Le Fonds prête.", raw="9" * 64)
    first = collection.run(store)
    assert first["countries"]["rw"]["texts"]["published"] == 2
    assert public_texts(store) == [("rw-law", "en"), ("rw-law", "fr")]
    collection.docs["rw-law-fr"]["approved"] = False  # its pages were edited after the approval
    second = collection.run(store)
    assert public_texts(store) == [("rw-law", "en")] and public_records(store) == ["rw-law"]
    assert second["countries"]["rw"]["texts"]["withdrawn"] == 1
    assert collection.statuses()["rw-law-fr"]["text"] == "withdrawn"
    assert collection.statuses()["rw-law-fr"]["details"] == "public"
    assert collection.statuses()["rw-law-en"]["text"] == "public"
    # It stays down, and the workbench goes on saying so, until it is approved again.
    assert collection.run(store)["countries"]["rw"]["texts"]["withdrawn"] == 0
    assert collection.statuses()["rw-law-fr"]["text"] == "withdrawn"
    collection.docs["rw-law-fr"]["approved"] = True
    collection.run(store)
    assert public_texts(store) == [("rw-law", "en"), ("rw-law", "fr")]
    assert len(store.load().versions) == 2  # the same words: the same version, back


@pytest.mark.parametrize("decision_state", ["missing", "expired"])
def test_a_missing_or_expired_decision_stops_new_publication_but_never_a_take_down(
    both_decisions, store, tmp_path, decision_state
):
    collection = Collection(tmp_path)
    collection.add("rw-keep", "rw-keep")
    collection.add("rw-lapses", "rw-lapses", words="Lapses.", raw="8" * 64)
    collection.add("rw-revoked", "rw-revoked", words="Revoked.", raw="9" * 64)
    collection.run(store)
    assert public_texts(store) == [("rw-keep", "en"), ("rw-lapses", "en"), ("rw-revoked", "en")]
    for name in ("rw-catalogue.json", "rw-text.json"):
        path = both_decisions / name
        if decision_state == "missing":
            path.unlink()
        else:
            found = json.loads(path.read_text())
            found["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
            path.write_text(json.dumps(found))
    collection.docs["rw-lapses"]["approved"] = False
    collection.docs["rw-revoked"]["decision"] = "revoked"
    collection.add("rw-new", "rw-new", words="New.", raw="6" * 64)  # waits for a decision
    summary = collection.run(store)["countries"]["rw"]
    assert "blocked" in summary
    assert public_records(store) == ["rw-keep"]
    assert summary["records"]["taken_down"] == 2 and summary["texts"]["withdrawn"] == 2
    statuses = collection.statuses()
    assert statuses["rw-lapses"]["details"] == "taken_down" and statuses["rw-lapses"]["text"] == "withdrawn"
    assert statuses["rw-new"]["details"] == "blocked"
    assert "rw-new" not in {s.id for s in store.load().sources}


def test_only_a_text_decision_missing_still_takes_texts_down(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-law-en", "rw-law", "en")
    collection.add("rw-law-fr", "rw-law", "fr", words="Le Fonds prête.", raw="9" * 64)
    collection.run(store)
    (both_decisions / "rw-text.json").unlink()
    collection.docs["rw-law-fr"]["approved"] = False
    summary = collection.run(store)["countries"]["rw"]
    assert "No text decision" in summary["texts"]["blocked"] and summary["texts"]["withdrawn"] == 1
    assert public_texts(store) == [("rw-law", "en")]
    assert collection.statuses()["rw-law-fr"]["text"] == "withdrawn"


def test_a_corrected_approval_replaces_the_published_text_at_the_next_run(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-law", "rw-law")
    collection.run(store)
    (old,) = store.load().versions
    collection.approve("rw-law", "The Fund lends.")
    summary = collection.run(store)["countries"]["rw"]
    assert summary["texts"]["published"] == 1 and summary["texts"]["superseded"] == 1
    assert summary["texts"]["withdrawn"] == 0
    new = next(v for v in store.load().versions if v.id != old.id)
    assert hashes(store, "rw-law") == [new.content_hash] and new.prior_version_id == old.id
    assert collection.statuses()["rw-law"]["text"] == "public"
    # The record and the rights record were not rewritten on the way: one text replaced another.
    (rights,) = store.load().rights
    assert rights.status == "OPEN" and rights.privacy_status == "cleared"


def test_a_text_approved_as_it_stands_stays_public_though_this_run_could_not_build_it_again(
    both_decisions, store, tmp_path
):
    """The rules changed: the run cannot publish anything new from this approval, but what is public is
    exactly what the approval bound, so nothing comes down."""
    collection = Collection(tmp_path)
    collection.add("rw-law", "rw-law")
    collection.run(store)
    collection.docs["rw-law"]["built"] = approved_text("Read differently under new rules.").text
    summary = collection.run(store)["countries"]["rw"]
    assert "approve it again" in summary["texts_skipped"]["rw-law"]
    assert public_texts(store) == [("rw-law", "en")] and summary["texts"]["withdrawn"] == 0


def test_a_judgment_anonymised_later_comes_down_in_every_language_and_stays_down(
    both_decisions, store, tmp_path
):
    collection = Collection(tmp_path)
    collection.add("rw-case-en", "rw-case", "en")
    collection.add("rw-case-fr", "rw-case", "fr", words="Le Fonds prête.", raw="9" * 64)
    collection.run(store)
    assert public_texts(store) == [("rw-case", "en"), ("rw-case", "fr")]
    for doc in ("rw-case-en", "rw-case-fr"):
        collection.docs[doc]["anonymisation"] = "partial"  # approved again with the names withheld
    summary = collection.run(store)["countries"]["rw"]
    assert public_texts(store) == [] and public_records(store) == ["rw-case"]
    assert summary["texts"]["published"] == 0 and summary["texts"]["withdrawn"] == 2
    assert "names are withheld" in summary["texts_skipped"]["rw-case-en"]
    (rights,) = store.load().rights
    assert rights.approved_content_hashes == [] and rights.privacy_status == "pending"
    collection.run(store)
    assert public_texts(store) == []
    # Reviewed again with no name withheld, and approved: its text is back, under that approval. A new
    # text in place of one withdrawn this way does not name it.
    collection.docs["rw-case-en"]["anonymisation"] = "none"
    collection.docs["rw-case-fr"]["anonymisation"] = "none"
    before = {v.id for v in store.load().versions}
    collection.approve("rw-case-en", "The Fund lends.")
    collection.run(store)
    assert public_texts(store) == [("rw-case", "en"), ("rw-case", "fr")]
    assert store.load().rights[0].privacy_status == "cleared"
    (new,) = [v for v in store.load().versions if v.id not in before]
    assert new.language == "en" and new.prior_version_id is None


def test_a_text_held_later_comes_down(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-law", "rw-law")
    collection.run(store)
    collection.docs["rw-law"]["text_held"] = {"note": "words are masked"}
    summary = collection.run(store)["countries"]["rw"]
    assert public_texts(store) == [] and public_records(store) == ["rw-law"]
    assert summary["texts"]["withdrawn"] == 1 and collection.statuses()["rw-law"]["text"] == "held"


def test_a_document_that_left_the_collection_takes_its_text_and_record_down(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-a", "rw-a")
    collection.add("rw-b", "rw-b", words="Another.", raw="8" * 64)
    collection.run(store)
    # A record no run published: no status is on record for it, so no run ever takes it down.
    corpus = store.load()
    source = corpus.sources[0]
    corpus.sources.append(source.model_copy(update={"id": "rw-from-the-catalogue"}))
    corpus.rights.append(
        corpus.rights[0].model_copy(
            update={"id": "rights-rw-from-the-catalogue", "source_id": "rw-from-the-catalogue"}
        )
    )
    store.save(corpus)
    del collection.docs["rw-b"]
    collection.approved_ids = lambda: {"rw-a", "rw-b"}  # its approval is still on record
    found = collection.run(store)
    assert public_texts(store) == [("rw-a", "en")]
    assert public_records(store) == ["rw-a", "rw-from-the-catalogue"]
    assert found["countries"]["rw"]["texts"]["withdrawn"] == 1 and found["not_in_collection"] == ["rw-b"]
    assert found["take_downs"]["rw"]["taken_down"] == 1
    gone = collection.statuses()["rw-b"]
    assert (gone["details"], gone["text"]) == ("taken_down", "withdrawn")
    # Said once: the next run has nothing more to do, and is not stopped by what it did.
    assert collection.run(store)["take_downs"]["rw"]["taken_down"] == 0


def public_words(store):
    from rights.policy import public_corpus

    return {v.canonical_text.split("(1) ")[1].split("\n")[0] for v in public_corpus(store.load()).versions}


LEAD, CONTRIBUTION = "The lead opinion.", "A contribution."


@pytest.fixture
def shared(both_decisions, store, tmp_path):
    """A judgment's lead opinion and a contribution: one work, one language, each from its own file."""
    collection = Collection(tmp_path)
    collection.add("rw-lead", "rw-case", words=LEAD, raw="1" * 64)
    collection.add("rw-contribution", "rw-case", words=CONTRIBUTION, raw="2" * 64)
    collection.run(store)
    assert public_words(store) == {LEAD, CONTRIBUTION}
    assert all(v.prior_version_id is None for v in store.load().versions)  # neither replaces the other
    return collection


@pytest.mark.parametrize("change", [{"decision": "revoked"}, {"approved": False}])
def test_a_document_comes_down_alone_though_another_shares_its_work_and_language(shared, store, change):
    shared.docs["rw-contribution"].update(change)
    summary = shared.run(store)["countries"]["rw"]
    assert public_words(store) == {LEAD} and summary["texts"]["withdrawn"] == 1
    statuses = shared.statuses()
    assert statuses["rw-contribution"]["text"] == "withdrawn" and statuses["rw-lead"]["text"] == "public"
    assert public_records(store) == ["rw-case"]  # the lead opinion is still approved


def test_a_document_held_comes_down_alone_though_another_shares_its_work_and_language(shared, store):
    shared.docs["rw-contribution"]["text_held"] = {"note": "words are masked"}
    shared.run(store)
    assert public_words(store) == {LEAD} and shared.statuses()["rw-contribution"]["text"] == "held"


def test_each_documents_history_is_its_own(shared, store):
    # The contribution's new file keeps its own earlier text; a correction of it replaces that one only.
    shared.approve("rw-contribution", "A contribution, reprinted.", raw="3" * 64)
    shared.run(store)
    assert public_words(store) == {LEAD, CONTRIBUTION, "A contribution, reprinted."}
    new = next(v for v in store.load().versions if "reprinted" in v.canonical_text)
    old = next(v for v in store.load().versions if CONTRIBUTION in v.canonical_text)
    assert new.prior_version_id == old.id  # its own earlier text, not the lead opinion
    shared.approve("rw-contribution", "A contribution, read again.", raw="3" * 64)
    shared.run(store)
    assert public_words(store) == {LEAD, CONTRIBUTION, "A contribution, read again."}
    # Its approval lapses: its history comes down with it, and the lead opinion stays.
    shared.docs["rw-contribution"]["approved"] = False
    shared.run(store)
    assert public_words(store) == {LEAD}


def test_no_take_down_changes_what_a_record_says_of_its_texts(both_decisions, store, tmp_path):
    """A note that said which texts stand would mark out, by when it changes or when it does not, a text
    withdrawn for a reason that is not told. It says the same however a text came down."""

    def note_of(work):
        return next(s for s in store.load().sources if s.id == work).catalogue_note

    collection = Collection(tmp_path)
    collection.add("rw-lapses", "rw-lapses", words="Lapses.", raw="1" * 64)
    collection.add("rw-revoked", "rw-revoked", words="Revoked.", raw="2" * 64)
    collection.add("rw-withheld", "rw-withheld", words="Withheld.", raw="3" * 64)
    collection.add("rw-en", "rw-both", words="English.", raw="4" * 64)
    collection.add("rw-fr", "rw-both", "fr", words="French.", raw="5" * 64)
    collection.add("rw-keeps", "rw-keeps", words="Keeps.", raw="6" * 64)
    collection.run(store)
    works = ("rw-lapses", "rw-revoked", "rw-withheld", "rw-both", "rw-keeps")
    notes = {work: note_of(work) for work in works}
    assert len(set(notes.values())) == 1 and next(iter(notes.values())).startswith(publish.TEXT_NOTE)
    collection.docs["rw-fr"]["approved"] = False
    collection.docs["rw-withheld"]["anonymisation"] = "partial"
    collection.docs["rw-lapses"].update(approved=False)
    collection.docs["rw-revoked"].update(decision="revoked")
    collection.run(store)
    collection.run(store)
    assert {work: note_of(work) for work in works} == notes


def two_countries(tmp_path):
    publish.decide("ke", "Ada Reviewer", 12, [], "", "")
    publish.decide_text("ke", "Ada Reviewer", 12, EVIDENCE, **TEXT_DECISION)
    kenya = rwanda_law(
        place=Place(code="ke", name="Kenya", level="national"),
        identity=Identity(
            family="legislation", type="law", work_uri="/akn/ke/act/2026/9", title="Example Act"
        ),
    )
    collection = Collection(tmp_path)
    many(collection, 12)
    for number in range(12, 15):
        collection.add(
            f"ke-{number}", f"ke-{number}", words=f"Kenyan law {number}.", raw=f"{number:x}" * 64,
            jurisdiction="ke", record=kenya,
        )  # fmt: skip
    return collection


def test_a_mass_lapse_stops_nothing_someone_recorded_in_any_country(both_decisions, store, tmp_path):
    collection = two_countries(tmp_path)
    collection.run(store)
    everything = public_records(store)
    # Everything in Rwanda lapses: a wrong collection, say. Revocations and withheld names still act.
    for number in range(12):
        collection.docs[f"rw-{number}"]["approved"] = False
    collection.docs["rw-0"]["decision"] = "revoked"
    collection.docs["rw-1"]["anonymisation"] = "partial"
    collection.docs["ke-12"]["decision"] = "revoked"
    collection.docs["ke-13"]["anonymisation"] = "partial"
    collection.add("rw-new", "rw-new", words="New.", raw="e" * 64)
    with pytest.raises(publish.Refused, match="would take down") as refused:
        collection.run(store)
    assert "--allow-take-downs" in str(refused.value)
    # Details changed to withhold names lapse too, and that comes down; what only lapsed does not.
    assert public_records(store) == sorted(set(everything) - {"rw-0", "rw-1", "ke-12"})
    texts = {work for work, _ in public_texts(store)}
    assert not texts & {"rw-0", "rw-1", "ke-12", "ke-13"} and "rw-2" in texts and "ke-14" in texts
    assert "rw-new" not in {s.id for s in store.load().sources}
    last = collection.runs()[-1]
    assert last["status"] == "failed" and last["summary"]["take_downs"]["ke"]["taken_down"] == 1


def named_judgment(names_withheld=False, summary=None, also_known_as=()):
    """A fictional judgment whose title names a party, who may be marked withheld."""
    return DocumentRecord(
        identity=Identity(
            family="judgment", type="judgment", work_uri="/akn/rw/judgment/2026/9",
            title="Ada Bello v Example Services Limited", year=2026, alternative_titles=also_known_as,
        ),
        place=Place(code="rw", name="Rwanda", level="national"),
        expressions=(Expression(language="en", this_text=True),),
        publications=(Publication(kind="law_report", name="Court site", url="https://www.judiciary.gov.rw/9"),),
        events=(Event(kind="delivered", when=Dated(value=date(2026, 3, 1))),),
        status=Status(legal="decided"),
        text=TextInfo(
            form="court_copy", version=Version(label="As delivered"),
            summary=Summary(kind="editorial", text=summary, official=False) if summary else None,
        ),
        classification=Classification(hierarchy="judicial"),
        profile=JudgmentProfile(
            family="judgment", court=Court(name="High Court", code="hc", level="superior", seat="Kigali"),
            parties=(
                Party(name="Ada Bello", role="appellant", anonymised=names_withheld),
                Party(name="Example Services Limited", role="respondent"),
            ),
            anonymisation="partial" if names_withheld else "none",
            anonymisation_reason="child" if names_withheld else None,
        ),
    )  # fmt: skip


@pytest.fixture
def case_named(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-case", "rw-case", record=named_judgment())
    collection.run(store)
    assert public_records(store) == ["rw-case"]
    return collection


def title_of(store, work="rw-case"):
    return next(s for s in store.load().sources if s.id == work).title


def withheld_now(collection, summary=None):
    """The reviewer marks the name withheld and approves again: the approval is current."""
    collection.docs["rw-case"].update(anonymisation="partial", record=named_judgment(True, summary))


def test_a_public_title_naming_someone_withheld_comes_down_then_returns_without_the_name(case_named, store):
    withheld_now(case_named)
    first = case_named.run(store)
    assert public_records(store) == [] and public_texts(store) == []
    assert first["take_downs"]["rw"]["taken_down"] == 1
    assert case_named.statuses()["rw-case"]["details"] == "taken_down"
    case_named.run(store)
    assert public_records(store) == ["rw-case"] and "Bello" not in title_of(store)
    assert public_texts(store) == []  # its text stays withheld


def test_a_public_title_naming_someone_withheld_comes_down_when_its_details_are_refused(case_named, store):
    withheld_now(case_named, summary="Bello was dismissed while on leave.")
    case_named.run(store)
    assert public_records(store) == []
    summary = case_named.run(store)["countries"]["rw"]
    assert "would still name a withheld party" in summary["records_skipped"]["rw-case"]
    assert public_records(store) == []  # and stays down until the details are put right
    withheld_now(case_named)
    case_named.run(store)
    assert public_records(store) == ["rw-case"] and "Bello" not in title_of(store)


def test_a_public_title_naming_someone_withheld_comes_down_with_no_current_decision(
    case_named, store, both_decisions
):
    (both_decisions / "rw-catalogue.json").unlink()
    withheld_now(case_named)
    summary = case_named.run(store)["countries"]["rw"]
    assert "blocked" in summary and public_records(store) == [] and public_texts(store) == []


def test_an_alias_naming_someone_withheld_goes_with_the_title(both_decisions, store, tmp_path):
    """Aliases are never deleted by a save, so one the details no longer give must stop naming anyone, or
    the record is found by that name and taken down again at every other run."""
    collection = Collection(tmp_path)
    collection.add("rw-case", "rw-case", record=named_judgment(also_known_as=("The Bello case",)))
    collection.run(store)
    assert any("Bello" in c.value for c in store.load().citations)
    withheld_now(collection)
    collection.run(store)
    assert public_records(store) == []
    collection.run(store)
    assert public_records(store) == ["rw-case"]
    assert not any("Bello" in c.value for c in store.load().citations)
    collection.run(store)
    assert public_records(store) == ["rw-case"]


def test_a_record_that_no_longer_names_anyone_withheld_is_not_taken_down_again(case_named, store):
    withheld_now(case_named)
    case_named.run(store)
    case_named.run(store)
    assert public_records(store) == ["rw-case"]
    assert case_named.run(store)["take_downs"]["rw"]["taken_down"] == 0
    assert public_records(store) == ["rw-case"]


def test_a_text_is_built_only_to_publish_it_one_country_at_a_time(
    both_decisions, store, tmp_path, monkeypatch
):
    """A run reads every approval first, without the texts, which are the bulk of what it could hold; each
    country's texts are built when it publishes them and let go with it."""
    collection = two_countries(tmp_path)
    collection.run(store)
    collection.asked.clear()
    marks = []
    take_down, publish_texts = publish.take_down, publish.publish_texts

    def counting_take_down(*args, **kwargs):
        marks.append(len(collection.asked))
        return take_down(*args, **kwargs)

    def counting_texts(*args, **kwargs):
        marks.append(len(collection.asked))
        return publish_texts(*args, **kwargs)

    monkeypatch.setattr(publish, "take_down", counting_take_down)
    monkeypatch.setattr(publish, "publish_texts", counting_texts)
    collection.run(store)
    first_write = marks[0]
    assert all(not with_text for _, with_text in collection.asked[:first_write])
    built = [doc[:2] for doc, with_text in collection.asked if with_text]
    assert built and collection.asked[first_write:] and all(w for _, w in collection.asked[first_write:])
    assert built == sorted(built)  # a country at a time


# --- a run that cannot tell what is approved, or looks wrong, does nothing and says so ---------------


def test_a_run_that_cannot_read_an_approval_publishes_nothing_and_takes_nothing_down(
    both_decisions, store, tmp_path
):
    collection = Collection(tmp_path)
    collection.add("rw-a", "rw-a")
    collection.add("rw-b", "rw-b", words="Another.", raw="8" * 64)
    collection.run(store)
    before = snapshot(store)
    collection.add("rw-new", "rw-new", words="New.", raw="6" * 64)
    collection.docs["rw-a"]["approved"] = False
    collection.unreadable = {"rw-new"}  # read last: everything is read before anything is written
    with pytest.raises(ConnectionError):
        collection.run(store)
    assert snapshot(store) == before
    last = collection.runs()[-1]
    assert last["status"] == "failed" and "went away" in last["error"]


def test_a_run_whose_approvals_cannot_be_listed_is_recorded_as_failed(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-a", "rw-a")
    collection.run(store)
    before = snapshot(store)

    def unreadable():
        raise ConnectionError("the review database went away")

    collection.approved_ids = unreadable
    with pytest.raises(ConnectionError):
        collection.run(store)
    assert snapshot(store) == before and collection.runs()[-1]["status"] == "failed"


def test_an_empty_read_of_the_approvals_is_refused_while_reviewed_texts_are_public(
    both_decisions, store, tmp_path
):
    collection = Collection(tmp_path)
    collection.add("rw-a", "rw-a")
    collection.run(store)
    before = snapshot(store)
    collection.docs = {}
    with pytest.raises(publish.Refused, match="names no approval"):
        collection.run(store)
    assert snapshot(store) == before
    assert "names no approval" in collection.runs()[-1]["error"]
    # An operator who has looked may let it through, naming how many may come down.
    collection.run(store, allow_take_downs=1)
    assert public_texts(store) == []


def test_a_collection_that_holds_none_of_the_approved_documents_is_refused(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-a", "rw-a")
    collection.run(store)
    before = snapshot(store)
    collection.documents = lambda: []  # ALVARY_REVIEW_COLLECTIONS points somewhere else
    with pytest.raises(publish.Refused, match="holds none"):
        collection.run(store)
    assert snapshot(store) == before and collection.runs()[-1]["status"] == "failed"


def many(collection, count):
    for number in range(count):
        collection.add(f"rw-{number}", f"rw-{number}", words=f"Law number {number}.", raw=f"{number:x}" * 64)


def test_a_mass_lapse_is_refused_as_a_wrong_collection_until_an_operator_accepts_it(
    both_decisions, store, tmp_path
):
    collection = Collection(tmp_path)
    many(collection, 12)
    collection.run(store)
    before = snapshot(store)
    # Every approval no longer covers its pages: a collection of another revision, say.
    for found in collection.docs.values():
        found["approved"] = False
    collection.add("rw-new", "rw-new", words="New.", raw="e" * 64)
    with pytest.raises(publish.Refused, match="would take down"):
        collection.run(store)
    assert snapshot(store) == before  # not even the new approval: the whole run is refused
    last = collection.runs()[-1]
    assert last["status"] == "failed" and "--allow-take-downs" in last["error"]
    with pytest.raises(publish.Refused, match="would take down"):
        collection.run(store, allow_take_downs=5)  # fewer than it would take down
    assert snapshot(store) == before
    collection.run(store, allow_take_downs=12)
    assert public_records(store) == ["rw-new"]


def test_a_few_lapses_come_down_without_asking(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    many(collection, 12)
    collection.run(store)
    for number in range(2):
        collection.docs[f"rw-{number}"]["approved"] = False
    collection.run(store)
    assert len(public_records(store)) == 10 and len(public_texts(store)) == 10


def test_every_public_document_of_a_small_country_lapsing_at_once_is_refused(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    many(collection, 2)
    collection.run(store)
    for found in collection.docs.values():
        found["approved"] = False
    with pytest.raises(publish.Refused, match="would take down"):
        collection.run(store)
    assert len(public_records(store)) == 2


def test_a_lone_public_document_may_lapse(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    many(collection, 1)
    collection.run(store)
    collection.docs["rw-0"]["approved"] = False
    collection.run(store)
    assert public_records(store) == [] and public_texts(store) == []


def test_revocations_are_decisions_someone_took_so_no_count_holds_them_back(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    many(collection, 12)
    collection.run(store)
    for number in range(10):
        collection.docs[f"rw-{number}"]["decision"] = "revoked"
    collection.run(store)
    assert public_records(store) == ["rw-10", "rw-11"] and len(public_texts(store)) == 2


def test_a_failure_while_publishing_comes_after_every_take_down(both_decisions, store, tmp_path, monkeypatch):
    collection = Collection(tmp_path)
    many(collection, 3)
    collection.run(store)
    collection.docs["rw-0"]["approved"] = False

    def broken(*_args, **_kwargs):
        raise publish.Refused("the rights record would not serve this text")

    monkeypatch.setattr(publish, "publish_records", broken)
    with pytest.raises(publish.Refused):
        collection.run(store)
    assert ("rw-0", "en") not in public_texts(store)
    assert collection.runs()[-1]["status"] == "failed"


def test_nothing_an_operator_holds_back_is_published_again(both_decisions, store, tmp_path):
    collection = Collection(tmp_path)
    collection.add("rw-a", "rw-a")
    collection.add("rw-b", "rw-b", words="Another.", raw="8" * 64)
    collection.run(store)
    corpus = store.load()
    held = {r.source_id: r for r in corpus.rights}
    # A text held for privacy with its record public, and a record withdrawn confidentially.
    corpus.rights = [
        held["rw-a"].model_copy(update={"privacy_status": "blocked"}),
        held["rw-b"].model_copy(update={"privacy_status": "blocked", "metadata_public": False}),
    ]
    store.save(corpus)
    summary = collection.run(store)["countries"]["rw"]
    assert public_texts(store) == [] and public_records(store) == ["rw-a"]
    assert "privacy review is blocked" in summary["texts_skipped"]["rw-a"]
    assert "rw-b" in summary["records_skipped"]
    rights = {r.source_id: r for r in store.load().rights}
    assert rights["rw-a"].privacy_status == "blocked" and rights["rw-b"].privacy_status == "blocked"
    assert not rights["rw-b"].metadata_public
