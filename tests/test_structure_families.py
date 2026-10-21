"""Structure rules for the Commonwealth families that are not Nigeria's (TXP-03): the East African
Community's Acts, and Kenya's, South Africa's and Ghana's. Each is tried on short excerpts of an official
text it was calibrated on, reviewed units as the workbench holds them: every heading level, numbering
form and edge case the family prints, and a pinpoint resolved to its part.

The excerpts are a few provisions each, copied from the official file; running heads and page numbers
are left out, as the readings keep them apart. Some sources' terms allow non-commercial use only: never
add more of their text than a test needs."""

import pytest
from test_text_publication import build, by_locator, codes, head, para, span

from api import pinpoint
from workbench import publication, structure


def chain(node):
    """A part and the parts it is in, outermost first, as the provision lookup reads them."""
    parts = []
    while node is not None:
        parts.append(node)
        node = node.parent
    return parts[::-1]


def resolve(result, cited, document_type=None):
    """The one part a cited pinpoint names: the part whose own citation reads back to the same key."""
    key = pinpoint.read(cited)
    named = [n for n in result.nodes if (pinpoint.cite(chain(n), document_type) or (None,))[0] == key]
    assert len(named) == 1, (cited, [n.locator for n in named])
    return named[0]


def spans_nest(text, result):
    """Every leaf held once; every span decodes and lies within its parent's."""
    raw = text.text.encode()
    assert sorted(i for n in result.nodes for i in n.leaves) == list(range(len(text.leaves)))
    for node in result.nodes:
        raw[node.start : node.end].decode()
        if node.parent:
            assert node.parent.start <= node.start <= node.end <= node.parent.end


# --- eac-act-1 ---------------------------------------------------------------------------------------

# The East African Community Customs Management Act, 2004, revised edition 2009, as the Community
# publishes it (pages 1, 2, 12, 13, 20, 27, 134 and 135 of the PDF):
# https://www.eac.int/documents?controller=download&task=download.file&file=b74b38e0-687f-4238-8cf9-f092e7edb5a1&name=EAC%20CMA%20(2008%20DEC).pdf
EAC_CMA = [
    [
        para("EAST AFRICAN COMMUNITY", align="center"),
        head("The East African Community Customs Management Act, 2004"),
    ],
    [
        head("THE EAST AFRICAN COMMUNITY CUSTOMS MANAGEMENT ACT, 2004"),
        head("ARRANGEMENT OF SECTIONS"),
        para("Section Title"),
        head("PART I"),
        head("PRELIMINARY PROVISIONS"),
        para("1. Short title, application and commencement."),
        para("2. Interpretation."),
        head("PART II"),
        head("ADMINISTRATION"),
        para("3. The Directorate of Customs."),
        para("4. Functions of the Directorate."),
        head("PART III"),
        head("IMPORTATION"),
        head("Prohibited and Restricted Imports"),
        para("18. Prohibited and restricted imports."),
        head("SCHEDULES"),
        para("SECOND SCHEDULE—Prohibited and Restricted Imports Generally"),
    ],
    [
        head("THE EAST AFRICAN COMMUNITY", align="center"),
        para("No. 1 of 2005", align="center"),
        para("Date of assent: 31st December, 2004"),
        para("Date of commencement: 1st January, 2005"),
        para("An Act of the Community to make provisions for the management and administration of Customs and for related matters."),
        head("PART I"),
        head("PRELIMINARY PROVISIONS"),
        para("1. (1) This Act may be cited as the East African Community Customs Management Act, 2004.", notes=["Short title, application and commencement."]),
        para("(2) This Act shall apply to the Partner States."),
        para("(3) This Act shall commence on the date to be appointed by the Council."),
        para("2. (1) In this Act, unless the context otherwise requires—", notes=["Interpretation."]),
        para("“agent” in relation to an aircraft, vehicle or vessel, includes any person who notifies the proper officer in writing that he or she intends to act as the agent and who, or on whose behalf any person authorised by him or her, signs any document required or authorised by this Act to be signed by an agent:"),
        para("Provided that the owner of any aircraft, vehicle or vessel, if resident or represented in a Partner State, shall either himself or herself or through his or her representative be deemed to be the agent for all the purposes of this Act if no such agent is appointed;"),
        para("“approved place of loading” and “approved place of unloading” mean any quay, jetty, wharf, or other place, including any part of a Customs airport, appointed by the Commissioner by notice in the Gazette to be a place where goods may be loaded or unloaded;"),
    ],
    [
        head("PART II"),
        head("ADMINISTRATION"),
        para("3. The Directorate of Customs as established by the Council under the Treaty shall be responsible for the initiation of policies on Customs and related trade matters in the Community and the coordination of such policies in the Partner States.", notes=["Directorate of Customs."]),
        para("4. (1) Without prejudice to the generality of section 3, the Directorate shall, in relation to management and administration of Customs, coordinate and monitor—", notes=["Functions of the Directorate"]),
        para("(a) administration of the Common External Tariff;"),
        para("(b) enforcement of the Customs law of the Community;"),
        para("(c) trade facilitation as provided for in Article 6 of the Protocol;"),
        para("(d) administration of the Rules of Origin;"),
        para("(e) compilation and dissemination of trade statistics;"),
        para("(f) application and interfacing of information technology in Customs administration;"),
        para("(g) training in Customs related matters;"),
        para("(h) quality control in Customs operations and enforcement of compliance;"),
        para("(i) Customs related negotiations; and"),
        para("(j) activities of the Commissioners in the implementation of this Act"),
    ],
    [
        head("PART III"),
        head("IMPORTATION"),
        head("Prohibited and Restricted Imports"),
        para("18. (1) The goods specified in Part A of the Second Schedule are prohibited goods and the importation thereof is prohibited.", notes=["Prohibited and restricted imports."]),
        para("(2) The goods specified in Part B of the Second Schedule are restricted goods and the importation thereof, save in accordance with any conditions regulating their importation, is prohibited."),
    ],
    [
        head("SECOND SCHEDULE", align="center"),
        head("(ss. 18, 19, and 20.)", align="center"),
        head("PROHIBITED AND RESTRICTED IMPORTS GENERALLY", align="center"),
        head("PART A—PROHIBITED GOODS"),
        para("(1) All goods the importation of which is for the time being prohibited under this Act or by any written law for the time being in force in the Partner State."),
        para("(11) The following Agricultural and Industrial Chemicals:"),
        para("(a) Agricultural Chemicals"),
        para("(i) 2.4 - T"),
        para("(ii) Aldrin"),
        head("PART B—RESTRICTED GOODS"),
        para("(1) All goods the importation of which is for the time being regulated under this Act or by any written law for the time being in force in the Partner State."),
    ],
]  # fmt: skip


