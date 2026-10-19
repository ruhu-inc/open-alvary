"""Judgments as retrievable paragraphs (CPR-04, judgment-paragraphs-1): the header, opinions, numbered
paragraphs and orders of a reviewed judgment, and its paragraphs cited by neutral citation."""

import hashlib
from datetime import UTC, datetime, timedelta

from api import text_index
from api.catalogue import LegacyCatalogue
from api.repositories import LegacyRepository
from operations import publish
from schema.models import Authority, Corpus, Jurisdiction, Language, RightsRecord, Source
from schema.public import NodeViewRecord, VersionViewRecord
from workbench import publication, serialize, structure


def para(text, kind="paragraph", **extra):
    return {"kind": kind, "text": text, **extra}


def head(text, **extra):
    return para(text, "heading", **extra)


def build(pages):
    pages = [{"page": n, "units": units} for n, units in enumerate(pages, start=1)]
    text = serialize.serialize(pages)
    return text, structure.derive(text, pages, structure.JUDGMENT)


def by_locator(result):
    return {node.locator: node for node in result.nodes}


def span(text, node):
    return text.text.encode()[node.start : node.end].decode()


def codes(result):
    return [issue.code for issue in result.issues]


JUDGMENT = [
    [
        head("IN THE SUPREME COURT OF NIGERIA"),
        para("HOLDEN AT ABUJA", align="center"),
        para("ON FRIDAY, THE 15TH DAY OF MARCH, 2019"),
        para("BEFORE THEIR LORDSHIPS: JOHN DOE, JSC; JANE ROE, JSC"),
        para("SC. 123/2010"),
        para("BETWEEN"),
        para("ALHAJI MUSA BELLO", kind="list_item", marker="1."),
        para("APPELLANT"),
        para("AND"),
        para("THE STATE — RESPONDENT"),
        head("LEAD JUDGMENT DELIVERED BY JOHN DOE, JSC"),
        para("[1] This appeal is against the judgment of the Court of Appeal."),
        para("The appellant was convicted at trial."),
        para("[2] The Court of Appeal said:"),
        para("“[7] The evidence against the appellant was overwhelming.”", level=1),
        para("The words could not be plainer.", level=1),
        head("Issue one: the confession"),
        para("[3] The confession was voluntary, as the exhibits show."),
        {"kind": "table", "caption": "Exhibits", "rows": [["Exhibit", "Description"], ["P1", "Statement"]]},
    ],
    [
        para("1. Whether the confession was voluntary.", kind="list_item"),
        para("[4] For the reasons above, the first issue is resolved against the appellant."),
        para("For these reasons, the appeal is dismissed."),
        para("The conviction is affirmed.", kind="list_item", marker="1."),
        head("DISSENTING OPINION OF JANE ROE, JSC"),
        para("[1] I regret that I cannot agree."),
        para("[2] The confession was not voluntary."),
        para("For these reasons, I would allow the appeal."),
        head("ORDER"),
        para("The appeal is dismissed."),
    ],
]


def test_a_judgment_is_its_header_its_opinions_their_paragraphs_and_the_orders():
    text, result = build(JUDGMENT)
    nodes = by_locator(result)
    assert [(n.kind, n.locator) for n in result.nodes] == [
        ("document", "document"),
        ("notice", "header"),
        ("opinion", "op_1"),
        ("paragraph", "para_1"),
        ("paragraph", "para_2"),
        ("paragraph", "para_3"),
        ("table", "para_3__table_1"),
        ("paragraph", "para_4"),
        ("order", "order_1"),
        ("opinion", "op_2"),
        ("paragraph", "op_2__para_1"),
        ("paragraph", "op_2__para_2"),
        ("order", "order_2"),
    ]
    header = span(text, nodes["header"])
    assert header.startswith("IN THE SUPREME COURT OF NIGERIA") and header.endswith("THE STATE — RESPONDENT")
    assert "1. ALHAJI MUSA BELLO" in header  # a numbered party is not a paragraph
    lead, dissent = nodes["op_1"], nodes["op_2"]
    assert (lead.label, lead.author, lead.heading) == (
        "lead",
        "JOHN DOE, JSC",
        "LEAD JUDGMENT DELIVERED BY JOHN DOE, JSC",
    )
    assert (dissent.label, dissent.author) == ("dissenting", "JANE ROE, JSC")
    assert [nodes[f"para_{n}"].label for n in (1, 2, 3, 4)] == ["[1]", "[2]", "[3]", "[4]"]


