"""Pinpoints (API-04 provision lookup) and the section index's units (RET-01, MIN-02, D15), without a
database: how a part is cited, how a citation is read back, and how a text is cut into search units."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from api import pinpoint, text_index
from schema.public import NodeViewRecord, VersionViewRecord


def part(kind, label=None, locator="x"):
    return SimpleNamespace(kind=kind, label=label, locator=locator)


@pytest.mark.parametrize(
    "cited, key",
    [
        ("s. 23(1)(a)", ("s:23", "1", "a")),
        ("S.23 (1) (a)", ("s:23", "1", "a")),
        ("section 23A(1)", ("s:23a", "1")),
        ("§ 23", ("s:23",)),
        ("s. 2, “Fund”", ("s:2", "def:fund")),
        ("s 2 definition of 'Data Subject'", ("s:2", "def:data subject")),
        ("reg. 5(2)", ("s:5", "2")),
        ("art. 12", ("art:12",)),
        ("Article XIV", ("art:14",)),
        ("Ingingo ya mbere", ("art:1",)),
        ("Article 1er", ("art:1",)),
        ("First Schedule, para. 3(a)", ("sch:1", "para:3", "a")),
        ("Schedule 2, Part II, paragraph 4", ("sch:2", "part:2", "para:4")),
        ("Sch. 1 para 3", ("sch:1", "para:3")),
        ("2nd Schedule", ("sch:2",)),
        ("Schedule", ("sch:1",)),
        ("para. 3", None),
        ("chapter 4", None),
    ],
)
def test_a_cited_pinpoint_is_read_into_a_key(cited, key):
    assert pinpoint.read(cited) == key


def test_a_citation_is_split_into_a_work_and_a_pinpoint_in_either_order():
    assert pinpoint.split("NDPA s. 23(1)(a)") == [("NDPA", "s. 23(1)(a)", ("s:23", "1", "a"))]
    assert (
        pinpoint.split("Nigeria Data Protection Act 2023, s. 23(1)(a)")[0][0]
        == "Nigeria Data Protection Act 2023"
    )
    assert pinpoint.split("section 23 of the NDPA") == [("NDPA", "section 23", ("s:23",))]
    assert pinpoint.split("SEC Act 2007 s. 3") == [("SEC Act 2007", "s. 3", ("s:3",))]
    assert pinpoint.split("ISA 2025, First Schedule, para. 3")[0][1] == "First Schedule, para. 3"
    assert pinpoint.split("Act No. 2 of 2025") == [] and pinpoint.split("Securities Act 2007") == []


def test_a_part_is_cited_from_its_printed_numbers_and_read_back_to_the_same_key():
    section, subsection, paragraph = (
        part("section", "23.", "sec_23"),
        part("subsection", "(1)"),
        part("paragraph", "(a)"),
    )
    key, shown = pinpoint.cite([part("document"), part("part", "PART III"), section, subsection, paragraph])
    assert shown == "s. 23(1)(a)" and pinpoint.read(shown) == key
    assert pinpoint.cite([section], "regulation")[1] == "reg. 23"
    assert pinpoint.cite([section, part("definition", "Data Subject")])[1] == "s. 23, “Data Subject”"
    # A Rwandan article is cited by its number, whatever its printed heading: never "art. Article 12".
    for label in ("Article 12", "Ingingo ya 12", "Art. 12."):
        assert pinpoint.cite([part("article", label, "chap_2__art_12")])[1] == "art. 12"
    assert pinpoint.cite([part("article", "Ingingo ya mbere", "art_1")])[0] == ("art:1",)
    assert pinpoint.cite([part("article", "Article 12", "art_12_2")])[0] == ("art:12",)  # a repeated number
    schedule = part("schedule", "SECOND SCHEDULE", "sch_2")
    key, shown = pinpoint.cite(
        [schedule, part("part", "PART II"), part("paragraph", "4."), part("subparagraph", "(b)")]
    )
    assert shown == "Second Schedule, Part II, para. 4(b)" and pinpoint.read(shown) == key
    assert pinpoint.cite([part("schedule", "SCHEDULE II", "sch_ii")])[0] == ("sch:2",)
    assert pinpoint.cite([part("document")]) is None and pinpoint.cite([part("part", "PART I")]) is None
    assert pinpoint.cite([part("paragraph", "(a)")]) is None  # outside any section


def node(id_, kind, start, end, raw, parent=None, label=None, heading=None, locator=None):
    return NodeViewRecord(
        id=id_, source_id="w", version_id="ver_1", parent_id=parent, kind=kind, locator=locator or id_, order=0,
        start_byte=start, end_byte=end, source_hash=hashlib.sha256(raw).hexdigest(), label=label, heading=heading,
    )  # fmt: skip


def version(text):
    return VersionViewRecord(
        id="ver_1", source_id="w", canonical_url="https://example.org/a.pdf", retrieved_at=datetime.now(UTC),
        raw_hash="0" * 64, content_hash=hashlib.sha256(text.encode()).hexdigest(), canonical_text=text,
        parser="test",
    )  # fmt: skip


def spans(text, *pieces):
    raw, found, at = text.encode(), [], 0
    for piece in pieces:
        start = raw.index(piece.encode(), at)
        found.append((start, start + len(piece.encode())))
        at = start
    return found


def test_a_text_is_cut_into_provisions_that_never_overlap_and_skip_the_contents():
    text = (
        "EXAMPLE ACT\n\nARRANGEMENT OF SECTIONS\n\n1. Loans\n\nPART I\n\n"
        "1. (1) The Fund shall lend.\n\n(2) It is repaid.\n\n2. In this Act “Fund” means the Fund.\n\n"
        "FIRST SCHEDULE\n\n1. Lagos office\n\n2. Abuja office\n"
    )
    raw = text.encode()
    (doc,) = spans(text, text.rstrip("\n"))
    contents, part_i, s1, s1_1, s1_2, s2, sch, p1, p2 = spans(
        text, "ARRANGEMENT OF SECTIONS\n\n1. Loans", "PART I\n\n1. (1) The Fund shall lend.\n\n(2) It is repaid.\n\n"
        "2. In this Act “Fund” means the Fund.", "1. (1) The Fund shall lend.\n\n(2) It is repaid.", "(1) The Fund",
        "(2) It is repaid.", "2. In this Act", "FIRST SCHEDULE\n\n1. Lagos office\n\n2. Abuja office", "1. Lagos office",
        "2. Abuja office",
    )  # fmt: skip
    nodes = [
        node("doc", "document", *doc, raw),
        node("contents", "contents", *contents, raw, parent="doc"),
        node("part", "part", *part_i, raw, parent="doc", label="PART I"),
        node("s1", "section", s1[0], s1[1], raw, parent="part", label="1.", heading="Loans", locator="sec_1"),
        node("s1_1", "subsection", *s1_1, raw, parent="s1", label="(1)"),
        node("s1_2", "subsection", *s1_2, raw, parent="s1", label="(2)"),
        node("s2", "section", s2[0], part_i[1], raw, parent="part", label="2.", locator="sec_2"),
        node("sch", "schedule", *sch, raw, parent="doc", label="FIRST SCHEDULE", locator="sch_1"),
        node("p1", "paragraph", *p1, raw, parent="sch", label="1.", locator="sch_1__para_1"),
        node("p2", "paragraph", *p2, raw, parent="sch", label="2.", locator="sch_1__para_2"),
    ]
    units = text_index.units(version(text), nodes, "act")
    words = [raw[u.start : u.end].decode() for u in units]
    assert words == [
        "EXAMPLE ACT",  # the document's own words: not in any provision
        "PART I",  # the part's heading
        "1. (1) The Fund shall lend.\n\n(2) It is repaid.",
        "2. In this Act “Fund” means the Fund.",
        "FIRST SCHEDULE",
        "1. Lagos office",
        "2. Abuja office",
    ]
    assert [u.pinpoint for u in units] == [None, None, "s. 1", "s. 2", "First Schedule", "First Schedule, para. 1",
                                           "First Schedule, para. 2"]  # fmt: skip
    assert units[2].headings == "PART I 1. Loans" and units[2].node.id == "s1"
    assert all(
        a.end <= b.start for a, b in zip(units, units[1:], strict=False)
    )  # never overlapping, in order


def test_an_oversized_provision_is_searched_by_its_parts_and_a_long_run_is_cut_at_whitespace(monkeypatch):
    monkeypatch.setattr(text_index, "MAX_UNIT_BYTES", 40)
    text = (
        "1. (1) Alpha beta gamma delta.\n\n(2) Epsilon zeta eta theta.\n\nThé end of a lông section ändern.\n"
    )
    raw = text.encode()
    s1, s1_1, s1_2 = spans(
        text, text.rstrip("\n"), "(1) Alpha beta gamma delta.", "(2) Epsilon zeta eta theta."
    )
    nodes = [
        node("s1", "section", *s1, raw, label="1.", locator="sec_1"),
        node("s1_1", "subsection", *s1_1, raw, parent="s1", label="(1)"),
        node("s1_2", "subsection", *s1_2, raw, parent="s1", label="(2)"),
    ]
    units = text_index.units(version(text), nodes, "act")
    words = [raw[u.start : u.end].decode() for u in units]
    assert (
        words[0] == "1."
        and words[1] == "(1) Alpha beta gamma delta."
        and words[2] == "(2) Epsilon zeta eta theta."
    )
    assert "".join(words[3:]).replace(" ", "") == "Théendofalôngsectionändern."
    assert all(u.end - u.start <= 40 for u in units)
    assert [u.pinpoint for u in units[:3]] == ["s. 1", "s. 1(1)", "s. 1(2)"]


def test_a_snippet_is_the_exact_bytes_of_the_text_with_its_terms_marked():
    words = "25.—(1) Processing is lawful where  consent,\nand —shall ensure café consent is freely given."
    base = 1000
    head = f"shall ensure café {text_index.START}consent{text_index.STOP} is {text_index.START}freely{text_index.STOP} given"
    snippet, start, end, marks = text_index.place(words, head, base)
    raw = words.encode()
    assert snippet == "shall ensure café consent is freely given"
    assert raw[start - base : end - base].decode() == snippet
    assert [raw[a - base : b - base].decode() for a, b in marks] == ["consent", "freely"]
    opening, start, end, marks = text_index.place(words, "not in the words", base)
    assert words.startswith(opening) and start == base and marks == ()
