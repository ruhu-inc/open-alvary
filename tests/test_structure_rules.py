"""Which structure rules a document's text is read by (TXP-03): an explicit table by jurisdiction and
type, with no default. A document no rules are written for is refused, at approval and at publishing,
rather than read by another's rules (DAT-05); the documents that have rules keep exactly the structure
they had."""

import json

import pytest
from test_consolidation import FINAL
from test_judgment_structure import JUDGMENT
from test_text_publication import ACT, RWANDA
from test_workbench import ACT as WORKBENCH_ACT
from test_workbench import approve, confirm_details, flag_id, save, signed_in
from test_workbench import world as world  # noqa: F401  (fixture)

from workbench import publication, serialize, structure


def paged(units_by_page):
    return [{"page": n, "units": units} for n, units in enumerate(units_by_page, start=1)]


# The repository's fixtures, with the structure each had when the rules were chosen by falling through
# to Nigerian Acts: the same rules, the same nodes, byte for byte.
UNCHANGED = [
    (
        {"id": "ng-example-act-2023", "jurisdiction": "ng", "type": "act"},
        paged(ACT),
        "ng-federal-act-1",
        "4ecc05176589628fdbdb4763131aa93613166b352cb1ff9e0467b0f739c849f9",
    ),
    (
        {"id": "rw-law-2024-1-en", "jurisdiction": "rw", "type": "law", "language": "en"},
        paged(RWANDA),
        "rw-law-1",
        "f820ad6c244de0791a1f5919af4f7a1f5087ca44464a510ded15ebc6be3c0287",
    ),
    (
        {"id": "ng-sc-2019-4", "jurisdiction": "ng", "type": "judgment"},
        paged(JUDGMENT),
        "judgment-paragraphs-1",
        "2412236829856d2195dae7af168a0cdb3b7c6149438681b1aa5c065a77d3c5f2",
    ),
    (
        {"id": "ng-example-act-2023", "jurisdiction": "ng", "type": "act"},
        FINAL,
        "ng-federal-act-1",
        "60d56f3ea99a3ac4aa02e6a52b4029b8d41a5bc0ae1fe7438a2643cdd862cee9",
    ),
]


@pytest.mark.parametrize("document, pages, rules, structure_sha256", UNCHANGED)
def test_documents_with_rules_keep_exactly_the_structure_they_had(document, pages, rules, structure_sha256):
    built = publication.build(document, pages)
    assert (built.rules_id, built.structure_sha256) == (rules, structure_sha256)
    assert not any(issue.code == structure.NO_RULES for issue in built.issues)


@pytest.mark.parametrize(
    "document, rules",
    [
        ({"id": "ng-electricity-act-2023", "type": "act"}, "ng-federal-act-1"),
        ({"id": "ng-sc-2019-4", "jurisdiction": "ng", "type": "Judgment"}, "judgment-paragraphs-1"),
        ({"id": "ng-sc-2019-5", "jurisdiction": "ng", "type": "act", "document_type": "ruling"}, "judgment-paragraphs-1"),
        *[({"id": f"rw-{kind}-1-en", "jurisdiction": "rw", "type": kind}, "rw-law-1") for kind in structure.RW_INSTRUMENTS],
        ({"id": "aa-eac-act-2005-1", "jurisdiction": "aa-eac", "type": "act"}, "eac-act-1"),
        ({"id": "ke-act-2019-24", "type": "act"}, "ke-act-1"),
        ({"id": "za-act-2013-4", "jurisdiction": "za", "type": "act"}, "za-act-1"),
        ({"id": "gh-act-2012-843", "jurisdiction": "gh", "type": "act"}, "gh-act-1"),
        ({"id": "aa-ohada-auscgie-2014-fr", "jurisdiction": "aa-ohada", "type": "uniform_act"}, "ohada-uniform-act-1"),
        ({"id": "aa-au-charter-1981-en", "jurisdiction": "aa-au", "type": "charter"}, "au-treaty-1"),
        ({"id": "aa-au-protocol-1998-pt", "jurisdiction": "aa-au", "type": "protocol"}, "au-treaty-1"),
        ({"id": "aa-au-convention-2014-fr", "jurisdiction": "aa-au", "type": "convention"}, "au-treaty-1"),
        ({"id": "aa-ecowas-sa-2010-1-en", "jurisdiction": "aa-ecowas", "type": "supplementary_act"}, "ecowas-act-1"),
        ({"id": "aa-ecowas-dir-2011-1-en", "jurisdiction": "aa-ecowas", "type": "directive"}, "ecowas-directive-1"),
        ({"id": "aa-ecowas-reg-2026-3-fr", "jurisdiction": "aa-ecowas", "type": "regulation"}, "ecowas-regulation-1"),
        ({"id": "ci-loi-2013-450", "jurisdiction": "ci", "type": "law"}, "ci-loi-1"),
        ({"id": "sn-loi-2008-12", "jurisdiction": "sn", "type": "law"}, "sn-loi-1"),
        ({"id": "bf-loi-2021-1", "jurisdiction": "bf", "type": "law"}, "bf-loi-1"),
        ({"id": "cm-law-2024-017-en", "jurisdiction": "cm", "type": "law"}, "cm-law-1"),
        ({"id": "ao-lei-22-11", "jurisdiction": "ao", "type": "law"}, "ao-lei-1"),
        ({"id": "mz-lei-2026-13", "jurisdiction": "mz", "type": "law"}, "mz-lei-1"),
    ],
)  # fmt: skip
def test_rules_are_chosen_by_jurisdiction_and_type(document, rules):
    assert structure.rules_for(document) == rules


