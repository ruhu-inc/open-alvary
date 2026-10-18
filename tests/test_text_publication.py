"""Reviewed pages to canonical text and legal structure (TXP-02, TXP-03)."""

import hashlib

from workbench import serialize, structure


def para(text, kind="paragraph", **extra):
    return {"kind": kind, "text": text, **extra}


def head(text, **extra):
    return para(text, "heading", **extra)


def build(pages, rules=structure.NG_FEDERAL_ACT, language=None):
    pages = [{"page": n, "units": units} for n, units in enumerate(pages, start=1)]
    text = serialize.serialize(pages)
    return text, structure.derive(text, pages, rules, language)


def by_locator(result):
    return {node.locator: node for node in result.nodes}


def span(text, node):
    return text.text.encode()[node.start : node.end].decode()


def codes(*results):
    return [issue.code for result in results for issue in result.issues]


# --- serialization -----------------------------------------------------------------------------------


def test_leaves_numbers_notes_and_separators():
    text, _ = build(
        [
            [
                head("  EXAMPLE ACT, 2023 "),
                para("Notwithstanding  any law,\tthis applies.", marker="1.", notes=["Application"]),
                para("Café rules", marks=[{"type": "bold", "start": 0, "end": 4}], align="justify", level=1),
            ]
        ]
    )
    assert text.text == (
        "EXAMPLE ACT, 2023\n\nApplication\n\n1. Notwithstanding  any law,\tthis applies.\n\nCafé rules\n"
    )
    assert [leaf.role for leaf in text.leaves] == ["text", "note", "text", "text"]
    assert text.content_hash == hashlib.sha256(text.text.encode()).hexdigest()
    assert text.profile_id == "oa-text-1/workbench-units-1"


def test_formatting_alignment_and_level_are_not_in_the_bytes():
    plain, _ = build([[para("1. Words.")]])
    styled, _ = build([[para("1. Words.", marks=[{"type": "italic", "start": 3, "end": 8}], align="center")]])
    leveled, _ = build([[para("1. Words.", level=0)]])
    assert plain.content_hash == styled.content_hash == leveled.content_hash


def test_empty_units_are_left_out_and_reported():
    text, _ = build([[para("1. One."), head(""), para("   "), para("2. Two.")]])
    assert text.text == "1. One.\n\n2. Two.\n"
    assert codes(text) == ["empty_unit", "empty_unit"]
    assert not text.errors


def test_an_empty_document_has_no_bytes():
    text, _ = build([[head("")]])
    assert text.text == ""


def test_table_block_and_merged_cells():
    table = {
        "kind": "table",
        "caption": "Rates",
        "header_rows": 1,
        "rows": [["Band", "Rate", "stray"], ["A", "5%", ""]],
        "spans": [[0, 1, 1, 2]],
    }
    text, _ = build([[table]])
    assert text.text == "Rates\n\nBand\tRate\t\nA\t5%\t\n"
    assert codes(text) == ["covered_cell_text"]


def test_reviewed_join_across_a_page_break():
    text, result = build(
        [
            [para("5. The Committee shall establish rules for", marker=None)],
            [para("applicants and banks.", continues_previous=True)],
        ]
    )
    assert text.text == "5. The Committee shall establish rules for applicants and banks.\n"
    leaf = text.leaves[0]
    assert (leaf.page, leaf.last_page, leaf.joined) == (1, 2, [(2, 0)])
    assert by_locator(result)["sec_5"].page_end == 2


def test_dehyphenated_join_and_join_errors():
    text, _ = build(
        [[para("1. The licen-")], [para("see shall pay.", continues_previous=True, dehyphenate=True)]]
    )
    assert text.text == "1. The licensee shall pay.\n"
    bad, _ = build(
        [
            [para("1. Ends here.")],
            [para("no hyphen", continues_previous=True, dehyphenate=True)],
            [para("(2) numbered", marker="(2)", continues_previous=True)],
        ]
    )
    assert [issue.code for issue in bad.errors] == ["join", "join"]
    assert "no hyphen" in bad.text  # a refused join stays a separate leaf


