"""The common document record for every African jurisdiction and kind of legal document.

The examples are fictional: invented parties and numbers in real forms of citation."""

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from schema import record as R
from schema.jurisdictions import JurisdictionProfile, expected_steps, registry


def core(**overrides):
    """A minimal valid record for a Nigerian Act, to vary in each test."""
    values = {
        "identity": R.Identity(
            family="legislation",
            type="act",
            work_uri="/akn/ng/act/2022/8",
            title="Example Act, 2022",
        ),
        "place": R.Place(code="ng", name="Nigeria", level="national"),
        "expressions": (R.Expression(language="en", authority="authoritative", this_text=True),),
        "status": R.Status(legal="unknown"),
        "text": R.TextInfo(form="gazette_publication", version=R.Version(label="As enacted")),
        "classification": R.Classification(hierarchy="primary"),
        "profile": R.LegislationProfile(family="legislation"),
    }
    return R.DocumentRecord(**{**values, **overrides})


def test_every_family_maps_to_an_akoma_ntoso_document_type():
    doctypes = {"act", "bill", "judgment", "officialGazette", "doc", "statement", "debate"}
    assert all(doctype in doctypes and kinds for doctype, kinds in R.FAMILIES.values())
    assert R.work_uri("ke", "judgment", "5", 2019, actor="kehc") == "/akn/ke/judgment/kehc/2019/5"
    assert (
        R.work_uri("aa-ecowas", "regional_instrument", "1", 2010, subtype="supplementary-act")
        == "/akn/aa-ecowas/act/supplementary-act/2010/1"
    )
    assert (
        R.expression_uri("/akn/sn/act/2023/12", "fr", date(2023, 7, 12))
        == "/akn/sn/act/2023/12/fra@2023-07-12"
    )


def test_a_judgment_records_its_court_parties_bench_and_outcome_and_can_withhold_a_childs_name():
    judgment = core(
        identity=R.Identity(
            family="judgment",
            type="judgment",
            work_uri="/akn/ke/judgment/kehc/2019/5",
            title="Example Ltd v Another",
            full_title="Example Limited v Another Company Limited and the Attorney General",
            numbers=(R.Number(scheme="case", value="E009 of 2018", printed="Civil Appeal No. E009 of 2018"),),
            citations=(R.Citation(kind="neutral", text="[2019] KEHC 5 (KLR)"),),
        ),
        place=R.Place(code="ke", name="Kenya", level="national"),
        events=(R.Event(kind="delivered", when=R.Dated(value=date(2019, 3, 1))),),
        status=R.Status(legal="decided"),
        text=R.TextInfo(form="court_copy", version=R.Version(label="As delivered")),
        classification=R.Classification(hierarchy="judicial"),
        profile=R.JudgmentProfile(
            family="judgment",
            court=R.Court(name="High Court of Kenya", code="kehc", level="superior", seat="Nairobi"),
            case_numbers=("Civil Appeal No. E009 of 2018",),
            parties=(
                R.Party(name="Example Limited", role="appellant"),
                R.Party(name="[Withheld]", role="respondent", anonymised=True),
            ),
            bench=(R.Judge(name="A. Judge", role="sole"),),
            outcome=("allowed",),
            anonymisation="partial",
            anonymisation_reason="child",
        ),
    )
    gaps = R.missing(judgment)
    assert "event:decided" in gaps and "profile.case_numbers" not in gaps and "profile.outcome" not in gaps
    accounted = judgment.model_copy(
        update={"absent": {**judgment.absent, "event:decided": R.Absent(reason="not_in_source")}}
    )
    assert "event:decided" not in R.missing(accounted)


def test_dates_keep_their_printed_form_and_calendar():
    printed = R.Dated(value=date(2021, 9, 30), printed="Meskerem 20, 2014", calendar="ethiopian")
    assert printed.calendar == "ethiopian"
    with pytest.raises(ValidationError):
        R.Dated(value=date(2021, 9, 30), calendar="ethiopian")  # another calendar keeps its wording
    with pytest.raises(ValidationError):
        R.Dated()


def test_languages_say_which_text_is_authoritative_and_which_way_it_is_written():
    record = core(
        expressions=(
            R.Expression(
                language="ar", script="Arab", direction="rtl", authority="equally_authentic", this_text=True
            ),
            R.Expression(language="fr", authority="equally_authentic"),
        )
    )
    assert [e.language for e in record.expressions] == ["ar", "fr"]
    with pytest.raises(ValidationError):
        R.Expression(language="ar", script="Arab", authority="authoritative")  # Arabic is right to left
    with pytest.raises(ValidationError):
        core(expressions=(R.Expression(language="en", authority="authoritative"),))  # which one is this text?