@pytest.mark.parametrize(
    "document, named",
    [
        ({"id": "aa-ohada-auscgie-2014-fr", "jurisdiction": "aa-ohada", "type": "act"}, "aa-ohada/act"),
        ({"id": "ng-example-order-2024", "jurisdiction": "ng", "type": "order"}, "ng/order"),
        ({"id": "ng-example-2024", "jurisdiction": "ng"}, "ng/(no type)"),
        ({"id": "rw-agreement-00553a47bc45-fr", "jurisdiction": "rw", "type": "agreement"}, "rw/agreement"),
        ({"id": "rw-other-00f8ef71645e-fr", "jurisdiction": "rw", "type": "other"}, "rw/other"),
        ({"id": "rw-sc-2020-1", "jurisdiction": "rw", "type": "judgment"}, "rw/judgment"),
        ({"id": "aa-au-afchpr-2026-9", "jurisdiction": "aa-au", "type": "judgment"}, "aa-au/judgment"),
        (
            {"id": "aa-ecowas-dec-1980-21", "jurisdiction": "aa-ecowas", "type": "decision"},
            "aa-ecowas/decision",
        ),
        ({"id": "ao-decreto-presidencial-2016-108", "jurisdiction": "ao", "type": "decree"}, "ao/decree"),
        ({"id": "cv-lei-2021-121", "jurisdiction": "cv", "type": "law"}, "cv/law"),
        ({"id": "ug-example-act-2024", "type": "act"}, "ug/act"),
        ({"id": "ke-example-order-2024", "jurisdiction": "ke", "type": "order"}, "ke/order"),
        (
            {"id": "aa-eac-regulations-2010-1", "jurisdiction": "aa-eac", "type": "regulations"},
            "aa-eac/regulations",
        ),
    ],
)
def test_a_document_with_no_rules_has_one_node_and_an_error_that_blocks_it(document, named):
    pages = paged([[{"kind": "heading", "text": "TITLE"}, {"kind": "paragraph", "text": "1. Words."}]])
    built = publication.build(document, pages)
    assert structure.rules_for(document) is None and built.rules_id is None
    assert [(i.level, i.code, i.page) for i in built.errors] == [("error", "no_structure_rules", None)]
    assert built.errors[0].message == f"No structure rules for {named}: this layout is not supported yet"
    (root,) = built.structure.nodes
    assert root.leaves == [0, 1] and (root.start, root.end) == (0, len(built.serialized.text.encode()) - 1)
    summary = publication.summary(built)
    assert (
        summary["rules"] is None
        and summary["counts"] == {}
        and summary["errors"][0]["code"] == "no_structure_rules"
    )