def test_unnumbered_words_quotations_and_numbered_items_are_leaves_of_their_paragraph():
    text, result = build(JUDGMENT)
    nodes = by_locator(result)
    assert span(text, nodes["para_1"]).endswith("The appellant was convicted at trial.")
    # An indented quotation stays in the paragraph that quotes it, even with a number of its own.
    assert span(text, nodes["para_2"]) == (
        "[2] The Court of Appeal said:\n\n“[7] The evidence against the appellant was overwhelming.”"
        "\n\nThe words could not be plainer."
    )
    # A cross-heading belongs to the opinion and ends the paragraph before it.
    assert "Issue one" not in span(text, nodes["para_2"]) and "Issue one" in span(text, nodes["op_1"])
    # Where paragraph numbers are printed in brackets, "1." is an item of the open paragraph.
    assert span(text, nodes["para_3"]).endswith("1. Whether the confession was voluntary.")
    assert nodes["para_3__table_1"].heading == "Exhibits"
    assert span(text, nodes["para_4"]).startswith("[4] For the reasons above")  # numbered: a paragraph


def test_the_orders_are_the_lead_opinions_or_the_courts_never_a_dissents():
    text, result = build(JUDGMENT)
    nodes = by_locator(result)
    assert nodes["order_1"].parent is nodes["op_1"]
    assert span(text, nodes["order_1"]) == (
        "For these reasons, the appeal is dismissed.\n\n1. The conviction is affirmed."
    )
    assert span(text, nodes["op_2__para_2"]).endswith("For these reasons, I would allow the appeal.")
    assert nodes["order_2"].parent is result.root and nodes["order_2"].heading == "ORDER"
    assert span(text, nodes["order_2"]) == "ORDER\n\nThe appeal is dismissed."


def test_every_leaf_is_held_once_spans_nest_and_siblings_never_overlap():
    text, result = build(JUDGMENT)
    raw = text.text.encode()
    held = sorted(i for node in result.nodes for i in node.leaves)
    assert held == list(range(len(text.leaves)))
    assert len({n.locator for n in result.nodes}) == len(result.nodes)
    for node in result.nodes:
        raw[node.start : node.end].decode()
        if node.parent:
            assert node.parent.start <= node.start <= node.end <= node.parent.end
        assert all(a.end <= b.start for a, b in zip(node.children, node.children[1:], strict=False))
    assert codes(result) == []
    opinions = [n for n in result.canonical() if n["kind"] == "opinion"]
    assert [n["author"] for n in opinions] == ["JOHN DOE, JSC", "JANE ROE, JSC"]
    assert all("author" not in n for n in result.canonical() if n["kind"] != "opinion")


def test_dotted_numbers_open_paragraphs_in_order_and_items_within_them_are_reported():
    text, result = build(
        [
            [
                head("JUDGMENT"),
                para("(Delivered by John Inyang Okoro, JSC)"),
                para("1. The appellant appeals against his conviction."),
                para("The issues are:"),
                para("1. Whether the trial was fair.", level=1),
                para("2. The trial was fair."),
                para("3. The sentence was lawful."),
                para("2. Costs follow the event."),
                para("4. The appeal fails.", level=0),
                head("CONCURRING OPINION"),
                para("1. I agree."),
            ]
        ]
    )
    nodes = by_locator(result)
    assert (nodes["op_1"].label, nodes["op_1"].author) == ("lead", "John Inyang Okoro, JSC")
    assert [n.locator for n in nodes["op_1"].children] == ["para_1", "para_2", "para_3", "para_4"]
    assert "1. Whether the trial was fair." in span(text, nodes["para_1"])
    assert span(text, nodes["para_3"]).endswith("2. Costs follow the event.")
    assert (nodes["op_2"].label, nodes["op_2"].author) == ("concurring", "")
    assert [n.locator for n in nodes["op_2"].children] == ["op_2__para_1"]
    assert codes(result) == ["paragraph_order"]


