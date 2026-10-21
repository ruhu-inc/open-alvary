"""Regional and treaty law (JUR-04) published and served: a regional instrument's legal effect and a
treaty's parties are published with the record and served on its view, its anchors and its evidence
units; every other record's answer is unchanged. Every record, membership, ratification and date here
is a synthetic fixture, not research."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_memberships import row, write

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
LEVELS = {"aa-au": "continental"}
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
    for place in (*FIRST, "sn", "ke", "gh"):
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
    # A treaty's parties are on the record and its anchors, not on each unit.
    assert charter["treaty"] == {
        "adopted_by": "Assembly of the Union", "depositary": "African Union Commission",
        "entered_into_force": {"value": "2005-01-01", "precision": "day"}, "authentic_languages": ["en", "fr"],
    }  # fmt: skip
    national = by_source(retrieve(client, "border")["evidence"])["ke-customs-act"]
    assert not {"regional", "treaty", "applicability"} & set(national)
