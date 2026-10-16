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
