"""The common document record for every African jurisdiction and kind of legal document.

The examples are fictional: invented parties and numbers in real forms of citation."""

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from schema import record as R
from schema.jurisdictions import JurisdictionProfile, expected_steps, registry
from schema.memberships import MembershipRegister
from schema.memberships import register as membership_register


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


def test_an_authority_nothing_states_stays_unknown_and_a_language_rule_comes_with_its_evidence():
    assert R.Expression(language="fr").authority is None  # not recorded: the catalogue answers unknown
    with pytest.raises(ValidationError):  # evidence of an authority needs the authority
        R.Expression(language="fr", authority_evidence=R.Evidence(source="document", page=2))
    stated = R.Evidence(
        source="document", page=4, quote="En cas de divergence, la version française fait foi."
    )
    record = core(
        expressions=(
            R.Expression(language="fr", authority="authoritative", authority_evidence=stated, this_text=True),
            R.Expression(language="en"),
        ),
        language_rule=R.LanguageRule(
            rule="Where the versions differ, the French version prevails.",
            prevails="fr",
            provision="Article 9",
            evidence=stated,
        ),
    )
    assert record.language_rule.prevails == "fr" and record.expressions[1].authority is None
    with pytest.raises(ValidationError):
        R.LanguageRule(rule="The French version prevails.")  # and where it is stated
    none_stated = core(absent={"language_rule": R.Absent(reason="not_in_source")})
    assert none_stated.language_rule is None and none_stated.absent["language_rule"].reason == "not_in_source"


def test_the_details_screen_edits_each_versions_authority_and_the_language_rule():
    from workbench import record_view as V

    rwanda = registry()["rw"]
    specs = V.fields("legislation", "law", rwanda)
    by_key = {s.key: s for s in specs}
    assert {"authority", "authority_page", "authority_words", "authority_url"} <= {
        c["key"] for c in by_key["expressions"].columns
    }
    rule = by_key["language_rule.rule"].as_dict()
    assert rule["accountable"] and not rule["required"]  # optional, yet a reviewer may say why not given
    place, _ = V.place_for({"jurisdiction": "rw"}, {}, registry())
    words = "This Law was drafted in English, considered and adopted in Ikinyarwanda."
    values = {
        "identity.family": "legislation",
        "identity.type": "law",
        "identity.title": "Example Law",
        "text.form": "gazette_publication",
        "text.version.label": "As published",
        "status.legal": "unknown",
        "expressions": [
            {"language": "rw", "authority": "authoritative", "authority_page": "12", "authority_words": words,
             "this_text": "yes"},
            {"language": "fr", "this_text": "no"},  # nothing states it: unknown, never assumed
        ],
    }  # fmt: skip

    def build(changes=None, absent=None):
        return V.to_record(values | (changes or {}), absent or {}, place, rwanda, "rw-example")

    rw, fr = build().expressions
    assert rw.authority_evidence == R.Evidence(source="document", page=12, quote=words)
    assert fr.authority is None and fr.authority_evidence is None and build().language_rule is None
    ruled = build(
        {
            "language_rule.rule": "Where the versions differ, the Kinyarwanda version prevails.",
            "language_rule.prevails": "rw",
            "language_rule.provision": "Article 9",
            "language_rule.evidence.page": "3",
            "language_rule.evidence.quote": "the Kinyarwanda version prevails",
        }
    ).language_rule
    assert (ruled.prevails, ruled.provision, ruled.evidence) == (
        "rw",
        "Article 9",
        R.Evidence(source="document", page=3, quote="the Kinyarwanda version prevails"),
    )
    elsewhere = build({"language_rule.rule": "The versions are equally authentic.",
                       "language_rule.evidence.url": "https://www.example.org/official-languages-law"})  # fmt: skip
    assert elsewhere.language_rule.evidence.source == "external" and elsewhere.language_rule.prevails is None
    with pytest.raises(ValueError, match="where the language rule is stated"):
        build({"language_rule.rule": "The Kinyarwanda version prevails."})
    with pytest.raises(ValueError, match="give the rule"):
        build({"language_rule.prevails": "rw"})
    with pytest.raises(ValueError, match="needs the authority"):
        build({"expressions": [{"language": "rw", "authority_page": "2", "this_text": "yes"}]})
    with pytest.raises(ValueError, match="page's number"):
        build({"expressions": [{"language": "rw", "authority": "authoritative", "authority_page": "p. 2",
                                "this_text": "yes"}]})  # fmt: skip
    # The reviewer may say the copy states no rule: the record accounts for it.
    _, absent = V.validate(values, {"language_rule.rule": "not_in_source"}, specs)
    assert build(absent=absent).absent["language_rule"].reason == "not_in_source"
    # A Nigerian DETAILS-1 record that does not say its language's status: unknown, never "original".
    assert "authority" not in V.from_legacy({"language": "en"}, {})[0]["expressions"][0]
    original = V.from_legacy({"language": "en", "language_status": "original"}, {})[0]
    assert original["expressions"][0]["authority"] == "authoritative"


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