def test_regional_instruments_and_treaties_have_their_own_profiles():
    regional = core(
        identity=R.Identity(
            family="regional_instrument",
            type="supplementary_act",
            work_uri="/akn/aa-ecowas/act/supplementary-act/2010/1",
            title="Example Supplementary Act",
        ),
        place=R.Place(code="aa-ecowas", name="Economic Community of West African States", level="regional"),
        classification=R.Classification(hierarchy="regional"),
        profile=R.RegionalProfile(
            family="regional_instrument",
            organ=R.Body(name="Authority of Heads of State and Government", kind="regional_body"),
            legal_effect="directly_applicable",
        ),
    )
    assert "event:entered_into_force" in R.missing(regional)
    treaty = R.TreatyProfile(
        family="treaty",
        parties=(
            R.TreatyParty(party="Example Republic", place="gh", ratified=R.Dated(value=date(2020, 1, 1))),
        ),
        authentic_languages=("en", "fr", "ar", "pt"),
    )
    assert treaty.parties[0].place == "gh"


def test_records_refuse_inconsistent_identity():
    with pytest.raises(ValidationError):  # a judgment is an Akoma Ntoso judgment, not an act
        R.Identity(family="judgment", type="judgment", work_uri="/akn/ke/act/2019/5", title="x")
    with pytest.raises(ValidationError):  # not a kind of legislation
        R.Identity(family="legislation", type="judgment", work_uri="/akn/ng/act/2019/5", title="x")
    with pytest.raises(ValidationError):  # the profile must match the family
        core(profile=R.BillProfile(family="bill", stage="introduced"))
    with pytest.raises(ValidationError):  # the URI must be in the record's place
        core(place=R.Place(code="gh", name="Ghana", level="national"))
    with pytest.raises(ValidationError):  # subsidiary legislation names the law it is made under
        R.SubsidiaryProfile(family="subsidiary_legislation", made_under=())


def test_the_registry_covers_the_african_union_and_its_regional_communities():
    profiles = registry()
    countries = [p for p in profiles.values() if p.level == "national"]
    assert len(countries) == 55 and all("aa-au" in p.member_of and p.au_region for p in countries)
    assert {"aa-au", "aa-eac", "aa-ecowas", "aa-sadc"} <= set(profiles)
    assert all(p.sources for p in profiles.values() if p.researched)
    nigeria = profiles["ng"]
    assert expected_steps(nigeria, "act") == (
        "passed:senate",
        "passed:house",
        "certified",
        "assented",
        "published",
        "commenced",
    )
    assert "approved:states" in expected_steps(nigeria, "constitutional_amendment")
    assert expected_steps(profiles["gh"], "act") == ()  # not yet researched: the common standard alone
    with pytest.raises(ValidationError):
        JurisdictionProfile(code="xx", name="Nowhere", level="national", researched=True)


def test_a_reviewed_nigerian_act_becomes_a_complete_common_record():
    from workbench import details

    given = {
        "short_title": "Example Act, 2022",
        "long_title": "An Act to repeal the Old Act, 2004; and for related matters.",
        "document_type": "act",
        "jurisdiction": "ng",
        "number": "8",
        "year": "2022",
        "citation": "Example Act, No. 8, 2022",
        "enacting_body": "National Assembly of the Federal Republic of Nigeria",
        "bill_title": "Example Bill, 2022",
        "passed_senate": "2022-04-06",
        "passed_house": "2022-07-27",
        "authenticated_on": "2023-02-13",
        "authenticated_by": "Ojo O. A., fnia, fcia, Clerk to the National Assembly",
        "assent_date": "2023-03-17",
        "assented_by": "Muhammadu Buhari, GCFR, President of the Federal Republic of Nigeria",
        "commencement_date": "2023-03-17",
        "gazette_name": "Federal Republic of Nigeria Official Gazette",
        "gazette_number": "56",
        "gazette_date": "2023-03-27",
        "text_form": "gazette_supplement",
        "version_label": "As enacted",
        "summary": "This Act provides for examples.",
        "legal_status": "unknown",
        "relations": [{"kind": "repeals", "target": "Old Act, 2004"}],
        "language": "en",
    }
    nigeria = registry()["ng"]
    record = details.to_record(
        given, {}, {"id": "ng-example-act-2022", "title": "Example Act, 2022"}, {}, nigeria
    )
    assert record.identity.work_uri == "/akn/ng/act/2022/8"
    assert [e.step for e in record.events] == [
        "passed:senate",
        "passed:house",
        "certified",
        "assented",
        "commenced",
        "published",
    ]
    assent = next(e for e in record.events if e.kind == "assented")
    assert assent.person.name == "Muhammadu Buhari, GCFR"
    assert assent.person.office == "President of the Federal Republic of Nigeria"
    assert record.events[0].by.name == "Senate"
    assert R.missing(record, expected_steps(nigeria, record.identity.type)) == []


def test_the_published_json_schemas_match_the_contracts():
    for name, model in (("record", R.DocumentRecord), ("jurisdiction", JurisdictionProfile)):
        published = json.loads(Path(f"schema/{name}.schema.json").read_text())
        assert published == model.model_json_schema(), f"run scripts/generate_schema.py ({name})"
