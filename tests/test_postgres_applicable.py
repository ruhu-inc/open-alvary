"""Regional and treaty law (JUR-03, JUR-04) served and applied: a regional instrument's legal effect and a
treaty's parties are published with the record, served on its view, its anchors and its evidence units;
an opt-in applicable-law scope (scope=applicable) widens a search of a country to the law that binds there,
from reviewed memberships and recorded ratifications only, labels every unit's relation to that country,
states its widening (RSV-03), and changes nothing when not asked for. /currentness says whether a record
binds in a country on a date. Every record, membership, ratification and date here is a synthetic fixture,
not research."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_memberships import row, write

from api.main import create_app
from api.store import Store
from api.v1 import create_v1_app
from operations import memberships, publish
from schema import record as R
from workbench import publication

pytestmark = pytest.mark.postgres
AS_OF = "2026-01-01"
MEMBERSHIPS = [
    row("sn", "aa-au", "2001-05-26"),
    row("sn", "aa-ecowas", "1975-05-28"),
    row("sn", "aa-ohada", "1995-09-18"),
    row("ke", "aa-au", "2001-05-26"),
    row("ke", "aa-eac", "2000-07-07"),
    row("ng", "aa-ecowas", "1975-05-28"),  # Lagos State (ng-la) belongs to no body itself: Nigeria does
    row("cm", "aa-ohada", None, status="proposal"),  # not reviewed: never served, never applied
]
NOW = datetime.now(UTC).replace(microsecond=0)
DECISION = {
    "basis": "Independently authored catalogue entry",
    "legal_note": "Synthetic fixture.",
    "evidence": [],
    "expires_at": (NOW + timedelta(days=365)).isoformat(),
}
TEXT_DECISION = {
    "document_types": ["act", "other"],
    "rights_holder": "Synthetic fixture",
    "licence": "TEST-ONLY-NOT-A-LEGAL-LICENCE",
    "licence_url": None,
    "attribution_required": False,
    "attribution_text": None,
    "database_rights_notes": "Synthetic fixture.",
    "privacy_status": "cleared",
    "basis": "Synthetic fixture.",
    "evidence": ["https://www.example.org/permission"],
    "decided_by": "Test Reviewer",
    "decided_at": (NOW - timedelta(minutes=1)).isoformat(),
    "expires_at": (NOW + timedelta(days=365)).isoformat(),
}
LEVELS = {"aa-au": "continental", "ng-la": "subnational"}
ORGAN = R.Body(name="Council of Ministers", kind="regional_body")


def regional(effect, **fields):
    return R.RegionalProfile(family="regional_instrument", organ=ORGAN, legal_effect=effect, **fields)


# work → (place, family, type, title, profile, words, relations, events)
WORKS = {
    "ohada-commercial-companies-act": (
        "aa-ohada", "regional_instrument", "uniform_act", "Uniform Act on Commercial Companies",
        regional("directly_applicable"), "1. Every commercial company shall have a registered office.", (), (),
    ),
    "ohada-registry-report": (
        "aa-ohada", "official_document", "report", "Commercial Registry Report",
        R.OtherProfile(family="official_document"), "1. Commercial company registers are kept in each state.",
        (), (),
    ),
    "ecowas-company-accounts-directive": (
        "aa-ecowas", "regional_instrument", "directive", "Directive on Company Accounts",
        regional("requires_transposition", transposition_deadline=date(2010, 12, 31)),
        "1. Member States shall require every commercial company to publish its accounts.", (), (),
    ),
    "ecowas-register-decision": (
        "aa-ecowas", "regional_instrument", "decision", "Decision on a Company Register",
        regional("binding_on_addressees", member_states=("sn",)),
        "1. The addressee shall open a commercial company register.", (), (),
    ),
    "ecowas-registers-recommendation": (
        "aa-ecowas", "regional_instrument", "decision", "Recommendation on Company Registers",
        regional("non_binding"), "1. Member States should encourage commercial company registers.", (), (),
    ),
    "eac-customs-management-act": (
        "aa-eac", "regional_instrument", "community_act", "Customs Management Act",
        regional("directly_applicable"), "1. Customs duties are levied on goods imported into Partner States.",
        (), (),
    ),
    "au-trade-charter": (
        "aa-au", "treaty", "charter", "Charter on Trade Facilitation",
        R.TreatyProfile(
            family="treaty", adopted_by=R.Body(name="Assembly of the Union", kind="international_organisation"),
            depositary="African Union Commission", authentic_languages=("en", "fr"),
            parties=(
                R.TreatyParty(party="Kenya", place="ke", signed=R.Dated(value=date(2003, 7, 11)),
                              ratified=R.Dated(value=date(2005, 3, 1))),
                R.TreatyParty(party="Ghana", place="gh", signed=R.Dated(value=date(2004, 2, 2))),
            ),
            parties_complete_as_of=date(2026, 6, 1),
            parties_complete_evidence=R.Evidence(source="external", url="https://www.example.org/status-list.pdf"),
        ),
        "1. States Parties shall simplify customs procedures for commercial cooperation.", (),
        (R.Event(kind="entered_into_force", when=R.Dated(value=date(2005, 1, 1))),),
    ),
    "sn-company-accounts-act": (
        "sn", "legislation", "act", "Company Accounts Act",
        R.LegislationProfile(family="legislation"),
        "1. Every commercial company registered in Senegal shall publish its accounts yearly.",
        (R.Relation(kind="transposes", target=R.Reference(citation="Directive on Company Accounts",
                                                          work_uri="ecowas-company-accounts-directive")),),
        (),
    ),
    "ke-customs-act": (
        "ke", "legislation", "act", "Customs Act",
        R.LegislationProfile(family="legislation"), "1. Customs officers shall examine goods at the border.",
        (R.Relation(kind="domesticates", target=R.Reference(citation="Charter on Trade Facilitation",
                                                            work_uri="au-trade-charter")),),
        (),
    ),
    "gh-companies-act": (
        "gh", "legislation", "act", "Companies Act",
        R.LegislationProfile(family="legislation"), "1. A commercial company shall file returns.", (), (),
    ),
    "ng-companies-act": (
        "ng", "legislation", "act", "Federal Companies Act",
        R.LegislationProfile(family="legislation"),
        "1. Every commercial company in the Federation shall keep a register of its members.", (), (),
    ),
    "ng-la-companies-law": (
        "ng-la", "legislation", "law", "Lagos State Companies Premises Law",
        R.LegislationProfile(family="legislation"),
        "1. A commercial company trading in the State shall register its premises.", (), (),
    ),
}  # fmt: skip
FIRST = ("aa-ohada", "aa-ecowas", "aa-eac", "aa-au")  # published before the national acts that link to them


def document(work):
    place, family, type_, title, profile, _, relations, events = WORKS[work]
    doctype = R.FAMILIES[family][0]
    return R.DocumentRecord(
        identity=R.Identity(
            family=family, type=type_, work_uri=f"/akn/{place}/{doctype}/{type_.replace('_', '-')}/2020/1",
            title=title, citations=(R.Citation(kind="official", text=title),),
        ),
        place=R.Place(code=place, name=place, level=LEVELS.get(place, "regional" if place.startswith("aa-")
                                                             else "national")),
        expressions=(R.Expression(language="en", this_text=True),),
        events=events,
        publications=(R.Publication(kind="official_website", name="Official site",
                                    url=f"https://www.example.org/{work}.pdf"),),
        status=R.Status(legal="in_force"),
        relations=relations,
        text=R.TextInfo(form="official_website", version=R.Version(label="As adopted")),
        classification=R.Classification(hierarchy="regional" if place.startswith("aa-") else "primary"),
        profile=profile,
    )  # fmt: skip


def publish_fixture(store):
    """The records, through operations.publish, then a reviewed text of each in force from 2000."""
    for place in (*FIRST, "sn", "ke", "gh", "ng", "ng-la"):
        items = [
            publish.Approved(work, document(work), ["en"], "Test Reviewer", NOW - timedelta(minutes=10))
            for work, spec in WORKS.items()
            if spec[0] == place
        ]
        report = publish.publish_records(store, place, items, set(), DECISION)
        assert not report["skipped"], report
    texts = [
        publish.ApprovedText(
            document_id=work, work=work, language="en", document_type="act" if spec[1] == "legislation" else "other",
            text=publication.build({"id": work, "jurisdiction": "ng", "type": "act"},
                                   [{"page": 1, "units": [{"kind": "paragraph", "text": spec[5]}]}]),
            approver="Test Reviewer", approved_at=NOW - timedelta(minutes=5), effective_from=date(2000, 1, 1),
            provenance={"url": f"https://www.example.org/{work}.pdf", "retrieved_at": "2026-10-09T06:41:17+00:00",
                        "sha256": "7" * 64},
        )
        for work, spec in WORKS.items()
    ]  # fmt: skip
    report = publish.publish_texts(store, "sn", publish.Texts(approved=texts), TEXT_DECISION)
    assert len(report["published"]) == len(WORKS), report


@pytest.fixture
def published(pg_engine, tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    store = Store(pg_engine)
    memberships.publish(pg_engine, write(tmp_path / "memberships.json", MEMBERSHIPS))
    publish_fixture(store)
    return store, TestClient(create_v1_app(store, tmp_path, lambda: [])), tmp_path


def retrieve(client, q, **params):
    answer = client.get("/retrieve", params={"q": q, **params})
    assert answer.status_code == 200, answer.text
    return answer.json()


def by_source(units):
    return {u["source_id"]: u for u in units}


def anchor(store, work):
    corpus = store.load()
    version = next(v for v in corpus.versions if v.source_id == work)
    return next(n.id for n in corpus.structure if n.version_id == version.id and n.locator == "sec_1")


def test_the_record_and_its_anchors_carry_the_regional_effect_and_the_treaty_parties(published):
    store, client, _ = published
    directive = client.get("/sources/ecowas-company-accounts-directive").json()
    assert directive["regional"] == {"organ": "Council of Ministers", "legal_effect": "requires_transposition",
                                     "member_states": [], "transposition_deadline": "2010-12-31"}  # fmt: skip
    assert "treaty" not in directive
    charter = client.get("/sources/au-trade-charter").json()["treaty"]
    assert charter["entered_into_force"] == {"value": "2005-01-01", "precision": "day"}
    kenya, ghana = charter["parties"]
    assert kenya["ratified"]["value"] == "2005-03-01" and kenya["signed"]["value"] == "2003-07-11"
    assert [a["source_id"] for a in kenya["domesticated_by"]] == ["ke-customs-act"]
    assert ghana == {"party": "Ghana", "place": "gh", "signed": {"value": "2004-02-02", "precision": "day"},
                     "domesticated_by": []}  # fmt: skip
    assert charter["parties_complete_as_of"] == "2026-06-01"
    assert charter["parties_complete_evidence"] == {
        "source": "external", "url": "https://www.example.org/status-list.pdf"
    }  # fmt: skip
    part = client.get(f"/anchors/{anchor(store, 'au-trade-charter')}").json()
    assert part["treaty"] == charter and "regional" not in part
    national = client.get("/sources/sn-company-accounts-act").json()
    assert "regional" not in national and "treaty" not in national
    assert "regional" not in client.get(f"/anchors/{anchor(store, 'sn-company-accounts-act')}").json()


def test_each_unit_carries_its_records_legal_effect_or_treaty_status(published):
    _, client, _ = published
    ohada = by_source(retrieve(client, "registered office")["evidence"])["ohada-commercial-companies-act"]
    assert ohada["regional"] == {"organ": "Council of Ministers", "legal_effect": "directly_applicable",
                                 "member_states": []}  # fmt: skip
    assert "treaty" not in ohada and "applicability" not in ohada
    charter = by_source(retrieve(client, "customs procedures")["evidence"])["au-trade-charter"]
    # A treaty's parties, and whether the list is complete, are on the record and its anchors, not on each
    # unit.
    assert charter["treaty"] == {
        "adopted_by": "Assembly of the Union", "depositary": "African Union Commission",
        "entered_into_force": {"value": "2005-01-01", "precision": "day"}, "authentic_languages": ["en", "fr"],
    }  # fmt: skip
    national = by_source(retrieve(client, "border")["evidence"])["ke-customs-act"]
    assert not {"regional", "treaty", "applicability"} & set(national)


def test_a_senegal_applicable_query_on_company_law_returns_the_ohada_uniform_act_as_regional_direct(
    published,
):
    _, client, _ = published
    pack = retrieve(client, "commercial company", jurisdiction="sn", scope="applicable", as_of=AS_OF)
    units = by_source(pack["evidence"])
    ohada = units["ohada-commercial-companies-act"]["applicability"]
    assert (ohada["relation"], ohada["binds"], ohada["jurisdiction_id"], ohada["as_of"]) == (
        "regional_direct", "bound", "sn", AS_OF,
    )  # fmt: skip
    assert ohada["memberships"] == [{"id": "sn:aa-ohada:1", "body_id": "aa-ohada", "held": "held"}]
    assert ohada["regional"] == {"organ": "Council of Ministers", "legal_effect": "directly_applicable",
                                 "member_states": []}  # fmt: skip
    assert units["ohada-commercial-companies-act"]["regional"]["legal_effect"] == "directly_applicable"
    assert units["sn-company-accounts-act"]["applicability"]["relation"] == "national"
    decision = units["ecowas-register-decision"]["applicability"]
    assert decision["relation"] == "regional_binding_on_addressees" and decision["regional"][
        "member_states"
    ] == ["sn"]
    # A body's record that states no legal effect is kept, labelled unknown: reported, never dropped.
    assert units["ohada-registry-report"]["applicability"]["relation"] == "unknown"
    # What does not bind Senegal is left out, and the pack says so, by relation.
    assert "ecowas-registers-recommendation" not in units and "au-trade-charter" not in units
    scope = pack["applicability"]
    assert (
        scope["jurisdiction_id"] == "sn"
        and scope["date_basis"] == "as_of"
        and scope["membership"] == "reviewed"
    )
    assert [(b["body_id"], b["held"]) for b in scope["bodies"]] == [
        ("aa-au", "held"), ("aa-ecowas", "held"), ("aa-ohada", "held"),
    ]  # fmt: skip
    assert scope["left_out"] == {"non_binding": 1, "treaty_not_in_force_for_state": 1}
    assert scope["admitted"] == 4 and any("labelled unknown" in n for n in scope["notices"])
    # The caller's filters are shown as given; the widening is stated beside them, never applied silently.
    assert pack["query"]["filters"] == {"jurisdictions": ["sn"], "scope": "applicable"}
    assert retrieve(client, "commercial company", jurisdiction="sn", scope="applicable", as_of=AS_OF) == pack


def test_a_kenya_applicable_query_on_customs_returns_the_eac_act_and_the_ratified_treaty(published):
    _, client, _ = published
    pack = retrieve(client, "customs", jurisdiction="ke", scope="applicable", as_of=AS_OF)
    units = by_source(pack["evidence"])
    eac = units["eac-customs-management-act"]["applicability"]
    assert eac["relation"] == "regional_direct" and eac["memberships"][0]["id"] == "ke:aa-eac:1"
    assert units["ke-customs-act"]["applicability"]["relation"] == "national"
    charter = units["au-trade-charter"]
    assert charter["applicability"]["relation"] == "treaty_in_force_for_state"
    party = charter["applicability"]["party"]
    assert party["place"] == "ke" and party["ratified"] == {"value": "2005-03-01", "precision": "day"}
    assert [a["source_id"] for a in party["domesticated_by"]] == ["ke-customs-act"]
    assert charter["applicability"]["entered_into_force"]["value"] == "2005-01-01"
    # On a unit, the treaty's status without its parties: they are on the record.
    assert charter["treaty"] == {
        "adopted_by": "Assembly of the Union", "depositary": "African Union Commission",
        "entered_into_force": {"value": "2005-01-01", "precision": "day"}, "authentic_languages": ["en", "fr"],
    }  # fmt: skip


def test_an_ecowas_directive_requires_transposition_with_its_deadline_and_transposing_act(published):
    _, client, _ = published
    page = client.get(
        "/search/text",
        params={"q": "publish accounts", "jurisdiction": "sn", "scope": "applicable", "as_of": AS_OF},
    ).json()
    hits = {h["source_id"]: h for h in page["items"]}
    directive = hits["ecowas-company-accounts-directive"]["applicability"]
    assert (directive["relation"], directive["binds"]) == ("regional_requires_transposition", "bound")
    assert directive["regional"]["transposition_deadline"] == "2010-12-31"
    assert [a["source_id"] for a in directive["transposed_by"]] == ["sn-company-accounts-act"]
    assert "Transposition was due by 2010-12-31." in directive["reasons"]
    assert hits["sn-company-accounts-act"]["applicability"]["relation"] == "national"
    assert page["applicability"]["left_out"] == {"non_binding": 1, "treaty_not_in_force_for_state": 1}


def test_an_au_treaty_binds_the_state_that_ratified_it_and_not_one_without_a_ratification(published):
    store, client, _ = published
    # Named by source_id, the treaty is answered for Senegal with its relation: missing from a party list
    # complete as of 2026-06-01, so not a party on 2026-01-01.
    pack = retrieve(
        client,
        "customs procedures",
        jurisdiction="sn",
        scope="applicable",
        as_of=AS_OF,
        source_id="au-trade-charter",
    )
    [unit] = [u for u in pack["evidence"] if u["unit"] == "provision"]
    assert unit["applicability"]["relation"] == "treaty_not_in_force_for_state"
    assert unit["applicability"]["binds"] == "not_bound" and "party" not in unit["applicability"]
    assert any("named by source_id" in n for n in pack["applicability"]["notices"])
    # The check says the same for Senegal, and that Kenya, which ratified it, is bound.
    part = anchor(store, "au-trade-charter")
    items = [{"anchor_id": part, "as_of": AS_OF, "jurisdiction": j} for j in ("sn", "ke", "gh")]
    results = client.post("/currentness", json={"items": items}).json()["results"]
    found = [(r["applicability"]["relation"], r["applicability"]["binds"]) for r in results]
    assert found == [
        ("treaty_not_in_force_for_state", "not_bound"),
        ("treaty_in_force_for_state", "bound"),
        ("treaty_not_in_force_for_state", "not_bound"),  # Ghana signed only: signature alone does not bind
    ]
    assert results[0]["currentness"]["state"] == "in_force" and results[0]["result"] == "unchanged"
    assert "signature alone" in results[2]["applicability"]["reasons"][0]
    # After the date the list is complete as of, Senegal's absence proves nothing: unknown. Ghana's own
    # entry, its signature alone, still does not bind.
    later = [{"anchor_id": part, "as_of": "2026-09-01", "jurisdiction": j} for j in ("sn", "gh")]
    senegal, ghana = client.post("/currentness", json={"items": later}).json()["results"]
    assert (senegal["applicability"]["relation"], senegal["applicability"]["binds"]) == ("unknown", "unknown")
    assert (
        "the party list is not known to be complete on 2026-09-01" in senegal["applicability"]["reasons"][0]
    )
    assert ghana["applicability"]["relation"] == "treaty_not_in_force_for_state"
    # Before the treaty entered into force, it bound no one.
    [early] = client.post(
        "/currentness", json={"items": [{"anchor_id": part, "as_of": "2004-06-01", "jurisdiction": "ke"}]}
    ).json()["results"]
    assert early["applicability"]["relation"] == "treaty_not_in_force_for_state"
    # Without a jurisdiction, the check is as before.
    [plain] = client.post("/currentness", json={"items": [{"anchor_id": part}]}).json()["results"]
    assert "applicability" not in plain


def test_a_lagos_state_applicable_query_admits_federal_law_and_ecowas_law_through_nigeria(published):
    store, client, _ = published
    pack = retrieve(client, "commercial company", jurisdiction="ng-la", scope="applicable", as_of=AS_OF)
    units = by_source(pack["evidence"])
    # Nigeria's federal Act applies throughout Lagos State: national, and the basis says why.
    federal = units["ng-companies-act"]["applicability"]
    assert (federal["relation"], federal["binds"], federal["jurisdiction_id"]) == (
        "national",
        "bound",
        "ng-la",
    )
    assert federal["reasons"] == ["ng-la is within ng: the law of ng applies throughout it."]
    assert units["ng-la-companies-law"]["applicability"]["relation"] == "national"
    # The ECOWAS directive reaches Lagos State through Nigeria's reviewed membership.
    directive = units["ecowas-company-accounts-directive"]["applicability"]
    assert directive["relation"] == "regional_requires_transposition"
    assert directive["memberships"] == [{"id": "ng:aa-ecowas:1", "body_id": "aa-ecowas", "held": "held"}]
    assert directive["reasons"][0] == "ng-la is within ng: regional bodies and treaties reach it through ng."
    # Not Senegal's law, nor OHADA's (no Nigerian membership is reviewed), nor what binds Senegal only.
    assert not {
        "sn-company-accounts-act",
        "ohada-commercial-companies-act",
        "ecowas-register-decision",
    } & set(units)
    scope = pack["applicability"]
    assert (scope["jurisdiction_id"], scope["within"], scope["admitted"]) == ("ng-la", "ng", 1)
    assert [(b["body_id"], b["held"]) for b in scope["bodies"]] == [("aa-ecowas", "held")]
    assert scope["left_out"] == {"comparative": 1, "non_binding": 1}
    assert scope["notices"][0].startswith("ng-la is within ng: the law of ng applies throughout it")
    assert pack["query"]["filters"] == {"jurisdictions": ["ng-la"], "scope": "applicable"}
    # Text search answers the same; without the scope, Lagos State's own law only.
    page = client.get(
        "/search/text",
        params={"q": "commercial company", "jurisdiction": "ng-la", "scope": "applicable", "as_of": AS_OF},
    ).json()
    assert {h["source_id"] for h in page["items"]} == set(units) and page["applicability"] == scope
    plain = retrieve(client, "commercial company", jurisdiction="ng-la", as_of=AS_OF)
    assert {u["source_id"] for u in plain["evidence"]} == {"ng-la-companies-law"}
    # The check says the same of the federal Act.
    [checked] = client.post(
        "/currentness",
        json={
            "items": [
                {"anchor_id": anchor(store, "ng-companies-act"), "as_of": AS_OF, "jurisdiction": "ng-la"}
            ]
        },
    ).json()["results"]
    assert checked["applicability"]["relation"] == "national"


def test_regional_instruments_bind_through_membership_on_the_date(published):
    store, client, _ = published
    ohada = anchor(store, "ohada-commercial-companies-act")
    items = [
        {"anchor_id": ohada, "as_of": AS_OF, "jurisdiction": "sn"},
        {"anchor_id": ohada, "as_of": "1990-01-01", "jurisdiction": "sn"},  # before Senegal joined
        {"anchor_id": anchor(store, "eac-customs-management-act"), "as_of": AS_OF, "jurisdiction": "sn"},
    ]
    results = client.post("/currentness", json={"items": items}).json()["results"]
    assert [r["applicability"]["relation"] for r in results] == ["regional_direct", "comparative", "unknown"]
    assert results[1]["applicability"]["memberships"] == [
        {"id": "sn:aa-ohada:1", "body_id": "aa-ohada", "held": "not_held"}
    ]
    # Senegal has no reviewed EAC membership: unknown, not "not a member".
    assert "No membership of sn in aa-eac" in results[2]["applicability"]["reasons"][0]
    refused = client.post("/currentness", json={"items": [{"anchor_id": ohada, "jurisdiction": "aa-ecowas"}]})
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_input"


def test_an_unreviewed_membership_is_unknown_and_adds_no_body(published):
    store, client, _ = published
    pack = retrieve(client, "commercial company", jurisdiction="cm", scope="applicable", as_of=AS_OF)
    assert pack["evidence"] == [] and pack["coverage"]["texts_searched"] == 0
    scope = pack["applicability"]
    assert scope["membership"] == "unknown" and scope["bodies"] == [] and scope["admitted"] == 0
    assert "never means cm belongs to none" in scope["notices"][0]
    [result] = client.post(
        "/currentness",
        json={
            "items": [{"anchor_id": anchor(store, "ohada-commercial-companies-act"), "jurisdiction": "cm"}]
        },
    ).json()["results"]
    assert result["applicability"]["relation"] == "unknown" and result["applicability"]["binds"] == "unknown"


def test_a_named_record_is_answered_with_its_relation_whatever_it_is(published):
    _, client, _ = published

    def relation(work):
        pack = retrieve(client, "commercial company", jurisdiction="sn", scope="applicable", as_of=AS_OF,
                        source_id=work)  # fmt: skip
        return {u["applicability"]["relation"] for u in pack["evidence"]}

    assert relation("gh-companies-act") == {"comparative"}
    assert relation("ecowas-registers-recommendation") == {"non_binding"}


def test_without_scope_applicable_nothing_changes(published):
    _, client, _ = published
    pack = retrieve(client, "commercial company", jurisdiction="sn", as_of=AS_OF)
    assert {u["source_id"] for u in pack["evidence"]} == {"sn-company-accounts-act"}
    assert "applicability" not in pack and pack["query"]["filters"] == {"jurisdictions": ["sn"]}
    assert all("applicability" not in u and "regional" not in u for u in pack["evidence"])
    page = client.get("/search/text", params={"q": "commercial company", "jurisdiction": "sn"}).json()
    assert {h["source_id"] for h in page["items"]} == {"sn-company-accounts-act"}
    assert "applicability" not in page and all("applicability" not in h for h in page["items"])
    # A regional instrument found without a jurisdiction carries its legal effect, never a relation.
    everywhere = by_source(retrieve(client, "registered office")["evidence"])
    unit = everywhere["ohada-commercial-companies-act"]
    assert unit["regional"]["legal_effect"] == "directly_applicable" and "applicability" not in unit
    # The scope is asked of one country.
    for params in ({}, {"jurisdiction": ["sn", "ke"]}, {"jurisdiction": "aa-ecowas"}):
        refused = client.get("/retrieve", params={"q": "company", "scope": "applicable", **params})
        assert refused.status_code == 400 and refused.json()["code"] == "invalid_input", params
    refused = client.get("/search/text", params={"q": "company", "scope": "applicable"})
    assert refused.status_code == 400


def test_the_mcp_tools_answer_the_same_scope_and_check(published):
    store, client, tmp_path = published
    mcp = TestClient(create_app(store, tmp_path))

    def call(name, arguments):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        return json.loads(mcp.post("/mcp", json=body).json()["result"]["content"][0]["text"])

    pack = call("retrieve_evidence", {"query": "customs", "jurisdictions": ["ke"], "scope": "applicable",
                                      "as_of": AS_OF})  # fmt: skip
    rest = retrieve(client, "customs", jurisdiction="ke", scope="applicable", as_of=AS_OF, max_units=8,
                    max_evidence_bytes=24_576)  # fmt: skip
    assert pack == rest
    page = call("search_text", {"query": "publish accounts", "jurisdiction": "sn", "scope": "applicable"})
    assert page["applicability"]["date_basis"] == "today"
    part = anchor(store, "au-trade-charter")
    checked = call(
        "check_currentness", {"items": [{"anchor_id": part, "jurisdiction": "ke", "as_of": AS_OF}]}
    )
    assert checked["results"][0]["applicability"]["relation"] == "treaty_in_force_for_state"