def test_eac_acts_title_contents_long_title_and_parts():
    text, result = build(EAC_CMA, rules=structure.EAC_ACT)
    nodes = by_locator(result)
    # The Act's title, not the cover's first line; the title printed again ends nothing in the contents.
    assert result.root.heading == "The East African Community Customs Management Act, 2004"
    entries = [(n.label, n.target) for n in result.nodes if n.kind == "contents_entry"]
    assert entries == [
        ("PART I", "part_I"), ("1.", "sec_1"), ("2.", "sec_2"), ("PART II", "part_II"), ("3.", "sec_3"),
        ("4.", "sec_4"), ("PART III", "part_III"), ("18.", "sec_18"), ("SECOND SCHEDULE", "sch_2"),
    ]  # fmt: skip
    # "An Act of the Community to …" opens the preamble (Nigeria's rules leave it in the contents).
    assert span(text, nodes["preamble"]).startswith("An Act of the Community to make provisions")
    assert (nodes["part_I"].label, nodes["part_I"].heading) == ("PART I", "PRELIMINARY PROVISIONS")
    assert nodes["sec_1"].parent is nodes["part_I"] and nodes["sec_4"].parent is nodes["part_II"]
    spans_nest(text, result)


def test_eac_sections_margin_notes_definitions_and_cross_headings():
    text, result = build(EAC_CMA, rules=structure.EAC_ACT)
    nodes = by_locator(result)
    assert nodes["sec_1"].heading == "Short title, application and commencement."
    assert span(text, nodes["sec_1__subsec_1"]).startswith("(1) This Act may be cited")
    assert nodes["sec_2__subsec_1__def_agent"].kind == "definition"
    # Two terms meaning one thing ("… and “approved place of unloading” mean …") are one definition.
    assert nodes["sec_2__subsec_1__def_approved_place_of_loading"].label == "approved place of loading"
    # "(i)" after "(h)" is a letter, as in Nigeria's rules.
    assert [n.locator for n in nodes["sec_4__subsec_1"].children][-3:] == [
        "sec_4__subsec_1__para_h",
        "sec_4__subsec_1__para_i",
        "sec_4__subsec_1__para_j",
    ]
    # A cross-heading inside a part is a leaf of the part, before the section it groups.
    assert span(text, nodes["part_III"]).startswith(
        "PART III\n\nIMPORTATION\n\nProhibited and Restricted Imports"
    )
    assert nodes["sec_18"].parent is nodes["part_III"]