def test_the_details_screen_records_each_partys_denunciation_and_reservations():
    from workbench import record_view as V

    columns = {
        c["key"] for c in {s.key: s for s in V.fields("treaty", "charter", None)}["profile.parties"].columns
    }
    assert {"signed", "ratified", "acceded", "denounced", "reservations"} <= columns
    place, profile = V.place_for({"jurisdiction": "aa-au"}, {}, registry())
    values = {
        "identity.family": "treaty",
        "identity.type": "charter",
        "identity.title": "Example Charter",
        "text.form": "official_website",
        "text.version.label": "As adopted",
        "status.legal": "unknown",
        "expressions": [{"language": "en", "this_text": "yes"}],
        "profile.depositary": "Example Commission",
        "profile.parties": [
            {"party": "Example Republic", "place": "gh", "ratified": "2005-03-01", "denounced": "2020-01-01",
             "reservations": "Article 4"},
            {"party": "Other Republic", "place": "ke", "signed": "2004-01-01"},
        ],
    }  # fmt: skip
    first, second = V.to_record(values, {}, place, profile, "example-charter").profile.parties
    assert first.denounced == R.Dated(value=date(2020, 1, 1)) and first.reservations == "Article 4"
    assert second.denounced is None and second.reservations is None and second.ratified is None


def test_a_party_list_is_complete_only_as_of_a_date_its_status_list_shows():
    from workbench import record_view as V

    keys = [s.key for s in V.fields("treaty", "charter", None)]
    beside = keys[keys.index("profile.parties") + 1 : keys.index("profile.parties") + 4]
    assert beside == [
        "profile.parties_complete_as_of",
        "profile.parties_complete_evidence.url",
        "profile.parties_complete_evidence.quote",
    ]
    place, profile = V.place_for({"jurisdiction": "aa-au"}, {}, registry())
    values = {
        "identity.family": "treaty", "identity.type": "charter", "identity.title": "Example Charter",
        "text.form": "official_website", "text.version.label": "As adopted", "status.legal": "unknown",
        "expressions": [{"language": "en", "this_text": "yes"}], "profile.depositary": "Example Commission",
        "profile.parties": [{"party": "Example Republic", "place": "gh", "ratified": "2005-03-01"}],
    }  # fmt: skip
    listed = {
        "profile.parties_complete_as_of": "2026-06-01",
        "profile.parties_complete_evidence.url": "https://www.example.org/status-list.pdf",
        "profile.parties_complete_evidence.quote": "Status list as at 1 June 2026",
    }
    treaty = V.to_record(values | listed, {}, place, profile, "example-charter").profile
    assert treaty.parties_complete_as_of == date(2026, 6, 1)
    assert treaty.parties_complete_evidence == R.Evidence(
        source="external",
        quote="Status list as at 1 June 2026",
        url="https://www.example.org/status-list.pdf",
    )
    # Not given: not complete. A date without its status list, or a list without its date, is refused.
    unknown = V.to_record(values, {}, place, profile, "example-charter").profile
    assert unknown.parties_complete_as_of is None and unknown.parties_complete_evidence is None
    for key in ("profile.parties_complete_as_of", "profile.parties_complete_evidence.url"):
        with pytest.raises(ValueError, match="status list"):
            V.to_record(values | {key: listed[key]}, {}, place, profile, "example-charter")
    with pytest.raises(ValidationError):
        R.TreatyProfile(family="treaty", parties_complete_as_of=date(2026, 6, 1))
    # Whether a list is complete is a legal fact: a reader's draft never fills it in.
    assert not set(listed) & set(V.first_draft(values | listed, {})[0])


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
    assert len(countries) == 55 and all(p.au_region for p in countries)
    # Membership of the AU is a dated relationship in the membership register, not a profile field.
    au = {m.subject_id for m in membership_register().memberships if m.target_id == "aa-au"}
    assert au == {p.code for p in countries}
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
    for name, model in (
        ("record", R.DocumentRecord),
        ("jurisdiction", JurisdictionProfile),
        ("membership", MembershipRegister),
    ):
        published = json.loads(Path(f"schema/{name}.schema.json").read_text())
        assert published == model.model_json_schema(), f"run scripts/generate_schema.py ({name})"