def test_a_french_uniform_act_is_read_by_its_own_rules_never_as_a_nigerian_act():
    pages = paged(
        [
            [
                {"kind": "heading", "text": "ACTE UNIFORME RELATIF AU DROIT DES SOCIÉTÉS COMMERCIALES"},
                {"kind": "heading", "text": "LIVRE 1 — DISPOSITIONS GÉNÉRALES"},
                {"kind": "heading", "text": "Article premier"},
                {"kind": "paragraph", "text": "Toute société commerciale est soumise aux dispositions du présent Acte."},
                {"kind": "heading", "text": "Article 10"},
                {"kind": "paragraph", "text": "Les statuts sont établis par acte notarié."},
                {"kind": "paragraph", "text": "Ils mentionnent la forme de la société."},
            ]
        ]
    )  # fmt: skip
    # Read as a Nigerian Act it gave no error and no warning: it could have been approved.
    serialized = serialize.serialize(pages)
    assert not structure.derive(serialized, pages, structure.NG_FEDERAL_ACT).issues
    # Typed "act", the OHADA row does not apply: refused.
    built = publication.build(
        {"id": "aa-ohada-auscgie-2014-fr", "jurisdiction": "aa-ohada", "type": "act"}, pages
    )
    assert [i.code for i in built.issues] == ["no_structure_rules"]
    # Typed "uniform_act", its articles and their alinéas.
    built = publication.build(
        {"id": "aa-ohada-auscgie-2014-fr", "jurisdiction": "aa-ohada", "type": "uniform_act"}, pages
    )
    nodes = {n.locator: n for n in built.structure.nodes}
    assert built.rules_id == "ohada-uniform-act-1" and not built.issues
    assert nodes["book_1"].heading == "DISPOSITIONS GÉNÉRALES"
    assert [n.locator for n in nodes["art_10"].children] == ["art_10__al_1", "art_10__al_2"]


def test_derive_names_only_rules_that_exist():
    with pytest.raises(ValueError, match="No structure rules 'ohada-act-1'"):
        structure.derive(serialize.serialize([]), [], "ohada-act-1")


def ready_to_approve(world):
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    confirm_details(approver)
    return approver


def test_the_workbench_shows_a_document_with_no_rules_and_cannot_approve_it(world, monkeypatch):
    approver = ready_to_approve(world)
    monkeypatch.setattr(structure, "RULES", {})  # no rules for Nigerian Acts either
    refused = approve(approver)
    assert refused.status_code == 409 and refused.json()["readiness"]["text"]["errors"] == 1
    check = approver.get(f"/api/documents/{WORKBENCH_ACT}").json()["text_check"]
    assert check["rules"] is None and check["counts"] == {}
    assert check["errors"] == [
        {
            "level": "error",
            "code": "no_structure_rules",
            "page": None,
            "unit": None,
            "message": "No structure rules for ng/act: this layout is not supported yet",
        }
    ]
    assert approver.get(f"/api/documents/{WORKBENCH_ACT}/canonical").json()["rules"] is None
    monkeypatch.undo()  # with its rules, the same pages approve
    assert approve(approver).json() == {"revision": 1}


def test_publishing_refuses_a_document_approved_before_its_kind_had_to_have_rules(
    world, monkeypatch, tmp_path
):
    from operations import publish
    from schema.jurisdictions import registry
    from workbench.review import Review

    review_store, library = world
    review = Review(review_store, library, registry())
    provenance = {
        "url": "https://example.org/act.pdf",
        "retrieved_at": "2026-10-09T06:41:17+00:00",
        "sha256": "7" * 64,
    }
    (tmp_path / f"{WORKBENCH_ACT}.provenance.json").write_text(json.dumps(provenance))
    approver = ready_to_approve(world)
    assert approve(approver).json() == {"revision": 1}
    assert [
        t.document_id for t in publish.texts_from_workbench(review, library, review_store, "ng").approved
    ] == [WORKBENCH_ACT]
    monkeypatch.setattr(structure, "RULES", {})
    found = publish.texts_from_workbench(review, library, review_store, "ng")
    assert found.approved == [] and found.skipped[WORKBENCH_ACT] == (
        "1 problem(s) in its text's structure, first: No structure rules for ng/act: this layout is not "
        "supported yet"
    )