def test_eac_schedules_with_lettered_parts_and_their_reference_line():
    text, result = build(EAC_CMA, rules=structure.EAC_ACT)
    nodes = by_locator(result)
    schedule = nodes["sch_2"]
    # The line of the sections it serves is the schedule's, not its title.
    assert (schedule.label, schedule.heading) == (
        "SECOND SCHEDULE",
        "PROHIBITED AND RESTRICTED IMPORTS GENERALLY",
    )
    assert span(text, schedule).startswith("SECOND SCHEDULE\n\n(ss. 18, 19, and 20.)")
    # Part A and Part B restart their numbers, so the part qualifies them: no repeated locator.
    assert nodes["sch_2__part_A"].heading == "PROHIBITED GOODS"
    assert "sch_2__part_A__subpara_1" in nodes and "sch_2__part_B__subpara_1" in nodes
    assert nodes["sch_2__part_A__subpara_11__subpara_a__subpara_ii"].kind == "subparagraph"
    assert codes(result) == []


def test_eac_pinpoints_resolve():
    _, result = build(EAC_CMA, rules=structure.EAC_ACT)
    assert resolve(result, "s. 4").locator == "sec_4"
    assert resolve(result, "s. 4(1)(i)").locator == "sec_4__subsec_1__para_i"
    assert resolve(result, "s. 18(2)").locator == "sec_18__subsec_2"


# --- ke-act-1 ----------------------------------------------------------------------------------------

# The Data Protection Act, 2019 (No. 24 of 2019), Kenya Gazette Supplement No. 181 (pages 1, 2, 6, 9, 10
# and 45 of the PDF), as the Office of the Data Protection Commissioner publishes it:
# https://www.odpc.go.ke/wp-content/uploads/2024/02/TheDataProtectionAct__No24of2019.pdf
KE_DPA = [
    [
        head("SPECIAL ISSUE"),
        para("Kenya Gazette Supplement No. 181 (Acts No. 24)"),
        head("KENYA GAZETTE SUPPLEMENT", align="center"),
        head("ACTS, 2019", align="center"),
    ],
    [
        head("THE DATA PROTECTION ACT", align="center"),
        para("No. 24 of 2019", align="center"),
        para("Date of Assent: 8th November, 2019", align="center"),
        para("Date of Commencement: 25th November, 2019", align="center"),
        head("ARRANGEMENT OF SECTIONS", align="center"),
        para("Sections"),
        head("PART I—PRELIMINARY", align="center"),
        para("1. —Short title."),
        para("2. —Interpretation."),
        para("4. —Application."),
        head("PART II—ESTABLISHMENT OF THE OFFICE OF THE DATA PROTECTION COMMISSIONER", align="center"),
        para("5. —Establishment of the Office."),
    ],
    [
        head("THE DATA PROTECTION ACT, 2019", align="center"),
        para("AN ACT of Parliament to give effect to Article 31(c) and (d) of the Constitution; to establish the Office of the Data Protection Commissioner; to make provision for the regulation of the processing of personal data; to provide for the rights of data subjects and obligations of data controllers and processors; and for connected purposes"),
        para("ENACTED by Parliament of Kenya, as follows—"),
        head("PART I—PRELIMINARY", align="center"),
        para("1. This Act may be cited as the Data Protection Act, 2019.", notes=["Short title."]),
        para("2. In this Act, unless the context otherwise requires—", notes=["Interpretation."]),
        para('"anonymisation" means the removal of personal identifiers from personal data so that the data subject is no longer identifiable;'),
        para('"data" means information which—'),
        para("(a) is processed by means of equipment operating automatically in response to instructions given for that purpose;"),
        para("(b) is recorded with intention that it should be processed by means of such equipment;"),
    ],
    [
        para("4. This Act applies to the processing of personal data—", notes=["Application."]),
        para("(a) entered in a record, by or for a data controller or processor, by making use of automated or non-automated means:"),
        para("Provided that when the recorded personal data is processed by non-automated means, it forms a whole or part of a filing system;"),
        para("(b) by a data controller or data processor who—"),
        para("(i) is established or ordinarily resident in Kenya and processes personal data while in Kenya; or"),
        para("(ii) not established or ordinarily resident in Kenya, but processing personal data of data subjects located in Kenya."),
    ],
    [
        head("PART II—ESTABLISHMENT OF THE OFFICE OF DATA PROTECTION COMMISSIONER", align="center"),
        para("5. (1) There is established the office of the Data Protection Commissioner which shall be a body corporate with perpetual succession and a common seal and shall in its corporate name, be capable of—", notes=["Establishment of the Office."]),
        para("(a) suing and being sued;"),
        para("(b) taking, purchasing or otherwise acquiring, holding, charging or disposing of movable and immovable property;"),
        para("(c) entering into contracts; and"),
        para("(d) doing such other legal acts necessary for the proper performance of the functions of the Office."),
        para("(2) The Office is designated as a State Office in accordance with Article 260 (q) of the Constitution."),
    ],
    [
        head("FIRST SCHEDULE (s.15)"),
        para("I,…………………………, make oath/solemnly affirm/declare that I will faithfully and honestly fulfil my duties as the Data Commissioner in conformity with the Data Protection Act and that I shall not, without the due authority in that behalf, disclose or make known any matter or thing which comes to my knowledge by reason of discharge of my duties"),
        para("Magistrate/Judge"),
    ],
]  # fmt: skip