def test_paragraphs_without_an_opinion_heading_begin_one_and_a_bare_text_is_all_header():
    text, result = build(
        [
            [
                para("IN THE HIGH COURT OF LAGOS STATE"),
                para("ALHAJI MUSA BELLO", kind="list_item", marker="1."),
                para("[1] The claimant seeks a declaration."),
                para("Judgment is entered for the claimant.", marker="2"),  # a number set beside the words
            ]
        ]
    )
    nodes = by_locator(result)
    assert (nodes["op_1"].label, nodes["op_1"].heading) == ("", "")
    assert [n.locator for n in nodes["op_1"].children] == ["para_1", "para_2"]
    assert span(text, nodes["header"]).endswith("1. ALHAJI MUSA BELLO")
    _, bare = build([[para("IN THE HIGH COURT"), para("1. The claim fails.")]])
    assert [n.kind for n in bare.nodes] == ["document", "notice"] and codes(bare) == ["no_opinion"]


def test_an_order_printed_before_a_ruling_is_the_courts():
    text, result = build(
        [
            [
                head("ORDER"),
                para("1. The application is dismissed."),
                head("RULING"),
                para("[1] The applicant sought a stay."),
            ]
        ]
    )
    nodes = by_locator(result)
    assert nodes["order_1"].parent is result.root and nodes["op_1"].label == "ruling"
    assert span(text, nodes["order_1"]).endswith("1. The application is dismissed.")
    assert span(text, nodes["para_1"]) == "[1] The applicant sought a stay."


def test_judgments_and_rulings_follow_the_judgment_rules_in_any_jurisdiction():
    assert structure.rules_for({"id": "ng-sc-2019-4", "type": "judgment"}) == structure.JUDGMENT
    assert structure.rules_for({"id": "rw-sc-2020-1", "document_type": "ruling"}) == structure.JUDGMENT
    assert structure.rules_for({"id": "rw-law-2024-1", "type": "act"}) == structure.RW_LAW
    assert structure.rules_for({"id": "ng-act-2023", "type": "order"}) == structure.NG_FEDERAL_ACT
    pages = [{"page": n, "units": units} for n, units in enumerate(JUDGMENT, start=1)]
    built = publication.build({"id": "ng-sc-2019-4", "jurisdiction": "ng", "type": "judgment"}, pages)
    summary = publication.summary(built)
    assert summary["rules"] == "judgment-paragraphs-1" and summary["counts"]["opinion"] == 2
    assert summary["counts"]["order"] == 2 and summary["errors"] == []


# --- the resolver ------------------------------------------------------------------------------------

WORK = "ng-sc-2019-4"
CITATION = "[2019] NGSC 4"


def publish_judgment(store, pages):
    store.save(
        Corpus(
            jurisdictions=[Jurisdiction(id="ng", name="Nigeria", country_code="NG", languages=["en"])],
            authorities=[
                Authority(
                    id="ng-sc", jurisdiction_id="ng", name="Example Supreme Court", kind="court",
                    official_url="https://example.org/court/",
                )
            ],
            languages=[Language(id="en", name="English", tag="en")],
            sources=[
                Source(
                    id=WORK, jurisdiction_id="ng", authority_id="ng-sc", title="Bello v. The State",
                    document_type="judgment", canonical_url="https://example.org/court/sc-123-2010.pdf",
                    citation=CITATION, language="en",
                )
            ],
            rights=[
                RightsRecord(
                    id=f"rights-{WORK}", source_id=WORK, metadata_public=True,
                    metadata_basis="Independently authored catalogue entry",
                )
            ],
        )
    )  # fmt: skip
    now = datetime.now(UTC).replace(microsecond=0)
    decision = {
        "document_types": ["judgment"],
        "rights_holder": "Synthetic test rights holder",
        "licence": "Synthetic test decision",
        "licence_url": None,
        "attribution_required": False,
        "attribution_text": None,
        "database_rights_notes": "None claimed.",
        "privacy_status": "cleared",
        "basis": "Synthetic test judgment.",
        "evidence": ["https://example.org/decision"],
        "decided_by": "Test Reviewer",
        "decided_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=365)).isoformat(),
    }
    pages = [{"page": n, "units": units} for n, units in enumerate(pages, start=1)]
    text = publication.build({"id": WORK, "jurisdiction": "ng", "type": "judgment"}, pages)
    item = publish.ApprovedText(
        document_id=WORK, work=WORK, language="en", document_type="judgment", text=text,
        approver="Test Reviewer", approved_at=now - timedelta(minutes=5),
        provenance={
            "url": "https://example.org/court/sc-123-2010.pdf",
            "retrieved_at": "2026-10-09T06:41:17+00:00",
            "sha256": "7" * 64,
        },
    )  # fmt: skip
    report = publish.publish_texts(store, "ng", publish.Texts(approved=[item]), decision)
    assert report["published"], report
    corpus = store.load()
    return {n.locator: n for n in corpus.structure if n.source_id == WORK}