def test_a_join_on_the_same_page_is_refused():
    text, _ = build([[para("1. One"), para("two", continues_previous=True)]])
    assert [issue.code for issue in text.errors] == ["join"]


# --- Nigerian federal Acts -----------------------------------------------------------------------------


ACT = [
    [
        head("EXAMPLE ACT, 2023"),
        head("EXPLANATORY MEMORANDUM"),
        para("This Act sets up a fund."),
    ],
    [
        head("EXAMPLE ACT, 2023"),
        para("Arrangement of Sections", align="center"),
        para("Section:"),
        para("PART I — PRELIMINARY"),
        para("Objectives", kind="list_item", marker="1."),
        para("Establishment", kind="list_item", marker="2."),
        para("Interpretation", kind="list_item", marker="3."),
        para("Citation", kind="list_item", marker="4."),
    ],
    [
        para("A Bill", align="center"),
        para("For"),
        para("An Act to establish the Fund; and for related matters."),
        para("ENACTED by the National Assembly of the Federal Republic of Nigeria —"),
        para("PART I — PRELIMINARY"),
        para("1. The objectives of this Act are to —", notes=["Objectives", "Cap. C23, LFN 2004"]),
        para("(a) provide a framework;", kind="list_item"),
        para("(h) do the eighth thing;", kind="list_item"),
        para("(i) do the ninth thing; and", kind="list_item"),
        para("(j) do the tenth thing.", kind="list_item"),
        para("2. (1) There is established a Fund.", notes=["Establishment"]),
        para("(2) The Fund shall —"),
        para("(a) hold money for", kind="list_item"),
        para("(i) students, and", kind="list_item"),
        para("(ii) schools;", kind="list_item"),
        para("(b) at level three", kind="list_item", level=3),
        para("and pay it out.", level=1),
        para("Provided that nothing here applies to banks."),
    ],
    [
        para("3. In this Act —", notes=["Interpretation"]),
        para("“Fund” means the Fund established by section 2;"),
        para("“zones” for the time being means —"),
        para("(a) North Central; and", kind="list_item"),
        para("(b) South West;", kind="list_item"),
        para("4. This Act may be cited as the Example Act, 2023.", notes=["Citation"]),
        head("SCHEDULES"),
        para("First Schedule      Section 2(1)", align="center"),
        head("PART I"),
        head("SUCCESSOR COMPANIES"),
        para("Transmission Company", kind="list_item", marker="1."),
        head("PART II"),
        head("PLANTS"),
        para("Calabar Power Station", kind="list_item", marker="1."),
        head("Second Schedule      Section 3"),
        head("SUPPLEMENTARY PROVISIONS"),
        head("Proceedings"),
        para("1. (1) The Council shall meet."),
        para("(2) The quorum is five."),
        head("Committees"),
        para("(3) The Council may appoint committees."),
    ],
    [
        para("I, CERTIFY, IN ACCORDANCE WITH SECTION 2 (1) OF THE ACTS AUTHENTICATION ACT."),
        {
            "kind": "table",
            "caption": "",
            "header_rows": 1,
            "rows": [["SHORT TITLE", "SUMMARY"], ["Example", "A fund"]],
        },
        para("I certify that this Bill has been carefully compared.", notes=["I ASSENT."]),
    ],
]


def test_front_matter_contents_and_preamble():
    text, result = build(ACT)
    nodes = by_locator(result)
    assert result.root.heading == "EXAMPLE ACT, 2023"
    assert nodes["notice_1"].heading == "EXPLANATORY MEMORANDUM"
    entries = [n for n in result.nodes if n.kind == "contents_entry"]
    assert [(n.label, n.target) for n in entries] == [
        ("PART I", "part_I"),
        ("1.", "sec_1"),
        ("2.", "sec_2"),
        ("3.", "sec_3"),
        ("4.", "sec_4"),
    ]
    assert span(text, nodes["preamble"]).startswith("A Bill\n\nFor\n\nAn Act to establish")
    assert span(text, nodes["preamble"]).endswith("Federal Republic of Nigeria —")
    assert nodes["sec_1"].parent is nodes["part_I"]