def test_kenyan_acts_cover_title_contents_and_long_title():
    text, result = build(KE_DPA, rules=structure.KE_ACT)
    nodes = by_locator(result)
    # The gazette's cover comes first; the Act's title is "THE DATA PROTECTION ACT", not "SPECIAL ISSUE".
    assert result.root.heading == "THE DATA PROTECTION ACT"
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == [
        "part_I", "sec_1", "sec_2", "sec_4", "part_II", "sec_5",
    ]  # fmt: skip
    # The title printed again ends the contents, and "AN ACT of Parliament to …" opens the preamble.
    assert "THE DATA PROTECTION ACT, 2019" not in span(text, nodes["contents"])
    assert span(text, nodes["preamble"]).startswith("AN ACT of Parliament to give effect")
    assert span(text, nodes["preamble"]).endswith("ENACTED by Parliament of Kenya, as follows—")
    assert nodes["part_II"].heading == "ESTABLISHMENT OF THE OFFICE OF DATA PROTECTION COMMISSIONER"
    spans_nest(text, result)
    assert codes(result) == []


def test_kenyan_sections_notes_definitions_provisos_and_schedules():
    text, result = build(KE_DPA, rules=structure.KE_ACT)
    nodes = by_locator(result)
    assert [nodes[f"sec_{n}"].heading for n in (1, 2, 4, 5)] == [
        "Short title.", "Interpretation.", "Application.", "Establishment of the Office.",
    ]  # fmt: skip
    assert [n.label for n in nodes["sec_2"].children] == ["anonymisation", "data"]
    assert nodes["sec_2__def_data__para_b"].parent is nodes["sec_2__def_data"]
    # The proviso stays with paragraph (a); (b)'s sub-paragraphs are roman.
    assert span(text, nodes["sec_4__para_a"]).endswith("it forms a whole or part of a filing system;")
    assert nodes["sec_4__para_b__subpara_ii"].kind == "subparagraph"
    assert nodes["sch_1"].label == "FIRST SCHEDULE"
    assert span(text, nodes["sch_1"]).endswith("Magistrate/Judge")


def test_kenyan_pinpoint_resolves():
    _, result = build(KE_DPA, rules=structure.KE_ACT)
    assert resolve(result, "section 5(1)(a)").locator == "sec_5__subsec_1__para_a"
    assert resolve(result, "s. 4(b)(ii)").locator == "sec_4__para_b__subpara_ii"
    assert resolve(result, 's. 2, "anonymisation"').locator == "sec_2__def_anonymisation"


# --- za-act-1 ----------------------------------------------------------------------------------------