def test_a_rwandan_organic_law_follows_rwandas_procedure_into_a_complete_record():
    """Both Chambers adopt an organic law (the Senate may sit first); the President promulgates, the
    Prime Minister countersigns and the Minister of Justice seals; three languages, Kinyarwanda first."""
    from workbench import record_view as V

    rwanda = registry()["rw"]
    assert rwanda.researched and rwanda.official_languages == ("rw", "en", "fr", "sw")
    assert expected_steps(rwanda, "law")[:2] == ("adopted:deputies", "adopted:senate")
    assert expected_steps(rwanda, "presidential_order")[0] == "approved:cabinet"
    document = {"type": "organic_law", "title": "Organic Law governing examples", "year": 2026}
    values = V.base_values(document, {})
    assert (values["identity.family"], values["identity.type"]) == ("legislation", "organic_law")
    assert V.base_values({"type": "presidential_order"}, {})["identity.family"] == "executive_instrument"
    # A later law in the collection repeals it: suggested as repealed, or noted where its listing disagrees;
    # never filled in (owner decision 2026-10-21), the status left not yet checked.
    repealer = {"kind": "repealed_by", "target": "Law n° 1/2027 of 01/01/2027", "target_id": "rw-law-1-2027"}
    later = {**document, "relations": [repealer]}
    drafted = V.base_values(later, {})
    assert "relations" not in drafted and drafted["status.legal"] == "unknown"
    repealed = "Repealed by Law n° 1/2027 of 01/01/2027, as its text says."
    assert V.suggestions(later) == {
        "status.legal": {"value": "repealed", "page": None, "evidence": repealed},
        "status.note": {"value": repealed, "page": None, "evidence": repealed},
    }
    listed = {
        "status.legal": {"value": "in_force", "page": None, "evidence": "Listed in force."},
        "status.note": {"value": "Listed in force.", "page": None, "evidence": "Listed in force."},
    }
    line = {
        "status": "repealed",
        "repealed_by": [{"work": "rw-law-1-2027", "title": "Law n° 1/2027 of 01/01/2027"}],
        "latest": {"work": "rw-law-3-2030", "title": "Law n° 3/2030 of 01/01/2030"},
        "parents_repealed": [{"work": "rw-law-5-2020", "title": "Law n° 5/2020", "repealed_by": [{"work": "rw-law-9-2025", "title": "Law n° 9/2025"}]}],
    }  # fmt: skip
    chain = V.suggestions({**document, "relations": [repealer], "currency": line})
    assert chain["status.note"]["value"] == (
        "Repealed by Law n° 1/2027 of 01/01/2027, as its text says. That law was repealed in turn; the law in "
        "force now is Law n° 3/2030 of 01/01/2030. Made under Law n° 5/2020, which Law n° 9/2025 has since "
        "repealed: check it is still in force."
    )
    # Where the listing disagrees, the repealing law, a primary source, leads; the listing is noted beside it.
    disagree = V.suggestions({**document, "suggested_legal": listed, "relations": [repealer]})
    noted = f"{repealed} A secondary source says otherwise: Listed in force. Check which is right."
    assert disagree["status.legal"] == {
        "value": "repealed",
        "page": None,
        "evidence": noted,
        "secondary": {"value": "in_force", "evidence": "Listed in force."},
    }
    assert disagree["status.note"]["value"] == noted
    # A document prepared before legal facts were kept apart: its reader's authorities, commencement and
    # status are not filled in either.
    older = {
        **document,
        "suggested": {
            "identity.family": "legislation",
            "identity.type": "organic_law",
            "identity.number.value": "009/2026.OL",
            "step:commenced.date": "2026-03-12",
            "profile.commencement": "on_publication",
            "status.legal": "in_force",
            "status.note": "Listed in force.",
            "expressions": [{"language": "rw", "authority": "authoritative", "authority_page": "2"}],
        },
    }
    assert V.base_values(older, {}) == {**values, "identity.number.value": "009/2026.OL",
                                        "expressions": [{"language": "rw"}]}  # fmt: skip
    specs = V.fields("legislation", "organic_law", rwanda)
    keys = {s.key for s in specs}
    assert {"step:adopted:senate.date", "step:countersigned.person", "step:sealed.person"} <= keys
    assert "step:passed:senate.date" not in keys  # Nigeria's steps stay Nigeria's
    values |= {
        "identity.full_title": "Organic Law n° 009/2026.OL of 12/03/2026 governing examples",
        "identity.number.value": "009/2026.OL",
        "identity.citation": "Organic Law n° 009/2026.OL of 12/03/2026 governing examples",
        "issued_by": "Parliament of the Republic of Rwanda",
        "step:adopted:deputies.date": "2026-02-20",
        "step:adopted:senate.date": "2026-02-17",
        "step:promulgated.date": "2026-03-12",
        "step:promulgated.person": "EXAMPLE Name, President of the Republic",
        "step:countersigned.date": "2026-03-12",
        "step:countersigned.person": "SAMPLE Name, Prime Minister",
        "step:sealed.date": "2026-03-12",
        "step:sealed.person": "OTHER Name, Minister of Justice and Attorney General",
        "step:published.date": "2026-03-12",
        "step:commenced.date": "2026-03-12",
        "publication.name": "Official Gazette of the Republic of Rwanda",
        "publication.number": "11 Bis",
        "text.form": "gazette_publication",
        "text.summary.text": "Governs examples.",
        "expressions": [
            {"language": "rw", "authority": "authoritative", "this_text": "yes"},
            {"language": "en", "authority": "official_translation", "this_text": "no"},
            {"language": "fr", "authority": "official_translation", "this_text": "no"},
        ],
    }
    absent = {"profile.bill": "not_in_source", "profile.revised_edition": "not_applicable"}
    assert V.missing(values, absent, specs) == []
    place, _ = V.place_for({"jurisdiction": "rw"}, {}, registry())
    record = V.to_record(values, absent, place, rwanda, "rw-example")
    assert R.missing(record, expected_steps(rwanda, "organic_law")) == []
    sealed = next(e for e in record.events if e.step == "sealed")
    assert sealed.kind == "certified" and sealed.person.name.startswith("OTHER Name")
    assert [e.language for e in record.expressions] == ["rw", "en", "fr"]


