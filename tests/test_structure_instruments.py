"""Structure rules for Nigeria's subsidiary instruments and the regulators' guidance (TXP-03): ng-subsidiary-1
(regulations, orders, rules, practice directions) and ng-guidance-1 (guidelines, circulars). Each is tried on
short invented excerpts in the shape of the texts the rules were calibrated on, as the workbench holds their
units: a statutory instrument as the Official Gazette prints it, a regulator's order, the civil aviation
regulations' decimal numbering, a rules of court's Orders, a practice direction, a guideline and a circular.

The excerpts are invented: each keeps the shape of a kind of text (its headings, numbering and edge cases)
with its words replaced, so no official text is reproduced here. Add only invented text."""

import pytest
from test_structure_families import resolve, spans_nest
from test_text_publication import build, by_locator, codes, head, para, span

from workbench import publication, structure

# A statutory instrument in the Official Gazette: the issue's cover, the title, the arrangement of the
# regulations, the title again, the commencement and enacting words, parts, regulations with their margin
# notes (one still in the words, as the reading may leave it), a schedule with the line of the regulation it
# serves, the date it was made and the explanatory note.
GAZETTE_SI = [
    [
        para("UNITY AND FAITH, PEACE AND PROGRESS"),
        head("Federal Republic of Nigeria"),
        head("Official Gazette"),
        para("Government Notice No. 77 The following is published as supplement to this Gazette :"),
    ],
    [
        head("QUELBORN (VASKET TRIMMING) REGULATIONS, 2031"),
        head("ARRANGEMENT OF REGULATIONS"),
        para("Regulation :"),
        head("PART I — OBJECTIVE"),
        para("1. Objective"),
        para("2. Application"),
        head("PART II — VASKETS"),
        para("3. Trimming of vaskets"),
        para("4. Offences"),
        head("SCHEDULE"),
    ],
    [
        head("QUELBORN (VASKET TRIMMING) REGULATIONS, 2031"),
        para("Commencement. [3rd Day of Plindor, 2031] In exercise of the powers conferred on the Council by section 9 of the Quelborn Act, the Council makes the following Regulations—"),
        head("PART I — OBJECTIVE"),
        para("1. These Regulations govern the trimming of vaskets.", notes=["Objective"]),
        para("2. These Regulations apply to every vasket trimmed in Nigeria.", notes=["Application"]),
        head("PART II — VASKETS"),
        para("Trimming of vaskets 3.—(1) A person shall not trim a vasket unless he—"),
        para("(a) holds a permit; and"),
        para("(b) keeps a record in the form in the Schedule."),
        para("(2) The Council may revoke a permit."),
        para("4. A person who contravenes regulation 3 commits an offence.", notes=["Offences"]),
    ],
    [
        head("SCHEDULE"),
        para("(Regulation 3 (1) (b))"),
        head("RECORD OF VASKETS TRIMMED"),
        para("1. Name of the trimmer ........................"),
        para("2. Number of vaskets ........................"),
        para("MADE at Abuja this 3rd day of Plindor, 2031. ZAMORA QUELL Chairman, Quelborn Council"),
        head("EXPLANATORY NOTE"),
        para("(This note does not form part of these Regulations but is intended to explain its purport) These Regulations govern the trimming of vaskets."),
    ],
]  # fmt: skip