# The Protection of Personal Information Act, 2013 (Act No. 4 of 2013), Government Gazette No. 37067
# (pages 1-3, 6, 7, 12, 20, 25 and 55 of the PDF), as gov.za publishes it, with its note on the
# languages it publishes: https://www.gov.za/sites/default/files/gcis_document/201409/3706726-11act4of2013popi.pdf
ZA_POPIA = [
    [
        para("Please note that most Acts are published in English and another South African official language. Currently we only have capacity to publish the English versions. This means that this document will only contain even numbered pages as the other language is printed on uneven numbered pages."),
        head("Government Gazette"),
        head("REPUBLIC OF SOUTH AFRICA", align="center"),
        para("Vol. 581 Cape Town 26 November 2013 No. 37067"),
        head("THE PRESIDENCY", align="center"),
        para("No. 912 26 November 2013"),
        para("It is hereby notified that the President has assented to the following Act, which is hereby published for general information:–"),
        para("No. 4 of 2013: Protection of Personal Information Act, 2013."),
    ],
    [
        head("GENERAL EXPLANATORY NOTE:"),
        para("[ ] Words in bold type in square brackets indicate omissions from existing enactments."),
        para("Words underlined with a solid line indicate insertions in existing enactments."),
        para("(English text signed by the President)", align="center"),
        para("(Assented to 19 November 2013)", align="center"),
        head("ACT", align="center"),
        para("To promote the protection of personal information processed by public and private bodies; to introduce certain conditions so as to establish minimum requirements for the processing of personal information; to provide for the establishment of an Information Regulator to exercise certain powers and to perform certain duties and functions in terms of this Act and the Promotion of Access to Information Act, 2000; to provide for the issuing of codes of conduct; to provide for the rights of persons regarding unsolicited electronic communications and automated decision making; to regulate the flow of personal information across the borders of the Republic; and to provide for matters connected therewith."),
        head("PREAMBLE", align="center"),
        head("RECOGNISING THAT—"),
        para("● section 14 of the Constitution of the Republic of South Africa, 1996, provides that everyone has the right to privacy;"),
    ],
    [
        para("PARLIAMENT of the Republic of South Africa therefore enacts, as follows:—"),
        head("CONTENTS OF ACT", align="center"),
        head("CHAPTER 1", align="center"),
        head("DEFINITIONS AND PURPOSE", align="center"),
        para("1. Definitions"),
        head("CHAPTER 3", align="center"),
        head("CONDITIONS FOR LAWFUL PROCESSING OF PERSONAL INFORMATION", align="center"),
        head("Part A", align="center"),
        head("Processing of personal information in general", align="center"),
        head("Condition 1", align="center"),
        head("Accountability", align="center"),
        para("8. Responsible party to ensure conditions for lawful processing"),
        head("Part B", align="center"),
        head("Processing of special personal information", align="center"),
        para("26. Prohibition on processing of special personal information"),
        head("CHAPTER 5", align="center"),
        head("SUPERVISION", align="center"),
        head("Part A", align="center"),
        head("Information Regulator", align="center"),
        para("39. Establishment of Information Regulator"),
        head("SCHEDULE", align="center"),
        para("Laws amended by section 110"),
    ],
    [
        head("CHAPTER 1", align="center"),
        head("DEFINITIONS AND PURPOSE", align="center"),
        head("Definitions"),
        para("1. In this Act, unless the context indicates otherwise—"),
        para("‘‘biometrics’’ means a technique of personal identification that is based on physical, physiological or behavioural characterisation including blood typing, fingerprinting, DNA analysis, retinal scanning and voice recognition;"),
        para("‘‘de-identify’’, in relation to personal information of a data subject, means to delete any information that—"),
        para("(a) identifies the data subject;"),
        para("(b) can be used or manipulated by a reasonably foreseeable method to identify the data subject; or"),
        para("(c) can be linked by a reasonably foreseeable method to other information that identifies the data subject,"),
        para("and ‘‘de-identified’’ has a corresponding meaning;"),
    ],
    [
        head("CHAPTER 3", align="center"),
        head("CONDITIONS FOR LAWFUL PROCESSING OF PERSONAL INFORMATION", align="center"),
        head("Part A", align="center"),
        head("Processing of personal information in general", align="center"),
        head("Condition 1", align="center"),
        head("Accountability", align="center"),
        head("Responsible party to ensure conditions for lawful processing"),
        para("8. The responsible party must ensure that the conditions set out in this Chapter, and all the measures that give effect to such conditions, are complied with at the time of the determination of the purpose and means of the processing and during the processing itself."),
        head("Part B", align="center"),
        head("Processing of special personal information", align="center"),
        head("Prohibition on processing of special personal information"),
        para("26. A responsible party may, subject to section 27, not process personal information concerning—"),
        para("(a) the religious or philosophical beliefs, race or ethnic origin, trade union membership, political persuasion, health or sex life or biometric information of a data subject; or"),
        para("(b) the criminal behaviour of a data subject to the extent that such information relates to—"),
        para("(i) the alleged commission by a data subject of any offence; or"),
        para("(ii) any proceedings in respect of any offence allegedly committed by a data subject or the disposal of such proceedings."),
    ],
    [
        head("CHAPTER 5", align="center"),
        head("SUPERVISION", align="center"),
        head("Part A", align="center"),
        head("Information Regulator", align="center"),
        head("Establishment of Information Regulator"),
        para("39. There is hereby established a juristic person to be known as the Information Regulator, which—"),
        para("(a) has jurisdiction throughout the Republic;"),
        para("(b) is independent and is subject only to the Constitution and to the law and must be impartial and perform its functions and exercise its powers without fear, favour or prejudice;"),
        para("(c) must exercise its powers and perform its functions in accordance with this Act and the Promotion of Access to Information Act; and"),
        para("(d) is accountable to the National Assembly."),
    ],
    [
        head("SCHEDULE", align="center"),
        head("LAWS AMENDED BY SECTION 110", align="center"),
        {"kind": "table", "caption": "", "header_rows": 1, "rows": [["No. and year of law", "Short title", "Extent of repeal or amendment"]]},
    ],
]  # fmt: skip