def test_a_repealing_laws_own_words_lead_the_status_suggestion_over_a_listing():
    """Where a later law of the collection repeals a law that RLRC's listing files among the laws in force,
    the suggestion is the repealing law's: the words of its provision that repeals it, from which law and
    on which page. The listing, a secondary source, is noted beside it. Both stay suggestions."""
    from workbench import record_view as V

    listing = "RLRC's Laws of Rwanda files it among the laws in force (listing read 2026-10-11)."
    words = "Article 105: Repealing provision Law n° 026/2019 of 8/09/2019 on tax procedures are repealed."
    repealer = {"work": "rw-law-020-2023", "title": "Law n° 020/2023", "words": words, "page": 107}
    document = {
        "type": "law",
        "title": "Law n° 026/2019 of 8/09/2019 on tax procedures",
        "suggested_legal": {
            "status.legal": {"value": "in_force", "page": None, "evidence": listing},
            "status.note": {"value": listing, "page": None, "evidence": listing},
        },
        "currency": {"status": "repealed", "basis": "text", "repealed_by": [repealer]},
    }
    suggested = V.suggestions(document)
    assert suggested["status.legal"] == {
        "value": "repealed",
        "page": None,  # not a page of this law: the repealing law's, under "source"
        "evidence": words,
        "source": {"work": "rw-law-020-2023", "title": "Law n° 020/2023", "page": 107},
        "secondary": {"value": "in_force", "evidence": listing},
    }
    assert suggested["status.note"]["value"] == (
        f"Repealed by Law n° 020/2023, as its text says (p. 107). A secondary source says otherwise: {listing} "
        "Check which is right."
    )
    # A listing that agrees is still the secondary source.
    agreeing = listing.replace("laws in force", "repealed laws")
    document["suggested_legal"]["status.legal"] |= {"value": "repealed", "evidence": agreeing}
    suggested = V.suggestions(document)
    assert suggested["status.legal"]["evidence"] == words
    assert suggested["status.legal"]["secondary"] == {"value": "repealed", "evidence": agreeing}
    assert suggested["status.note"]["value"].endswith(f"A secondary source agrees: {agreeing}")
    # Never filled in: the first draft leaves the status not yet checked.
    assert V.base_values(document, {})["status.legal"] == "unknown"