def test_sections_margin_notes_and_numbering():
    text, result = build(ACT)
    nodes = by_locator(result)
    section = nodes["sec_1"]
    assert (section.label, section.heading) == ("1.", "Objectives")
    assert span(text, section).startswith("Objectives\n\nCap. C23, LFN 2004\n\n1. The objectives")
    assert [n.locator for n in section.children] == [
        "sec_1__para_a",
        "sec_1__para_h",
        "sec_1__para_i",
        "sec_1__para_j",
    ]
    # (i) after (h) is a letter; under (a) of subsection (2) it is a roman sub-paragraph.
    assert nodes["sec_2__subsec_2__para_a__subpara_i"].kind == "subparagraph"
    assert nodes["sec_2__subsec_2__para_a__subpara_ii"].kind == "subparagraph"
    # "2. (1) …": the section starts at its number, the subsection at "(1)".
    assert span(text, nodes["sec_2__subsec_1"]).startswith("(1) There is established")
    assert span(text, nodes["sec_2"]).startswith("Establishment\n\n2. (1) There")
    # The reviewer's level decides: "(b)" at level 3 is a sub-paragraph.
    assert nodes["sec_2__subsec_2__para_a__subpara_b"].kind == "subparagraph"
    # Unnumbered words at level 1 close the paragraphs and belong to subsection (2); a proviso
    # without a level stays in the innermost provision.
    assert span(text, nodes["sec_2__subsec_2"]).endswith("Provided that nothing here applies to banks.")
    assert "and pay it out." not in span(text, nodes["sec_2__subsec_2__para_a"])


def test_definitions():
    _, result = build(ACT)
    nodes = by_locator(result)
    assert nodes["sec_3__def_fund"].label == "Fund"
    assert [n.locator for n in nodes["sec_3__def_zones"].children] == [
        "sec_3__def_zones__para_a",
        "sec_3__def_zones__para_b",
    ]


def test_schedules_parts_and_headings_inside_provisions():
    text, result = build(ACT)
    nodes = by_locator(result)
    assert nodes["sch_1"].label == "First Schedule"
    assert nodes["sch_1__part_I"].heading == "SUCCESSOR COMPANIES"
    assert "sch_1__part_I__para_1" in nodes and "sch_1__part_II__para_1" in nodes  # no collision
    assert nodes["sch_2"].heading == "SUPPLEMENTARY PROVISIONS"
    assert nodes["sch_2__para_1__subpara_3"].parent is nodes["sch_2__para_1"]
    assert "Committees" in span(text, nodes["sch_2__para_1"])


def test_certificates_after_the_schedules_are_notices():
    _, result = build(ACT)
    nodes = by_locator(result)
    assert nodes["notice_2"].parent is result.root
    assert nodes["notice_2__table_1"].kind == "table"
    assert nodes["notice_3"].page_start == 5
    assert not any(n.kind == "schedule" and n.page_start == 5 for n in result.nodes)


def test_every_leaf_held_once_and_spans_nest():
    text, result = build(ACT)
    raw = text.text.encode()
    held = sorted(i for node in result.nodes for i in node.leaves)
    assert held == list(range(len(text.leaves)))
    for node in result.nodes:
        raw[node.start : node.end].decode()
        if node.parent:
            assert node.parent.start <= node.start <= node.end <= node.parent.end
    assert codes(text, result) == ["running_head"]