def test_south_african_notices_preamble_then_contents():
    text, result = build(ZA_POPIA, rules=structure.ZA_ACT)
    nodes = by_locator(result)
    # gov.za's note, the gazette's front page with the notice of assent, and its explanatory note.
    assert [n.heading[:24] for n in result.nodes if n.kind == "notice"] == [
        "Please note that most Ac", "Government Gazette", "GENERAL EXPLANATORY NOTE",
    ]  # fmt: skip
    assert span(text, nodes["notice_2"]).endswith("Protection of Personal Information Act, 2013.")
    # "ACT" above the long title opens the preamble, which runs to the enacting words.
    assert span(text, nodes["preamble"]).startswith("ACT\n\nTo promote the protection")
    assert span(text, nodes["preamble"]).endswith("therefore enacts, as follows:—")
    # The contents follow the enacting words; a part is listed under its chapter.
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == [
        "chap_1", "sec_1", "chap_3", "chap_3__part_A", "sec_8", "chap_3__part_B", "sec_26", "chap_5",
        "chap_5__part_A", "sec_39", "sch_1",
    ]  # fmt: skip
    # The body begins where a chapter the contents list is printed again; that is no contents warning.
    assert nodes["chap_1"].page_start == 4
    assert codes(result) == []
    spans_nest(text, result)


def test_south_african_chapters_parts_headings_above_and_definitions():
    text, result = build(ZA_POPIA, rules=structure.ZA_ACT)
    nodes = by_locator(result)
    assert (nodes["chap_3"].label, nodes["chap_3"].heading) == (
        "CHAPTER 3",
        "CONDITIONS FOR LAWFUL PROCESSING OF PERSONAL INFORMATION",
    )
    # Parts restart in each chapter: Chapter 3's Part A and Chapter 5's are two parts.
    assert nodes["chap_3__part_A"].parent is nodes["chap_3"]
    assert nodes["chap_5__part_A"].heading == "Information Regulator"
    # A section's heading is printed above it, and held by it.
    assert [nodes[f"sec_{n}"].heading for n in (1, 8, 26, 39)] == [
        "Definitions",
        "Responsible party to ensure conditions for lawful processing",
        "Prohibition on processing of special personal information",
        "Establishment of Information Regulator",
    ]
    assert span(text, nodes["sec_8"]).startswith(
        "Responsible party to ensure conditions for lawful processing\n\n8."
    )
    # "Condition 1" and its name group the sections after them: leaves of the part.
    assert span(text, nodes["chap_3__part_A"]).startswith(
        "Part A\n\nProcessing of personal information in general\n\nCondition 1\n\nAccountability\n\n"
    )
    assert nodes["sec_8"].parent is nodes["chap_3__part_A"]
    # Terms in ‘‘ ’’, and a term's scope before its meaning.
    assert [n.label for n in nodes["sec_1"].children] == ["biometrics", "de-identify"]
    assert span(text, nodes["sec_1__def_de_identify"]).endswith(
        "and ‘‘de-identified’’ has a corresponding meaning;"
    )
    assert nodes["sch_1__table_1"].kind == "table"


