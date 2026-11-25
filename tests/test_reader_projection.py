"""The website's reader projection (api/reader.py; PRD-03 and PRD-04): which text opens, and a text's
nodes as the record page reads them. tests/test_published_text.mjs lays out nodes of the same shape, made
by a copy of these rules in JavaScript; the cases marked "as the page's tests" hold the two together."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from api.reader import listed, reader_nodes, reader_text
from schema.models import NodeKind
from schema.public import TextDetails

DAY = datetime(2026, 11, 25, tzinfo=UTC)


def text(id, language, effective_from=None, prior=None, read=DAY):
    return TextDetails(
        id=id,
        source_id="rec",
        content_hash="a" * 64,
        verification_status="verified",
        content_verifier="reviewer",
        content_verified_at=DAY,
        language=language,
        effective_from=effective_from,
        retrieved_at=read,
        prior_version_id=prior,
    )


def chosen(texts, *args, **more):
    found, matched, missed = reader_text(texts, *args, **more)
    return (found.id if found else None, matched, missed)


def test_the_text_opened_is_the_one_asked_for_then_the_records_language_then_the_first_by_code():
    served = [text("ver-rw", "rw"), text("ver-fr", "fr"), text("ver-en", "en")]
    assert chosen(served, "rw") == ("ver-rw", "default", [])
    assert chosen(served, "sw") == ("ver-en", "default", [])  # the first by code, not the first kept
    assert chosen(served) == ("ver-en", "default", [])
    assert chosen(served, "rw", language="fr") == ("ver-fr", "language", [])
    assert chosen(served, "rw", language="FR") == ("ver-fr", "language", [])
    assert chosen(served, "RW") == ("ver-rw", "default", [])
    # A version is named exactly, and when it is found the language sent with it is not looked at.
    assert chosen(served, "rw", "ver-en", "fr") == ("ver-en", "version", [])
    assert chosen(served, "rw", "ver-en", "pt") == ("ver-en", "version", [])
    assert chosen(served, "rw", "VER-EN") == ("ver-rw", "default", ["version"])
    # Every way of asking for what is not there says what was missed.
    assert chosen(served, "rw", language="pt") == ("ver-rw", "default", ["language"])
    assert chosen(served, "rw", "ver-gone") == ("ver-rw", "default", ["version"])
    assert chosen(served, "rw", "ver-gone", "pt") == ("ver-rw", "default", ["version", "language"])
    assert chosen(served, "rw", "ver-gone", "fr") == ("ver-fr", "language", ["version"])
    # No public text: nothing is chosen and nothing is missed.
    assert reader_text([], "rw", "ver-gone", "fr") == (None, None, None)


def test_within_a_language_the_latest_text_that_none_of_its_language_replaces():
    several = [
        text("ver-2016", "en", date(2016, 1, 1)),
        text("ver-2020-misread", "en", date(2020, 6, 1)),
        text("ver-2020", "en", date(2020, 6, 1), prior="ver-2020-misread"),
        text("ver-undated", "en"),
        text("ver-fr", "fr", prior="ver-2020"),  # a French text replaces no English one
    ]
    assert chosen(several, "en") == ("ver-2020", "default", [])
    assert chosen(several, "en", "ver-2016") == ("ver-2016", "version", [])
    # The same date: the one read later; the same reading time: the higher ID. Never the order kept in.
    later = datetime(2026, 11, 26, tzinfo=UTC)
    ties = [text("ver-b", "en", date(2020, 1, 1)), text("ver-a", "en", date(2020, 1, 1), read=later)]
    assert chosen(ties, "en")[0] == chosen(ties[::-1], "en")[0] == "ver-a"
    same = [text("ver-b", "en", date(2020, 1, 1)), text("ver-a", "en", date(2020, 1, 1))]
    assert chosen(same, "en")[0] == chosen(same[::-1], "en")[0] == "ver-b"
    # Texts that name each other as replaced leave none: then all of them count.
    ring = [
        text("ver-x", "en", date(2019, 1, 1), prior="ver-y"),
        text("ver-y", "en", date(2021, 1, 1), prior="ver-x"),
    ]
    assert chosen(ring, "en")[0] == "ver-y"


def test_the_records_texts_are_listed_by_language_then_latest_first_without_their_words():
    texts = [
        text("ver-rw", "rw"),
        text("ver-en-old", "en", date(2016, 1, 1)),
        text("ver-fr", "fr"),
        text("ver-en-new", "en", date(2020, 1, 1)),
    ]
    assert [t["id"] for t in listed(texts)] == ["ver-en-new", "ver-en-old", "ver-fr", "ver-rw"]
    assert listed(texts)[0] == {
        "id": "ver-en-new",
        "language": "en",
        "effective_from": "2020-01-01",
        "retrieved_at": "2026-11-25T00:00:00Z",
    }


def node(kind, locator, start, end, parent=None, order=0, **more):
    return SimpleNamespace(
        id="n_" + locator,
        kind=kind,
        locator=locator,
        start_byte=start,
        end_byte=end,
        parent_id="n_" + parent if parent else None,
        order=order,
        label=more.get("label"),
        heading=more.get("heading"),
        cell_states=more.get("cell_states"),
    )


def test_every_kind_of_node_has_its_place_and_an_unknown_kind_is_sent_whole():
    # One of each kind in the corpus, nested as texts nest them, and one the corpus does not have yet.
    nodes = [
        node("document", "document", 0, 1000),
        node("preamble", "preamble", 0, 10, "document"),
        node("contents", "contents", 10, 40, "document", 1, heading="CONTENTS"),
        node("contents_entry", "contents__1", 20, 30, "contents"),
        node("notice", "notice_1", 40, 50, "document", 2),
        node("title", "title_i", 50, 600, "document", 3, label="TITLE I"),
        node("part", "part_i", 50, 600, "title_i", label="PART I"),
        node("chapter", "chap_i", 50, 600, "part_i", label="CHAPTER I"),
        node("division", "chap_i__dvs_1", 50, 600, "chap_i", label="Section One"),
        node("article", "art_1", 60, 100, "chap_i__dvs_1", label="Article 1", heading="Purpose"),
        node("section", "sec_2", 100, 400, "chap_i__dvs_1", 1, label="2."),
        node("subsection", "sec_2__subsec_1", 110, 300, "sec_2", label="(1)"),
        node("definition", "sec_2__def_x", 120, 250, "sec_2__subsec_1"),
        node("paragraph", "sec_2__def_x__para_a", 130, 240, "sec_2__def_x", label="(a)"),
        node("subparagraph", "sec_2__def_x__para_a__i", 140, 200, "sec_2__def_x__para_a", label="(i)"),
        node("subparagraph", "deep", 150, 160, "sec_2__def_x__para_a__i"),
        node("table", "sec_2__table_1", 260, 290, "sec_2__subsec_1", 1, cell_states={"0,1": "illegible"}),
        node("schedule", "sch_1", 600, 700, "document", 4, label="SCHEDULE"),
        node("annex", "annex_i", 700, 800, "document", 5, label="ANNEX I"),
        node("opinion", "opinion_1", 800, 900, "document", 6, label="JUDGMENT"),
        node("paragraph", "opinion_1__para_1", 810, 890, "opinion_1", label="[1]"),
        node("quotation", "opinion_1__para_1__quot_1", 820, 850, "opinion_1__para_1"),
        node("subparagraph", "opinion_1__para_1__a", 860, 880, "opinion_1__para_1", 1, label="(a)"),
        node("order", "order_1", 900, 950, "document", 7, heading="ORDERS"),
        node("recital", "recital_1", 950, 1000, "document", 8, label="WHEREAS"),
        node("notice", "empty", 1000, 1000, "document", 9),
    ]
    assert {n.kind for n in nodes} - {"recital"} == set(NodeKind.__args__)
    sent = reader_nodes(nodes)
    by_locator = {n.get("locator"): n for n in sent if "locator" in n}
    # Full nodes: an ID, a kind, a locator, a span, and only the other fields that have a value.
    assert by_locator["art_1"] == {
        "id": "n_art_1",
        "kind": "article",
        "locator": "art_1",
        "start_byte": 60,
        "end_byte": 100,
        "label": "Article 1",
        "heading": "Purpose",
    }
    assert by_locator["sec_2__table_1"]["cell_states"] == {"0,1": "illegible"}
    assert by_locator["recital_1"]["kind"] == "recital"  # a kind added later: sent whole
    assert not any("parent_id" in n or "order" in n for n in sent)
    # Not sent: a printed contents' line, a definition, a quotation, a judgment's own paragraph, a node
    # with no bytes.
    assert {
        "contents__1",
        "sec_2__def_x",
        "opinion_1__para_1__quot_1",
        "opinion_1__para_1",
        "empty",
    }.isdisjoint(by_locator)
    # Indentation nodes: their kind, their span and how far in, counted through the definition that is
    # not sent, never more than three, and not counting a judgment's own paragraph.
    assert [n for n in sent if "depth" in n] == [
        {"kind": "subsection", "start_byte": 110, "end_byte": 300, "depth": 1},
        {"kind": "paragraph", "start_byte": 130, "end_byte": 240, "depth": 2},
        {"kind": "subparagraph", "start_byte": 140, "end_byte": 200, "depth": 3},
        {"kind": "subparagraph", "start_byte": 150, "end_byte": 160, "depth": 3},
        {"kind": "subparagraph", "start_byte": 860, "end_byte": 880, "depth": 1},
    ]
    # Document order: where it starts, the longer span first, a node before those inside it.
    assert [n["kind"] for n in sent][:8] == [
        "document",
        "preamble",
        "contents",
        "notice",
        "title",
        "part",
        "chapter",
        "division",
    ]


def test_a_part_and_its_only_section_with_one_span_go_the_part_first_as_the_pages_tests():
    # The corpus's order counts among siblings: the section is the first of its part, the part the
    # second of its document. By span alone the section would be sent before the part that holds it.
    nodes = [
        node("document", "document", 0, 90),
        node("part", "part_ii", 8, 90, "document", 1, label="PART II"),
        node("section", "sec_4", 8, 90, "part_ii", 0, label="4."),
        node("paragraph", "sec_4__para_a", 60, 90, "sec_4", label="(a)"),
    ]
    assert [n["kind"] for n in reader_nodes(nodes)] == ["document", "part", "section", "paragraph"]
    assert [n["kind"] for n in reader_nodes(nodes[::-1])] == ["document", "part", "section", "paragraph"]


def test_a_judgments_own_paragraphs_are_not_sent_and_what_is_inside_one_is_one_step_in_as_the_pages_tests():
    nodes = [
        node("document", "document", 0, 200),
        node("opinion", "opinion_1", 0, 150, "document", label="JUDGMENT"),
        node("paragraph", "opinion_1__para_1", 10, 80, "opinion_1", label="[1]"),
        node("subparagraph", "opinion_1__para_1__a", 50, 80, "opinion_1__para_1", label="(a)"),
        node("paragraph", "opinion_1__para_2", 80, 150, "opinion_1", 1, label="[2]"),
        node("order", "order_1", 150, 200, "document", 1, heading="ORDERS"),
    ]
    assert [(n["kind"], n.get("depth")) for n in reader_nodes(nodes)] == [
        ("document", None),
        ("opinion", None),
        ("subparagraph", 1),
        ("order", None),
    ]


def test_a_printed_contents_line_outside_any_contents_is_sent_so_it_is_still_set_apart():
    nodes = [node("document", "document", 0, 100), node("contents_entry", "stray", 10, 20, "document")]
    assert [n["kind"] for n in reader_nodes(nodes)] == ["document", "contents_entry"]


def test_a_text_whose_spans_and_parents_disagree_has_no_nodes():
    document = node("document", "document", 0, 300)
    # Two siblings whose spans cross.
    crossing = [
        document,
        node("contents", "contents", 0, 200, "document"),
        node("section", "sec_1", 100, 300, "document", 1),
    ]
    assert reader_nodes(crossing) is None
    # A node lying inside one that is not its ancestor.
    inside = [
        document,
        node("part", "part_i", 0, 200, "document"),
        node("section", "sec_1", 50, 100, "document", 1),
    ]
    assert reader_nodes(inside) is None
    # A node lying outside its parent cannot be made here: the corpus refuses it. Agreement through a
    # node that is not sent is agreement: the paragraph's nearest sent ancestor is the section.
    through = [
        document,
        node("section", "sec_1", 0, 300, "document"),
        node("definition", "sec_1__def", 10, 200, "sec_1"),
        node("paragraph", "sec_1__def__a", 20, 100, "sec_1__def"),
    ]
    assert [n["kind"] for n in reader_nodes(through)] == ["document", "section", "paragraph"]
    assert reader_nodes([]) == []