def test_a_suggested_family_that_does_not_go_with_its_kind_is_put_right_not_dropped():
    """Rwanda's reader filed agreements, regulations and ordinances as executive instruments; the first
    draft kept none of their suggestions until 2026-10-29. Now the family is put right from the kind,
    the rest kept, and the reviewer told."""
    from workbench import record_view as V

    treaty = {
        "type": "agreement",
        "title": "Treaty",
        "suggested": {
            "identity.family": "executive_instrument",
            "identity.type": "agreement",
            "identity.title": "Treaty of the African Medicines Agency (AMA)",
            "publication.name": "Official Gazette of the Republic of Rwanda",
        },
    }
    values = V.base_values(treaty, {})
    assert values["identity.family"] == "treaty" and values["identity.type"] == "agreement"
    assert values["identity.title"] == "Treaty of the African Medicines Agency (AMA)"
    assert values["publication.name"] == "Official Gazette of the Republic of Rwanda"
    used, notices = V.usable_suggestions(treaty)
    assert used["identity.family"] == "treaty" and len(notices) == 1 and "Treaty" in notices[0]
    unknown = {**treaty, "suggested": {**treaty["suggested"], "identity.type": "pamphlet"}}
    used, notices = V.usable_suggestions(unknown)
    assert (
        "identity.type" not in used and used["identity.title"].startswith("Treaty") and "choose" in notices[0]
    )
    assert (
        V.usable_suggestions({"suggested": {"identity.family": "treaty", "identity.type": "agreement"}})[1]
        == []
    )


def test_a_judgments_summary_and_outcome_are_optional_for_now():
    """Owner, 2026-10-29: a judgment is approved with its details and text; its summary and outcome are
    added later. Everything else keeps its summary required."""
    from schema.jurisdictions import registry
    from workbench import record_view as V

    ng = registry()["ng"]
    judgment = {s.key: s.required for s in V.fields("judgment", "judgment", ng)}
    assert judgment["text.summary.text"] is False and judgment["profile.outcome"] is False
    law = {s.key: s.required for s in V.fields("legislation", "act", ng)}
    assert law["text.summary.text"] is True