def test_south_african_pinpoints_resolve():
    _, result = build(ZA_POPIA, rules=structure.ZA_ACT)
    assert resolve(result, "s. 26(b)(ii)").locator == "sec_26__para_b__subpara_ii"
    assert resolve(result, "Chapter 5").locator == "chap_5"
    assert resolve(result, "s. 39(d)").locator == "sec_39__para_d"


# --- gh-act-1 ----------------------------------------------------------------------------------------

# The Data Protection Act, 2012 (Act 843) (pages 1, 3, 5, 6, 8, 38 and 43 of the PDF), as the Data Protection Commission
# publishes it: https://dpc.gov.gh/wp-content/uploads/2025/05/data-protection-act-2012-act-843.pdf
GH_843 = [
    [
        head("Data Protection Act, 2012"),
        head("ARRANGEMENT OF SECTIONS", align="center"),
        para("Section"),
        head("Data Protection Commission", align="center"),
        para("1. Establishment of Data Protection Commission"),
        para("2. Object of the Commission"),
        para("4. Governing body of the Commission"),
        head("Administration", align="center"),
        para("11. Appointment of Executive Director"),
        head("Miscellaneous and general provisions", align="center"),
        para("96. Interpretation"),
    ],
    [
        head("ENTITLED", align="center"),
        head("DATA PROTECTION ACT, 2012", align="center"),
        para("AN ACT to establish a Data Protection Commission, to protect the privacy of the individual and personal data by regulating the processing of personal information, to provide the process to obtain, hold, use or disclose personal information and for related matters."),
        para("DATE OF ASSENT: 10th May, 2012", align="center"),
        para("PASSED by Parliament and assented to by the President:"),
        head("Data Protection Commission", align="center"),
        head("Establishment of Data Protection Commission"),
        para("1. (1) There is established by this Act a Data Protection Commission. (2) Where there is hindrance to the acquisition of property, the property may be acquired for the Commission under the State Property and Contracts Act, 1960 (C.A.6) or the State Lands Act, 1962 (Act 125) and the costs shall be borne by the Commission."),
        head("Object of the Commission"),
        para("2. The object of the Commission is to"),
        para("(a) protect the privacy of the individual and personal data by regulating the processing of personal information, and"),
        para("(b) provide the process to obtain, hold, use or disclose personal information."),
    ],
    [
        head("Governing body of the Commission"),
        para("4. (1) The governing body of the Commission is a Board consisting of"),
        para("(a) a chairperson;"),
        para("(b) one representative from the following:"),
        para("(i) National Communications Authority not below the rank of a Director;"),
        para("(ii) Commission on Human Rights and Administrative Justice not below the rank of a Deputy Commissioner;"),
        para("(iii) Ministry of Communications not below the rank of a Director;"),
        para("(iv) National Information Technology Agency not below the rank of a Director;"),
        para("(v) Bank of Ghana not below the level of Deputy Governor; and"),
        para("(vi) Statistical Service not below the rank of a Director;"),
        para("(c) one representative elected by the Industry Forum;"),
        para("(d) two other persons nominated by the President at least one of whom is a woman; and"),
        para("(e) the Executive Director of the Commission."),
        para("(2) The members of the Board shall be appointed by the President in accordance with article 70 of the Constitution."),
        head("Administration", align="center"),
        head("Appointment of Executive Director"),
        para("11. (1) The President shall, in accordance with article 195 of the Constitution, appoint an Executive Director for the Commission."),
    ],
    [
        head("Miscellaneous and general provisions", align="center"),
        head("Interpretation"),
        para("96. In this Act unless the context otherwise requires"),
        para("“assessable processing” means processing of a description specified in an Executive Instrument made by the Minister under section 57 (1);"),
        para("“business” includes trade or profession;"),
        para("“corporate finance service” means a service which consists of"),
        para("(a) underwriting in respect of the issue or the placing of issues of any instrument;"),
    ],
    [
        para("Date of Gazette notification: 18th May, 2012", align="center"),
    ],
]  # fmt: skip