def test_a_part_heading_listed_in_the_contents_is_not_a_running_head():
    # The NDPA's PART headings repeat their contents lines; only a later copy at a page's top is one.
    pages = [
        [
            {"kind": "heading", "text": "EXAMPLE ACT, 2023"},
            {"kind": "heading", "text": "ARRANGEMENT OF SECTIONS"},
            {"kind": "heading", "text": "PART I — PRELIMINARY"},  # read as a heading, as in the NDPA
            {"kind": "list_item", "text": "1. Short title."},
            {"kind": "list_item", "text": "2. Application."},
        ],
        [
            {
                "kind": "paragraph",
                "text": "ENACTED by the National Assembly of the Federal Republic of Nigeria —",
            },
            {"kind": "heading", "text": "PART I — PRELIMINARY"},
            {"kind": "paragraph", "text": "1. This Act may be cited as the Example Act, 2023."},
        ],
        [
            {"kind": "heading", "text": "PART I — PRELIMINARY"},
            {"kind": "paragraph", "text": "2. It applies everywhere."},
        ],
    ]
    _, result = build(pages)
    running = [i for i in result.issues if i.code == "running_head"]
    assert [(i.page, i.message.split("”")[0]) for i in running] == [(3, "“PART I — PRELIMINARY")]
    assert "repeats the heading on page 2" in running[0].message


def test_structure_hash_follows_structure_not_formatting():
    _, base = build(ACT)
    restyled = [
        [{**unit, "align": "center"} if unit.get("kind") != "table" else unit for unit in page]
        for page in ACT
    ]
    _, styled = build(restyled)
    assert base.structure_hash == styled.structure_hash
    moved = [list(page) for page in ACT]
    moved[2][13] = para("(i) students, and", kind="list_item", level=2)
    _, releveled = build(moved)
    assert base.structure_hash != releveled.structure_hash


def test_warnings_for_the_reviewer():
    _, result = build(
        [
            [
                head("Arrangement of Sections"),
                para("Objectives", kind="list_item", marker="1."),
                para("Missing", kind="list_item", marker="9."),
                para("A Bill"),
                para("1. First.", notes=["Objectives"]),
                para("(a) one;", kind="list_item"),
                para("(a) again;", kind="list_item"),
                para("223. Misplaced number.", marker="(1)"),
                para("3. Third."),
                para("2. Out of order."),
            ]
        ]
    )
    assert codes(result) == [
        "repeated_locator",
        "two_numbers",
        "section_order",
        "contents_unmatched",
    ]


# --- Rwandan laws ------------------------------------------------------------------------------------


def test_rwandan_titles_chapters_divisions_and_articles():
    text, result = build(
        [
            [
                head("TABLE OF CONTENTS"),
                para("Article One: Purpose of this Law"),
                head("TITLE ONE: GENERAL PROVISIONS"),
                head("CHAPTER ONE: PURPOSE AND DEFINITIONS"),
                head("Article One: Purpose of this Law"),
                para("This Law determines the organisation of the Fund."),
                head("Article 2: Definitions"),
                para("In this Law, the following terms mean:"),
                head("TITLE II: THE FUND"),
                head("CHAPTER ONE: ESTABLISHMENT"),
                head("Section One: Organs"),
                head("Article 3: Organs of the Fund"),
                para("The organs of the Fund are the Board and the Management."),
            ]
        ],
        rules=structure.RW_LAW,
        language="en",
    )
    nodes = by_locator(result)
    assert nodes["contents"].children == []
    assert span(text, nodes["contents"]) == "TABLE OF CONTENTS\n\nArticle One: Purpose of this Law"
    assert nodes["art_1"].parent is nodes["title_one__chap_one"]
    assert nodes["art_1"].heading == "Purpose of this Law"
    assert nodes["title_ii__chap_one"].kind == "chapter"  # chapters restart in each title
    assert nodes["title_ii__chap_one__dvs_one"].kind == "division"
    assert nodes["art_3"].parent is nodes["title_ii__chap_one__dvs_one"]
    assert span(text, nodes["art_2"]).endswith("the following terms mean:")
    assert not result.issues


def test_rules_follow_the_jurisdiction():
    assert structure.rules_for({"id": "rw-law-2024-1"}) == structure.RW_LAW
    assert structure.rules_for({"id": "ng-electricity-act-2023"}) == structure.NG_FEDERAL_ACT