def test_a_judgments_paragraph_resolves_from_its_neutral_citation(store):
    nodes = publish_judgment(store, JUDGMENT)

    def resolve(citation):
        with store.engine.begin() as conn:
            return LegacyCatalogue(LegacyRepository(conn)).citations(citation).provision

    for cited in (
        f"{CITATION} [4]",
        f"{CITATION} at [4]",
        f"{CITATION}, para. 4",
        f"Bello v. State {CITATION} [4]",
    ):
        found = resolve(cited)
        assert found.status == "resolved" and found.candidates[0].anchor_id == nodes["para_4"].id, cited
    found = resolve(f"{CITATION} op. 2 [2]")
    assert found.status == "resolved" and found.candidates[0].locator == "op_2__para_2"
    assert found.candidates[0].citation == f"{CITATION}, op. 2 [2]"
    # "[2]" is the lead opinion's: the dissent numbers its own, and its [2] is "op. 2 [2]".
    assert resolve(f"{CITATION} [2]").candidates[0].locator == "para_2"
    assert resolve(f"{CITATION}, [4]").candidates[0].anchor_id == nodes["para_4"].id  # as its anchor cites it
    assert resolve(f"{CITATION} [9]").status == "unresolved"


def test_a_paragraph_of_a_later_opinion_numbered_through_the_judgment_resolves_by_its_number(store):
    pages = [
        [
            head("JUDGMENT"),
            para("[1] The appeal fails."),
            head("DISSENTING OPINION"),
            para("[2] I would allow it."),
        ]
    ]
    nodes = publish_judgment(store, pages)
    with store.engine.begin() as conn:
        found = LegacyCatalogue(LegacyRepository(conn)).citations(f"{CITATION} [2]").provision
    assert found.status == "resolved" and found.candidates[0].anchor_id == nodes["op_2__para_2"].id


def test_each_paragraph_of_a_judgment_is_a_search_unit():
    text, result = build(JUDGMENT)
    raw, ids = text.text.encode(), {id(n): f"n{i}" for i, n in enumerate(result.nodes)}
    content = hashlib.sha256(raw).hexdigest()
    version = VersionViewRecord(
        id="ver_1", source_id="w", canonical_url="https://example.org/j.pdf", retrieved_at=datetime.now(UTC),
        raw_hash="0" * 64, content_hash=content, canonical_text=text.text, parser="test",
    )  # fmt: skip
    nodes = [
        NodeViewRecord(
            id=ids[id(n)], source_id="w", version_id="ver_1", parent_id=ids[id(n.parent)] if n.parent else None,
            kind=n.kind, locator=n.locator, order=n.order, start_byte=n.start, end_byte=n.end,
            source_hash=content, label=n.label or None, heading=n.heading or None,
        )
        for n in result.nodes
    ]  # fmt: skip
    units = text_index.units(version, nodes, "judgment")
    by_node = {u.node.locator: raw[u.start : u.end].decode() for u in units}
    assert by_node["para_2"].endswith("The words could not be plainer.")
    assert [u.pinpoint for u in units if u.node.kind == "paragraph"] == [
        "[1]",
        "[2]",
        "[3]",
        "[4]",
        "op. 2 [1]",
        "op. 2 [2]",
    ]
    assert by_node["order_1"].startswith("For these reasons") and by_node["order_2"].startswith("ORDER")
    assert all(a.end <= b.start for a, b in zip(units, units[1:], strict=False))