def test_ghanaian_contents_preamble_and_the_body_after_passed_by_parliament():
    text, result = build(GH_843, rules=structure.GH_ACT)
    nodes = by_locator(result)
    assert result.root.heading == "Data Protection Act, 2012"
    # The contents' group headings are its own lines, not entries.
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == [
        "sec_1", "sec_2", "sec_4", "sec_11", "sec_96",
    ]  # fmt: skip
    assert span(text, nodes["preamble"]).endswith("PASSED by Parliament and assented to by the President:")
    # After the enacting words the first group heading opens the body: a leaf of the document, before
    # section 1, not the preamble's.
    assert "Data Protection Commission\n\nEstablishment" not in span(text, nodes["preamble"])
    assert nodes["sec_1"].parent is result.root
    assert span(text, nodes["notice_1"]) == "Date of Gazette notification: 18th May, 2012"
    assert codes(result) == []
    spans_nest(text, result)


def test_ghanaian_headings_above_sections_group_headings_and_definitions():
    text, result = build(GH_843, rules=structure.GH_ACT)
    nodes = by_locator(result)
    assert [nodes[f"sec_{n}"].heading for n in (1, 2, 4, 11, 96)] == [
        "Establishment of Data Protection Commission",
        "Object of the Commission",
        "Governing body of the Commission",
        "Appointment of Executive Director",
        "Interpretation",
    ]
    # An unnumbered group heading is a leaf of the document between sections, held by neither.
    assert "Administration" not in span(text, nodes["sec_4"]) and "Administration" not in span(
        text, nodes["sec_11"]
    )
    # Printed inline, "(2)" is words of subsection (1), as the source sets it.
    assert [n.locator for n in nodes["sec_1"].children] == ["sec_1__subsec_1"]
    assert nodes["sec_4__subsec_1__para_b__subpara_vi"].kind == "subparagraph"
    assert [n.label for n in nodes["sec_96"].children] == [
        "assessable processing", "business", "corporate finance service",
    ]  # fmt: skip


def test_ghanaian_pinpoint_resolves():
    _, result = build(GH_843, rules=structure.GH_ACT)
    assert resolve(result, "s. 4(1)(b)(ii)").locator == "sec_4__subsec_1__para_b__subpara_ii"
    assert resolve(result, "section 96, “business”").locator == "sec_96__def_business"


# --- the table of rules -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "jurisdiction, rules",
    [
        ("aa-eac", "eac-act-1"),
        ("ke", "ke-act-1"),
        ("za", "za-act-1"),
        ("gh", "gh-act-1"),
        ("ng", "ng-federal-act-1"),
    ],
)
def test_each_family_has_its_own_row(jurisdiction, rules):
    assert structure.RULES[(jurisdiction, "act")] == rules
    assert (
        structure.rules_for({"id": f"{jurisdiction}-act-2020-1", "jurisdiction": jurisdiction, "type": "act"})
        == rules
    )


def test_the_families_read_one_another_differently():
    # A Kenyan long title read by Nigeria's rules stays in the contents; Kenya's rules make it the preamble.
    nigeria = publication.build(
        {"id": "ng-x", "jurisdiction": "ng", "type": "act"},
        [{"page": n, "units": u} for n, u in enumerate(KE_DPA, 1)],
    )
    kenya = publication.build(
        {"id": "ke-x", "jurisdiction": "ke", "type": "act"},
        [{"page": n, "units": u} for n, u in enumerate(KE_DPA, 1)],
    )
    assert (
        span(nigeria.serialized, by_locator(nigeria.structure)["contents"]).count("AN ACT of Parliament") == 1
    )
    assert span(kenya.serialized, by_locator(kenya.structure)["preamble"]).startswith("AN ACT of Parliament")
    # South Africa's headings above sections are cross-headings for Nigeria's rules.
    _, za_by_ng = build(ZA_POPIA, rules=structure.NG_FEDERAL_ACT)
    assert by_locator(za_by_ng)["sec_8"].heading == ""