def test_a_statutory_instrument_cover_title_contents_and_preamble():
    text, result = build(GAZETTE_SI, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    # The Gazette's cover is a notice; the title ends it, and the contents.
    assert span(text, nodes["notice_1"]).startswith("UNITY AND FAITH, PEACE AND PROGRESS")
    assert result.root.heading == "QUELBORN (VASKET TRIMMING) REGULATIONS, 2031"
    entries = [(n.label, n.target) for n in result.nodes if n.kind == "contents_entry"]
    assert entries == [
        ("PART I", "part_I"), ("1.", "sec_1"), ("2.", "sec_2"), ("PART II", "part_II"), ("3.", "sec_3"),
        ("4.", "sec_4"), ("SCHEDULE", "sch_1"),
    ]  # fmt: skip
    assert "QUELBORN" not in span(text, nodes["contents"])
    # The commencement and the powers it is made under open the preamble, to the first part.
    assert span(text, nodes["preamble"]).startswith("Commencement. [3rd Day of Plindor, 2031] In exercise")
    assert nodes["sec_1"].parent is nodes["part_I"] and nodes["sec_3"].parent is nodes["part_II"]
    spans_nest(text, result)


def test_a_statutory_instrument_regulations_margin_notes_and_dashed_numbers():
    text, result = build(GAZETTE_SI, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    assert nodes["sec_1"].heading == "Objective"
    # "3.—(1)": the regulation and its first subregulation, as the Gazette prints them; the margin note the
    # reading left before the number is the regulation's heading, and the reviewer is asked to make it a note.
    assert (nodes["sec_3"].label, nodes["sec_3"].heading) == ("3.", "Trimming of vaskets")
    assert span(text, nodes["sec_3__subsec_1"]).startswith("(1) A person shall not trim a vasket")
    assert [n.locator for n in nodes["sec_3__subsec_1"].children] == [
        "sec_3__subsec_1__para_a",
        "sec_3__subsec_1__para_b",
    ]
    (note,) = [i for i in result.issues if i.code == "margin_note_in_words"]
    assert note.message == "“Trimming of vaskets” before 3. is read as its margin note: make it one if it is"
    assert resolve(result, "reg. 3(1)(b)", "regulation").locator == "sec_3__subsec_1__para_b"


def test_a_statutory_instrument_schedule_and_closing_words():
    text, result = build(GAZETTE_SI, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    schedule = nodes["sch_1"]
    # "(Regulation 3 (1) (b))", a paragraph under the schedule's name, is the schedule's line, not its title.
    assert (schedule.label, schedule.heading) == ("SCHEDULE", "RECORD OF VASKETS TRIMMED")
    assert [n.locator for n in schedule.children] == ["sch_1__para_1", "sch_1__para_2"]
    # The date it was made and the explanatory note are notices, not part of the schedule.
    assert span(text, nodes["notice_2"]).startswith("MADE at Abuja this 3rd day of Plindor, 2031.")
    assert span(text, nodes["notice_3"]).startswith("EXPLANATORY NOTE")
    assert codes(result) == ["margin_note_in_words"]


# A regulator's order (the shape of the electricity regulator's tariff orders): its number and caption, a
# heading over each group of numbered paragraphs, a heading that prints its paragraph's number, items whose
# letters the reading lost, the date it is given, and its appendices after it, a table's caption over each
# page it runs to.
ORDER = [
    [
        para("ORDER/QRC/2031/017"),
        head("BEFORE THE QUELBORN REGULATORY COMMISSION IN THE MATTER OF THE PLINDOR 2031 VASKET TARIFF ORDER"),
        head("TITLE"),
        para("This instrument shall be cited as the Plindor 2031 Vasket Tariff Order.", kind="list_item", marker="1."),
        head("COMMENCEMENT AND TERMINATION"),
        para("This Order takes effect on 1st Plindor 2031.", kind="list_item", marker="2."),
        head("CONTEXT"),
        para("The Commission reviewed the vasket tariffs.", kind="list_item", marker="3."),
        para("Restated the trimming plan;", kind="list_item"),
        para("Approved a new baseline.", kind="list_item"),
        para("The review considered the indices below.", kind="list_item", marker="4."),
    ],
    [
        head("5. Review of the Indices"),
        para("The indices adopted are as follows:"),
        para("Exchange Rate: the rate adopted is 12.5;", kind="list_item"),
        head("6. Effective Date"),
        para("This Order shall be effective from 1st Plindor 2031."),
        para("Dated this 30th day of Grenmor 2031"),
        para("ZAMORA QUELL Chairman"),
    ],
    [
        head("Appendix 1: Vasket Tariffs"),
        {"kind": "table", "caption": "Appendix 1: Vasket Tariffs", "header_rows": 1, "rows": [["Class", "Rate"], ["A", "4"]]},
    ],
    [
        {"kind": "table", "caption": "Appendix 1: Vasket Tariffs", "header_rows": 1, "rows": [["Class", "Rate"], ["B", "5"]]},
        head("Appendix 2: Trimming Bands"),
        para("The bands are those of the Order of 2030."),
    ],
]  # fmt: skip


def test_an_orders_paragraphs_headings_and_unlettered_items():
    text, result = build(ORDER, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    assert [n.locator for n in result.root.children if n.kind == "section"] == [
        "sec_1", "sec_2", "sec_3", "sec_4", "sec_5", "sec_6",
    ]  # fmt: skip
    # A heading over a group of paragraphs is a cross-heading; one that prints the number is the paragraph's.
    assert nodes["sec_2"].heading == "" and span(text, result.root).startswith("ORDER/QRC/2031/017")
    assert (nodes["sec_5"].label, nodes["sec_5"].heading) == ("5.", "Review of the Indices")
    assert span(text, nodes["sec_5"]).endswith("Exchange Rate: the rate adopted is 12.5;")
    assert span(text, nodes["sec_3"]).endswith("Approved a new baseline.")
    assert resolve(result, "s. 5").locator == "sec_5"
    spans_nest(text, result)


def test_an_orders_appendices_after_its_date():
    text, result = build(ORDER, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    assert span(text, nodes["notice_1"]).startswith("Dated this 30th day of Grenmor 2031")
    # The appendices after the date are the order's own; a caption over a table's next page goes on in it.
    assert [(n.label, n.heading) for n in result.root.children if n.kind == "schedule"] == [
        ("Appendix 1", "Vasket Tariffs"),
        ("Appendix 2", "Trimming Bands"),
    ]
    assert [n.kind for n in nodes["sch_1"].children] == ["table", "table"]
    assert codes(result) == []


# The civil aviation regulations' decimal numbering (the shape of a Part of the Nig.CARs): the Part's heading
# above the contents, the arrangement of regulations by number, regulations numbered within the Part and
# their sub-regulations within them, a heading that prints its number, items nested as the text prints
# them, a margin note left in the words, and the title and Part's heading at the head of the next page.
CARS = [
    [
        head("QUELBORN CIVIL VASKETING REGULATIONS"),
        head("PART 19—TRIMMER PROTECTION REGULATIONS"),
        head("ARRANGEMENT OF REGULATIONS"),
        para("19.1. Applicability."),
        para("19.1.2. Definitions."),
        para("19.2. Trimming."),
    ],
    [
        head("QUELBORN CIVIL VASKETING REGULATIONS"),
        head("PART 19—TRIMMER PROTECTION REGULATIONS"),
        head("19.1. APPLICABILITY"),
        para("19.1.1. This Part applies where—"),
        para("(a) a vasket is trimmed; or"),
        para("(b) a trimmer is engaged."),
        head("19.1.2. DEFINITIONS"),
        para("19.1.2.1. In this Part—"),
        para("(1) “Trimmer” means a person who trims vaskets."),
        para("(2) “Vasket” means a vasket of any kind."),
    ],
    [
        head("QUELBORN CIVIL VASKETING REGULATIONS"),
        head("PART 19—TRIMMER PROTECTION REGULATIONS"),
        para("(3) “Plinth” means the base of a vasket."),
        para("Trimming. 19.2.—(a) A trimmer shall—"),
        para("(1) keep the vasket dry;"),
        para("(2) report every fault; and"),
        para("(b) A trimmer shall be licensed."),
        para("19.2.1.—General"),
        para("(i) trim only by day; and"),
        para("(ii) record each trimming."),
    ],
]  # fmt: skip


def test_decimal_regulations_nest_by_their_numbers():
    text, result = build(CARS, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    # The Part's heading above the contents is front matter; the contents list the regulations by number.
    assert result.root.heading == "QUELBORN CIVIL VASKETING REGULATIONS"
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == [
        "sec_19.1", "sec_19.1.2", "sec_19.2",
    ]  # fmt: skip
    part = nodes["part_19"]
    assert [n.locator for n in part.children] == ["sec_19.1", "sec_19.2"]
    assert (nodes["sec_19.1"].label, nodes["sec_19.1"].heading) == ("19.1.", "APPLICABILITY")
    assert [n.locator for n in nodes["sec_19.1"].children] == ["sec_19.1.1", "sec_19.1.2"]
    assert nodes["sec_19.1.2.1"].parent is nodes["sec_19.1.2"]
    assert [n.locator for n in nodes["sec_19.1.1"].children] == ["sec_19.1.1__para_a", "sec_19.1.1__para_b"]
    # The title and the Part's heading at the next page's head are running heads: the definitions go on
    # through them, and the reviewer is asked to remove them (the Part's heading over the contents is not one).
    assert nodes["sec_19.1.2.1__subsec_3"].parent is nodes["sec_19.1.2.1"]
    assert [(i.page, i.unit) for i in result.issues if i.code == "running_head"] == [(3, 0), (3, 1)]
    spans_nest(text, result)


def test_decimal_regulations_items_nest_as_printed_and_margin_notes_in_the_words():
    text, result = build(CARS, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    # "Trimming. 19.2.—(a) …": the margin note, the regulation and its first item in one unit.
    assert (nodes["sec_19.2"].heading, nodes["sec_19.2__para_a"].label) == ("Trimming.", "(a)")
    assert span(text, nodes["sec_19.2__para_a"]).startswith("(a) A trimmer shall—")
    # Numbered items within a lettered one, as this text prints them; (b) goes on beside (a).
    assert [n.locator for n in nodes["sec_19.2__para_a"].children] == [
        "sec_19.2__para_a__subsec_1",
        "sec_19.2__para_a__subsec_2",
    ]
    assert nodes["sec_19.2__para_b"].parent is nodes["sec_19.2"]
    # "19.2.1.—General" is a regulation within 19.2; its roman items within it.
    assert nodes["sec_19.2.1"].parent is nodes["sec_19.2"]
    assert [n.label for n in nodes["sec_19.2.1"].children] == ["(i)", "(ii)"]
    assert resolve(result, "reg. 19.1.1(b)", "regulation").locator == "sec_19.1.1__para_b"
    assert resolve(result, "reg. 19.2(a)(2)", "regulation").locator == "sec_19.2__para_a__subsec_2"


# A rules of court's Orders, whose rules restart in each: the arrangement of rules by Order, each Order's
# rules within it.
RULES_OF_COURT = [
    [
        head("QUELBORN COURT RULES, 2031"),
        head("ARRANGEMENT OF RULES"),
        para("Rule :"),
        head("ORDER 1 — GENERAL"),
        para("1. Short title"),
        para("2. Interpretation"),
        head("ORDER 2 — ADMINISTRATION"),
        para("1. Chief Registrar"),
    ],
    [
        head("QUELBORN COURT RULES, 2031"),
        para("In exercise of the powers conferred on me by section 4 of the Quelborn Court Act, I make the following Rules—"),
        head("ORDER 1 — GENERAL"),
        para("1. These Rules may be cited as the Quelborn Court Rules, 2031.", notes=["Short title"]),
        para("2. In these Rules, “Court” means the Quelborn Court.", notes=["Interpretation"]),
        head("ORDER 2 — ADMINISTRATION"),
        para("1.—(1) There shall be a Chief Registrar of the Court.", notes=["Chief Registrar"]),
        para("(2) The Chief Registrar keeps the seal."),
    ],
]  # fmt: skip


def test_a_rules_of_courts_orders_qualify_their_rules():
    text, result = build(RULES_OF_COURT, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    assert [(n.label, n.target) for n in result.nodes if n.kind == "contents_entry"] == [
        ("ORDER 1", "order_1"), ("1.", "order_1__sec_1"), ("2.", "order_1__sec_2"),
        ("ORDER 2", "order_2"), ("1.", "order_2__sec_1"),
    ]  # fmt: skip
    assert (nodes["order_2"].label, nodes["order_2"].heading) == ("ORDER 2", "ADMINISTRATION")
    assert nodes["order_2__sec_1"].parent is nodes["order_2"]
    assert span(text, nodes["order_2__sec_1__subsec_2"]) == "(2) The Chief Registrar keeps the seal."
    # Each Order numbers its rules from 1: no repeated locator, no rule out of order, every entry found.
    assert codes(result) == []
    assert resolve(result, "Order 2, r. 1(2)").locator == "order_2__sec_1__subsec_2"
    assert resolve(result, "Order 1 rule 2").locator == "order_1__sec_2"
    spans_nest(text, result)


# A practice direction: its arrangement with page numbers, its commencement and the powers it is made under,
# numbered headings each over its words, and numbered and lettered items.
PRACTICE_DIRECTION = [
    [
        head("QUELBORN COURT"),
        head("QUELBORN COURT (VASKET FILINGS) PRACTICE DIRECTIONS, 2031"),
        head("ARRANGEMENT"),
        para("Commencement ............................................ 1"),
        para("1. Purpose .................................................. 1", kind="list_item"),
        para("2. Filing of Vaskets ......................................... 2", kind="list_item"),
    ],
    [
        para("Commencement Date: 1st Plindor, 2031 In the exercise of the powers conferred upon me by section 9 of the Quelborn Court Act, I make these Practice Directions."),
        head("1. Purpose"),
        para("These Practice Directions govern the filing of vaskets."),
        head("2. Filing of Vaskets"),
        para("(1) Every vasket shall be filed:", kind="list_item"),
        para("(a) by its owner; and", kind="list_item"),
        para("(b) within seven days.", kind="list_item"),
    ],
]  # fmt: skip


def test_a_practice_directions_numbered_headings():
    text, result = build(PRACTICE_DIRECTION, rules=structure.NG_SUBSIDIARY)
    nodes = by_locator(result)
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == ["sec_1", "sec_2"]
    assert span(text, nodes["preamble"]).startswith("Commencement Date: 1st Plindor, 2031 In the exercise")
    assert (nodes["sec_1"].heading, nodes["sec_2"].heading) == ("Purpose", "Filing of Vaskets")
    assert span(text, nodes["sec_1"]).endswith("These Practice Directions govern the filing of vaskets.")
    assert nodes["sec_2__subsec_1__para_b"].label == "(b)"
    assert codes(result) == []
    spans_nest(text, result)


# A regulator's guideline: its contents with dotted leaders, numbered headings and the decimal paragraphs
# under each, one holding two, and a form as its schedule.
GUIDELINE = [
    [
        para("QRC QUELBORN REGULATORY COMMISSION"),
        head("GUIDELINES ON VASKET TRIMMING"),
        head("Contents"),
        para("1. Introduction .............................. 2", kind="list_item"),
        para("2. Requirements .............................. 3", kind="list_item"),
    ],
    [
        head("1. Introduction"),
        para("1.1 These Guidelines are issued by the Commission under section 7 of the Quelborn Act."),
        head("2. Requirements"),
        para("2.1 A trimmer shall keep a register. 2.2 The register shall be kept for six years."),
        para("2.3 The Commission may inspect the register:", kind="list_item"),
        para("Date of trimming", kind="list_item"),
        head("SCHEDULE"),
        head("VASKET TRIMMING FORM 1"),
        head("1. TRIMMER'S INFORMATION"),
        para("Full name:", kind="list_item"),
    ],
]  # fmt: skip


def test_a_guidelines_decimal_paragraphs_under_numbered_headings():
    text, result = build(GUIDELINE, rules=structure.NG_GUIDANCE)
    nodes = by_locator(result)
    assert result.root.heading == "GUIDELINES ON VASKET TRIMMING"
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == ["sec_1", "sec_2"]
    # The contents end where the text prints again what they list: no enacting words to wait for.
    assert nodes["sec_1"].heading == "Introduction"
    assert [n.locator for n in nodes["sec_2"].children] == ["sec_2.1", "sec_2.3"]
    assert span(text, nodes["sec_2.3"]).endswith("Date of trimming")
    assert (nodes["sch_1"].heading, nodes["sch_1__para_1"].heading) == (
        "VASKET TRIMMING FORM 1",
        "TRIMMER'S INFORMATION",
    )
    assert codes(result) == []
    spans_nest(text, result)


# A circular: its reference and addressees, its subject, a first paragraph printed without its number and
# the numbered ones after it.
CIRCULAR = [
    [
        para("QRC/VSK/CIR/12/2031 4th Plindor 2031 Circular to all Vasket Trimmers"),
        head("MANDATORY REGISTRATION OF VASKET TRIMMERS"),
        para("The Commission has noted that some trimmers are not registered."),
        para("Every trimmer shall register with the Commission by 1st Grenmor 2031.", kind="list_item", marker="2."),
        para("A trimmer who fails to register shall be sanctioned.", kind="list_item", marker="3."),
        para("Signed ZAMORA QUELL Commissioner"),
    ],
]  # fmt: skip


def test_a_circulars_numbered_paragraphs():
    text, result = build(CIRCULAR, rules=structure.NG_GUIDANCE)
    nodes = by_locator(result)
    assert [n.locator for n in result.root.children] == ["sec_2", "sec_3"]
    assert span(text, result.root).startswith("QRC/VSK/CIR/12/2031")
    assert span(text, nodes["sec_3"]).endswith("Signed ZAMORA QUELL Commissioner")
    assert codes(result) == []


@pytest.mark.parametrize(
    "kind, rules",
    [
        ("regulation", "ng-subsidiary-1"),
        ("order", "ng-subsidiary-1"),
        ("rules", "ng-subsidiary-1"),
        ("practice_direction", "ng-subsidiary-1"),
        ("guideline", "ng-guidance-1"),
        ("circular", "ng-guidance-1"),
    ],
)
def test_nigerian_instruments_are_read_by_their_rules_and_can_be_approved(kind, rules):
    pages = [{"page": n, "units": units} for n, units in enumerate(GAZETTE_SI, start=1)]
    built = publication.build({"id": f"ng-{kind}-example", "jurisdiction": "ng", "type": kind}, pages)
    assert built.rules_id == rules and not built.errors
