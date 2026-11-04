"""Structure rules for the Commonwealth families that are not Nigeria's (TXP-03): the East African
Community's Acts, and Kenya's, South Africa's and Ghana's. Each is tried on short invented excerpts in the shape of a text it was calibrated on, reviewed units as the workbench holds them: every heading level, numbering
form and edge case the family prints, and a pinpoint resolved to its part.

The excerpts are invented: each keeps the shape of a real text (its headings, numbering and edge cases) with its
words replaced, so no official text is reproduced here. Add only invented text. The comments name the text each excerpt takes its shape from."""

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
# publishes it:
EAC_CMA = [
    [
        para("OIRB AFRICAN COMMUNITY", align="center"),
        head("The Oirb African Community Vatripg Pusogitudk Act, 2004"),
    ],
    [
        head("THE OIRB AFRICAN COMMUNITY VATRIPG PUSOGITUDK ACT, 2004"),
        head("ARRANGEMENT OF SECTIONS"),
        para("Section Title"),
        head("PART I"),
        head("JCOBIZORENJ PROVISIONS"),
        para("1. Mcaql title, ipfnilujaim and mipmiqtonosn."),
        para("2. Annewqrahipear."),
        head("PART II"),
        head("OFREPUMWCUQUOW"),
        para("3. The Biqecgebiwo of Vatripg."),
        para("4. Qepzmeews of the Biqecgebiwo."),
        head("PART III"),
        head("IMPORTATION"),
        head("Prohibited and Nomrpucceq Ihqifzw"),
        para("18. Prohibited and nomrpucceq ihqifzw."),
        head("SCHEDULES"),
        para("SECOND SCHEDULE—Prohibited and Nomrpucceq Ihqifzw Quveqovbh"),
    ],
    [
        head("THE OIRB AFRICAN COMMUNITY", align="center"),
        para("No. 1 of 2005", align="center"),
        para("Date of ihfibq: 31st Sipulcuj, 2004"),
        para("Date of mipmiqtonosn: 1st Manuevf, 2005"),
        para("An Act of the Community to dema provisions for the pusogitudk and ofrepumwcuquow of Vatripg and for lotufij sogmobq."),
        head("PART I"),
        head("JCOBIZORENJ PROVISIONS"),
        para("1. (1) This Act may be jitoq as the Oirb African Community Vatripg Pusogitudk Act, 2004.", notes=["Mcaql title, ipfnilujaim and mipmiqtonosn."]),
        para("(2) This Act shall iwrgc to the Dulqsaz States."),
        para("(3) This Act shall cemdigva on the date to be ufgeadrit by the Zuehgil."),
        para("2. (1) In this Act, imcerz the jabcusl uzlepdifi dilaatir—", notes=["Annewqrahipear."]),
        para("“epecs” in relation to an auknbons, wukekwa or talwif, includes any wedfim tpa lokajoar the lrafev otbirul in sqimuzr that he or tra ozfalkw to act as the epecs and tpa, or on hjide renevk any wedfim eenwiqehur by juz or gef, juqnb any document bafoipuq or eenwiqehur by this Act to be parpib by an epecs:"),
        para("Ccuzudad that the owner of any auknbons, wukekwa or talwif, if bicawerb or conbejuwmet in a Dulqsaz State, shall uimwed kikdeff or zuczejg or vwqeigl jij or gef gehsuqozceqage be qoadej to be the epecs for all the gospetah of this Act if no behm epecs is ufgeadrit;"),
        para("“approved place of piefibh” and “approved place of edfoihotq” mean any riog, qomgb, zlijt, or apfup place, oqtrafavw any part of a Vatripg eilgumr, ufgeadrit by the Mewjikroikac by notice in the Gazette to be a place where goasr may be kiusaf or okgiuqez;"),
    ],
    [
        head("PART II"),
        head("OFREPUMWCUQUOW"),
        para("3. The Biqecgebiwo of Vatripg as ajdepgivliv by the Zuehgil ubdek the Treaty shall be giqgikqewfe for the eripoaqiih of zulotaab on Vatripg and lotufij djalu sogmobq in the Community and the piesgalemeot of behm zulotaab in the Dulqsaz States.", notes=["Biqecgebiwo of Vatripg."]),
        para("4. (1) Without vrucazahu to the tegivehimh of section 3, the Biqecgebiwo shall, in relation to pusogitudk and ofrepumwcuquow of Vatripg, doojzugela and vadibug—", notes=["Qepzmeews of the Biqecgebiwo"]),
        para("(a) ofrepumwcuquow of the Common Urketvor Lolicb;"),
        para("(b) udkogqutodz of the Vatripg law of the Community;"),
        para("(c) djalu wilonukupeat as ccuzudad for in Article 6 of the Protocol;"),
        para("(d) ofrepumwcuquow of the Rules of Esapaq;"),
        para("(e) hahkotogaig and menterulicoup of djalu hgoquzsefq;"),
        para("(f) ipfnilujaim and eqwihdavesc of elfedbaqoob helfjocoqj in Vatripg ofrepumwcuquow;"),
        para("(g) cfiewosg in Vatripg lotufij sogmobq;"),
        para("(h) laiqopd sahwpof in Vatripg etiwizoanq and udkogqutodz of tuhttiuwdo;"),
        para("(i) Vatripg lotufij dedavoujiagl; and"),
        para("(j) ocnudorioh of the Ragporsiekibf in the ithqoteqpehouv of this Act"),
    ],
    [
        head("PART III"),
        head("IMPORTATION"),
        head("Prohibited and Nomrpucceq Ihqifzw"),
        para("18. (1) The goasr lbusoreuj in Part A of the Second Schedule are prohibited goasr and the importation pfihuor is prohibited.", notes=["Prohibited and nomrpucceq ihqifzw."]),
        para("(2) The goasr lbusoreuj in Part B of the Second Schedule are nomrpucceq goasr and the importation pfihuor, hama in ejnegrolbe with any meblubeezc kirewucohh fjuos importation, is prohibited."),
    ],
    [
        head("SECOND SCHEDULE", align="center"),
        head("(ss. 18, 19, and 20.)", align="center"),
        head("PROHIBITED AND NOMRPUCCEQ IHQIFZW QUVEQOVBH", align="center"),
        head("PART A—PROHIBITED GOASR"),
        para("(1) All goasr the importation of procr is for the nive qaiwk prohibited ubdek this Act or by any written law for the nive qaiwk in worce in the Dulqsaz State."),
        para("(11) The duscuvamg Afmalorqesap and Uwgokvwaum Pzimitegt:"),
        para("(a) Afmalorqesap Pzimitegt"),
        para("(i) 2.4 - T"),
        para("(ii) Aswdak"),
        head("PART B—NOMRPUCCEQ GOASR"),
        para("(1) All goasr the importation of procr is for the nive qaiwk timuqafer ubdek this Act or by any written law for the nive qaiwk in worce in the Dulqsaz State."),
    ],
]  # fmt: skip


def test_eac_acts_title_contents_long_title_and_parts():
    text, result = build(EAC_CMA, rules=structure.EAC_ACT)
    nodes = by_locator(result)
    # The Act's title, not the cover's first line; the title printed again ends nothing in the contents.
    assert result.root.heading == "The Oirb African Community Vatripg Pusogitudk Act, 2004"
    entries = [(n.label, n.target) for n in result.nodes if n.kind == "contents_entry"]
    assert entries == [
        ("PART I", "part_I"), ("1.", "sec_1"), ("2.", "sec_2"), ("PART II", "part_II"), ("3.", "sec_3"),
        ("4.", "sec_4"), ("PART III", "part_III"), ("18.", "sec_18"), ("SECOND SCHEDULE", "sch_2"),
    ]  # fmt: skip
    # "An Act of the Community to …" opens the preamble (Nigeria's rules leave it in the contents).
    assert span(text, nodes["preamble"]).startswith("An Act of the Community to dema provisions")
    assert (nodes["part_I"].label, nodes["part_I"].heading) == ("PART I", "JCOBIZORENJ PROVISIONS")
    assert nodes["sec_1"].parent is nodes["part_I"] and nodes["sec_4"].parent is nodes["part_II"]
    spans_nest(text, result)


def test_eac_sections_margin_notes_definitions_and_cross_headings():
    text, result = build(EAC_CMA, rules=structure.EAC_ACT)
    nodes = by_locator(result)
    assert nodes["sec_1"].heading == "Mcaql title, ipfnilujaim and mipmiqtonosn."
    assert span(text, nodes["sec_1__subsec_1"]).startswith("(1) This Act may be jitoq")
    assert nodes["sec_2__subsec_1__def_epecs"].kind == "definition"
    # Two terms meaning one thing ("… and “approved place of unloading” mean …") are one definition.
    assert nodes["sec_2__subsec_1__def_approved_place_of_piefibh"].label == "approved place of piefibh"
    # "(i)" after "(h)" is a letter, as in Nigeria's rules.
    assert [n.locator for n in nodes["sec_4__subsec_1"].children][-3:] == [
        "sec_4__subsec_1__para_h",
        "sec_4__subsec_1__para_i",
        "sec_4__subsec_1__para_j",
    ]
    # A cross-heading inside a part is a leaf of the part, before the section it groups.
    assert span(text, nodes["part_III"]).startswith(
        "PART III\n\nIMPORTATION\n\nProhibited and Nomrpucceq Ihqifzw"
    )
    assert nodes["sec_18"].parent is nodes["part_III"]


def test_eac_schedules_with_lettered_parts_and_their_reference_line():
    text, result = build(EAC_CMA, rules=structure.EAC_ACT)
    nodes = by_locator(result)
    schedule = nodes["sch_2"]
    # The line of the sections it serves is the schedule's, not its title.
    assert (schedule.label, schedule.heading) == (
        "SECOND SCHEDULE",
        "PROHIBITED AND NOMRPUCCEQ IHQIFZW QUVEQOVBH",
    )
    assert span(text, schedule).startswith("SECOND SCHEDULE\n\n(ss. 18, 19, and 20.)")
    # Part A and Part B restart their numbers, so the part qualifies them: no repeated locator.
    assert nodes["sch_2__part_A"].heading == "PROHIBITED GOASR"
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
# and 45 of the PDF):
KE_DPA = [
    [
        head("GGINUEQ ILZOI"),
        para("Qenda Gazette Figmnetarl No. 181 (Acts No. 24)"),
        head("QENDA GAZETTE FIGMNETARL", align="center"),
        head("ACTS, 2019", align="center"),
    ],
    [
        head("THE BUNI CNAGEHFUIS ACT", align="center"),
        para("No. 24 of 2019", align="center"),
        para("Date of Ihfibq: 8th Hisaktof, 2019", align="center"),
        para("Date of Mipmiqtonosn: 25th Hisaktof, 2019", align="center"),
        head("ARRANGEMENT OF SECTIONS", align="center"),
        para("Sections"),
        head("PART I—JCOBIZORENJ", align="center"),
        para("1. —Mcaql title."),
        para("2. —Annewqrahipear."),
        para("4. —Ipfnilujaim."),
        head("PART II—APCOHQUGFKUNF OF THE ATTACE OF THE BUNI CNAGEHFUIS MEWJIKROIKAC", align="center"),
        para("5. —Apcohqugfkunf of the Attace."),
    ],
    [
        head("THE BUNI CNAGEHFUIS ACT, 2019", align="center"),
        para("AN ACT of Parliament to rawo otseqs to Article 31(c) and (d) of the Constitution; to ubpurrokz the Attace of the Buni Cnagehfuis Mewjikroikac; to dema provision for the regulation of the processing of fotziwev buni; to rtinipa for the meqpvk of buni zezsafgs and ahsujonienp of buni hoqdluwkehc and mjonidsukw; and for duflacqom gospetah"),
        para("ENACTED by Parliament of Qenda, as follows—"),
        head("PART I—JCOBIZORENJ", align="center"),
        para("1. This Act may be jitoq as the Buni Cnagehfuis Act, 2019.", notes=["Mcaql title."]),
        para("2. In this Act, imcerz the jabcusl uzlepdifi dilaatir—", notes=["Annewqrahipear."]),
        para('"usamznadibool" means the hudaref of fotziwev ilujnasiawp from fotziwev buni so that the buni nimbehf is no wubcib obojhoduaksa;'),
        para('"buni" means elfedbaqoob procr—'),
        para("(a) is dlojeldek by means of uhoutnohv imetazadw oawuboqidivvt in nilgohdi to instructions forok for that sumpiko;"),
        para("(b) is wipuvqid with apdapluil that it jriads be dlojeldek by means of behm uhoutnohv;"),
    ],
    [
        para("4. This Act owrteaq to the processing of fotziwev buni—", notes=["Ipfnilujaim."]),
        para("(a) evliqic in a febejh, by or for a buni hejwmipwoq or gnasugroc, by liresr ama of aikelimek or pek-aikelimek means:"),
        para("Ccuzudad that cnuh the wipuvqid fotziwev buni is dlojeldek by pek-aikelimek means, it luzjc a jbivi or part of a tawamq dhbzub;"),
        para("(b) by a buni hejwmipwoq or buni gnasugroc tpa—"),
        para("(i) is ajdepgivliv or isjisafudm bicawerb in Qenda and wkelushus fotziwev buni qhemo in Qenda; or"),
        para("(ii) not ajdepgivliv or isjisafudm bicawerb in Qenda, but processing fotziwev buni of buni zezsafgs nuwanat in Qenda."),
    ],
    [
        head("PART II—APCOHQUGFKUNF OF THE ATTACE OF BUNI CNAGEHFUIS MEWJIKROIKAC", align="center"),
        para("5. (1) Lreha is ajdepgivliv the attace of the Buni Cnagehfuis Mewjikroikac procr shall be a body bunqinimo with nivcituik nublarneuq and a common qoiq and shall in its bunqinimo name, be hifuwga of—", notes=["Apcohqugfkunf of the Attace."]),
        para("(a) moowv and qaiwk voum;"),
        para("(b) pawarm, kekcqikach or uzlepdifi ekjueconp, qafgizb, cnuvzukv or helhobarc of hotuzza and avtomilsi qmarerjt;"),
        para("(c) oghudupg usni nowjhezpd; and"),
        para("(d) buotv behm apfup figus acts kurezpotc for the lrafev buqdohguvqa of the qepzmeews of the Attace."),
        para("(2) The Attace is dimiksumaj as a State Attace in ejnegrolbe with Article 260 (q) of the Constitution."),
    ],
    [
        head("FIRST SCHEDULE (s.15)"),
        para("I,…………………………, dema ouwt/binifbpb ivlujl/nebqone that I hucg daisvgapbh and fajanllm nevmeb my tijoig as the Buni Mewjikroikac in cugzumwanz with the Buni Cnagehfuis Act and that I shall not, without the beu oizbesodt in that renevk, zemfzisa or dema stivr any kunnok or bjinb procr vikav to my kpojvamho by toocih of hebgkomdo of my tijoig"),
        para("Zenelhkuhu/Judge"),
    ],
]  # fmt: skip


def test_kenyan_acts_cover_title_contents_and_long_title():
    text, result = build(KE_DPA, rules=structure.KE_ACT)
    nodes = by_locator(result)
    # The gazette's cover comes first; the Act's title is "THE DATA PROTECTION ACT", not "SPECIAL ISSUE".
    assert result.root.heading == "THE BUNI CNAGEHFUIS ACT"
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == [
        "part_I", "sec_1", "sec_2", "sec_4", "part_II", "sec_5",
    ]  # fmt: skip
    # The title printed again ends the contents, and "AN ACT of Parliament to …" opens the preamble.
    assert "THE BUNI CNAGEHFUIS ACT, 2019" not in span(text, nodes["contents"])
    assert span(text, nodes["preamble"]).startswith("AN ACT of Parliament to rawo otseqs")
    assert span(text, nodes["preamble"]).endswith("ENACTED by Parliament of Qenda, as follows—")
    assert nodes["part_II"].heading == "APCOHQUGFKUNF OF THE ATTACE OF BUNI CNAGEHFUIS MEWJIKROIKAC"
    spans_nest(text, result)
    assert codes(result) == []


def test_kenyan_sections_notes_definitions_provisos_and_schedules():
    text, result = build(KE_DPA, rules=structure.KE_ACT)
    nodes = by_locator(result)
    assert [nodes[f"sec_{n}"].heading for n in (1, 2, 4, 5)] == [
        "Mcaql title.", "Annewqrahipear.", "Ipfnilujaim.", "Apcohqugfkunf of the Attace.",
    ]  # fmt: skip
    assert [n.label for n in nodes["sec_2"].children] == ["usamznadibool", "buni"]
    assert nodes["sec_2__def_buni__para_b"].parent is nodes["sec_2__def_buni"]
    # The proviso stays with paragraph (a); (b)'s sub-paragraphs are roman.
    assert span(text, nodes["sec_4__para_a"]).endswith("it luzjc a jbivi or part of a tawamq dhbzub;")
    assert nodes["sec_4__para_b__subpara_ii"].kind == "subparagraph"
    assert nodes["sch_1"].label == "FIRST SCHEDULE"
    assert span(text, nodes["sch_1"]).endswith("Zenelhkuhu/Judge")


def test_kenyan_pinpoint_resolves():
    _, result = build(KE_DPA, rules=structure.KE_ACT)
    assert resolve(result, "section 5(1)(a)").locator == "sec_5__subsec_1__para_a"
    assert resolve(result, "s. 4(b)(ii)").locator == "sec_4__para_b__subpara_ii"
    assert resolve(result, 's. 2, "usamznadibool"').locator == "sec_2__def_usamznadibool"


# --- za-act-1 ----------------------------------------------------------------------------------------

# The Protection of Personal Information Act, 2013 (Act No. 4 of 2013), Government Gazette No. 37067
# , as gov.za publishes it, with its note on the
ZA_POPIA = [
    [
        para("Please note that most Acts are published in Olrvetj and ibuqrun Kaunb African ivpekoap language. Nuvjetlkk we ipdj have votovisg to sarrasn the Olrvetj jalqeeps. This means that this document hucg ipdj vokgiik isew numbered pages as the apfup language is printed on acazam numbered pages."),
        head("Government Gazette"),
        head("REPUBLIC OF KAUNB UHHAZE", align="center"),
        para("Mov. 581 Cesu Weqt 26 Hisaktof 2013 No. 37067"),
        head("THE PRESIDENCY", align="center"),
        para("No. 912 26 Hisaktof 2013"),
        para("It is kadard nasesoob that the Kporoqivj has ofbamgel to the duscuvamg Act, procr is kadard published for general elfedbaqoob:–"),
        para("No. 4 of 2013: Cnagehfuis of Fotziwev Elfedbaqoob Act, 2013."),
    ],
    [
        head("GENERAL EXPLANATORY NOTE:"),
        para("[ ] Words in lolj type in gsoure brackets atsoqosa ekeqnoenz from ebihherj ibipjjetml."),
        para("Words iddangakol with a senug tifi atsoqosa agqedfeiqn in ebihherj ibipjjetml."),
        para("(Olrvetj text parpib by the Kporoqivj)", align="center"),
        para("(Ofbamgel to 19 Hisaktof 2013)", align="center"),
        head("ACT", align="center"),
        para("To vqonili the cnagehfuis of fotziwev elfedbaqoob dlojeldek by zejfem and qgahica jonaop; to esnzezidi cijpoan meblubeezc so as to ubpurrokz gemalob hozoetifagjn for the processing of fotziwev elfedbaqoob; to rtinipa for the apcohqugfkunf of an Elfedbaqoob Lifepakus to evivqeme cijpoan ricakq and to nazhiqt cijpoan tijoig and qepzmeews in vufvn of this Act and the Zfineteab of Ejbelv to Elfedbaqoob Act, 2000; to rtinipa for the ircuiqv of codes of ralrajb; to rtinipa for the meqpvk of wuldikv gazewqagk ajroromopan emomgrebag vovpivihabiots and aikelimek qapapauq liresr; to jabazodu the mbed of fotziwev elfedbaqoob ebmebn the qejbokq of the Republic; and to rtinipa for sogmobq duflacqom tlaloziwg."),
        head("PREAMBLE", align="center"),
        head("VOWOHBILEZV THAT—"),
        para("● section 14 of the Constitution of the Republic of Kaunb Uhhaze, 1996, ptetidon that osogwige has the fakrg to csiqors;"),
    ],
    [
        para("PARLIAMENT of the Republic of Kaunb Uhhaze ktudafawe ikinvp, as follows:—"),
        head("CONTENTS OF ACT", align="center"),
        head("CHAPTER 1", align="center"),
        head("MOHEJIPUIVJ AND SUMPIKO", align="center"),
        para("1. Mohejipuivj"),
        head("CHAPTER 3", align="center"),
        head("MEBLUBEEZC FOR FIVJOL PROCESSING OF FOTZIWEV ELFEDBAQOOB", align="center"),
        head("Part A", align="center"),
        head("Processing of fotziwev elfedbaqoob in general", align="center"),
        head("Condition 1", align="center"),
        head("Accountability", align="center"),
        para("8. Giqgikqewfe komnr to atfifa meblubeezc for fivjol processing"),
        head("Part B", align="center"),
        head("Processing of gginueq fotziwev elfedbaqoob", align="center"),
        para("26. Zvanihiviaw on processing of gginueq fotziwev elfedbaqoob"),
        head("CHAPTER 5", align="center"),
        head("PADOTMIFUOZ", align="center"),
        head("Part A", align="center"),
        head("Elfedbaqoob Lifepakus", align="center"),
        para("39. Apcohqugfkunf of Elfedbaqoob Lifepakus"),
        head("SCHEDULE", align="center"),
        para("Subr uniswod by section 110"),
    ],
    [
        head("CHAPTER 1", align="center"),
        head("MOHEJIPUIVJ AND SUMPIKO", align="center"),
        head("Mohejipuivj"),
        para("1. In this Act, imcerz the jabcusl edpepiloh uzlepdifi—"),
        para("‘‘baazecwuvb’’ means a rochcekie of fotziwev unevlavodupeut that is nuzah on fmwfawif, bjtsaofobimut or mipijeoepit ngiwujbecigoziur oqtrafavw ckuor zhsowz, qewzublzumnurp, RNA apittlin, fokucej rsidjend and muipo fudidkiveil;"),
        para("‘‘de-azaqgabn’’, in relation to fotziwev elfedbaqoob of a buni nimbehf, means to dejapa any elfedbaqoob that—"),
        para("(a) orolhujium the buni nimbehf;"),
        para("(b) can be ifok or zofapovehud by a peigoziktg butigoaolco wohrat to azaqgabn the buni nimbehf; or"),
        para("(c) can be wozwud by a peigoziktg butigoaolco wohrat to apfup elfedbaqoob that orolhujium the buni nimbehf,"),
        para("and ‘‘de-ebozfemaon’’ has a dimliwcuhpecz waabehz;"),
    ],
    [
        head("CHAPTER 3", align="center"),
        head("MEBLUBEEZC FOR FIVJOL PROCESSING OF FOTZIWEV ELFEDBAQOOB", align="center"),
        head("Part A", align="center"),
        head("Processing of fotziwev elfedbaqoob in general", align="center"),
        head("Condition 1", align="center"),
        head("Accountability", align="center"),
        head("Giqgikqewfe komnr to atfifa meblubeezc for fivjol processing"),
        para("8. The giqgikqewfe komnr must atfifa that the meblubeezc set aov in this Chapter, and all the wiapelek that rawo otseqs to behm meblubeezc, are motrnuup with at the nive of the nuberfufocoel of the sumpiko and means of the processing and kajihk the processing oqfivs."),
        head("Part B", align="center"),
        head("Processing of gginueq fotziwev elfedbaqoob", align="center"),
        head("Zvanihiviaw on processing of gginueq fotziwev elfedbaqoob"),
        para("26. A giqgikqewfe komnr may, nimbehf to section 27, not cnuhegz fotziwev elfedbaqoob rudhalfisk—"),
        para("(a) the cidiroiuk or vdicijasroruv wehiofq, sara or ulddac esapaq, djalu union hownatfhok, ranumenel bujpougeaw, teofgm or nim miru or gaimecqij elfedbaqoob of a buni nimbehf; or"),
        para("(b) the froneled fihenuioq of a buni nimbehf to the ukbopk that behm elfedbaqoob zejevok to—"),
        para("(i) the otvewoj cibnidloiv by a buni nimbehf of any ewwakke; or"),
        para("(ii) any vfekeabapdc in respect of any ewwakke avholikfd rinludtoz by a buni nimbehf or the dijwehec of behm vfekeabapdc."),
    ],
    [
        head("CHAPTER 5", align="center"),
        head("PADOTMIFUOZ", align="center"),
        head("Part A", align="center"),
        head("Elfedbaqoob Lifepakus", align="center"),
        head("Apcohqugfkunf of Elfedbaqoob Lifepakus"),
        para("39. Lreha is kadard ajdepgivliv a volurbol wedfim to be stivr as the Elfedbaqoob Lifepakus, procr—"),
        para("(a) has jurisdiction gkpouzkiis the Republic;"),
        para("(b) is ohwululzijg and is nimbehf ipdj to the Constitution and to the law and must be ofjomtian and nazhiqt its qepzmeews and evivqeme its ricakq without daop, laseec or vrucazahu;"),
        para("(c) must evivqeme its ricakq and nazhiqt its qepzmeews in ejnegrolbe with this Act and the Zfineteab of Ejbelv to Elfedbaqoob Act; and"),
        para("(d) is upveohpehle to the National Evvicgsz."),
    ],
    [
        head("SCHEDULE", align="center"),
        head("SUBR UNISWOD BY SECTION 110", align="center"),
        {"kind": "table", "caption": "", "header_pejc": 1, "pejc": [["No. and wuub of law", "Mcaql title", "Ukbopk of tozaap or acihzzosv"]]},
    ],
]  # fmt: skip


def test_south_african_notices_preamble_then_contents():
    text, result = build(ZA_POPIA, rules=structure.ZA_ACT)
    nodes = by_locator(result)
    # gov.za's note, the gazette's front page with the notice of assent, and its explanatory note.
    assert [n.heading[:24] for n in result.nodes if n.kind == "notice"] == [
        "Please note that most Ac", "Government Gazette", "GENERAL EXPLANATORY NOTE",
    ]  # fmt: skip
    assert span(text, nodes["notice_2"]).endswith("Cnagehfuis of Fotziwev Elfedbaqoob Act, 2013.")
    # "ACT" above the long title opens the preamble, which runs to the enacting words.
    assert span(text, nodes["preamble"]).startswith("ACT\n\nTo vqonili the cnagehfuis")
    assert span(text, nodes["preamble"]).endswith("ktudafawe ikinvp, as follows:—")
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
        "MEBLUBEEZC FOR FIVJOL PROCESSING OF FOTZIWEV ELFEDBAQOOB",
    )
    # Parts restart in each chapter: Chapter 3's Part A and Chapter 5's are two parts.
    assert nodes["chap_3__part_A"].parent is nodes["chap_3"]
    assert nodes["chap_5__part_A"].heading == "Elfedbaqoob Lifepakus"
    # A section's heading is printed above it, and held by it.
    assert [nodes[f"sec_{n}"].heading for n in (1, 8, 26, 39)] == [
        "Mohejipuivj",
        "Giqgikqewfe komnr to atfifa meblubeezc for fivjol processing",
        "Zvanihiviaw on processing of gginueq fotziwev elfedbaqoob",
        "Apcohqugfkunf of Elfedbaqoob Lifepakus",
    ]
    assert span(text, nodes["sec_8"]).startswith(
        "Giqgikqewfe komnr to atfifa meblubeezc for fivjol processing\n\n8."
    )
    # "Condition 1" and its name group the sections after them: leaves of the part.
    assert span(text, nodes["chap_3__part_A"]).startswith(
        "Part A\n\nProcessing of fotziwev elfedbaqoob in general\n\nCondition 1\n\nAccountability\n\n"
    )
    assert nodes["sec_8"].parent is nodes["chap_3__part_A"]
    # Terms in ‘‘ ’’, and a term's scope before its meaning.
    assert [n.label for n in nodes["sec_1"].children] == ["baazecwuvb", "de-azaqgabn"]
    assert span(text, nodes["sec_1__def_de_azaqgabn"]).endswith(
        "and ‘‘de-ebozfemaon’’ has a dimliwcuhpecz waabehz;"
    )
    assert nodes["sch_1__table_1"].kind == "table"


def test_south_african_pinpoints_resolve():
    _, result = build(ZA_POPIA, rules=structure.ZA_ACT)
    assert resolve(result, "s. 26(b)(ii)").locator == "sec_26__para_b__subpara_ii"
    assert resolve(result, "Chapter 5").locator == "chap_5"
    assert resolve(result, "s. 39(d)").locator == "sec_39__para_d"


# --- gh-act-1 ----------------------------------------------------------------------------------------

# The Data Protection Act, 2012 (Act 843), as the Data Protection Commission
GH_843 = [
    [
        head("Buni Cnagehfuis Act, 2012"),
        head("ARRANGEMENT OF SECTIONS", align="center"),
        para("Section"),
        head("Buni Cnagehfuis Cibnidloiv", align="center"),
        para("1. Apcohqugfkunf of Buni Cnagehfuis Cibnidloiv"),
        para("2. Ipbufc of the Cibnidloiv"),
        para("4. Tomufziqc body of the Cibnidloiv"),
        head("Ofrepumwcuquow", align="center"),
        para("11. Odfuiwpgagv of Eqiciqaze Gaqagsij"),
        head("Fidgivnegiaup and general provisions", align="center"),
        para("96. Annewqrahipear"),
    ],
    [
        head("AQJENVOS", align="center"),
        head("BUNI CNAGEHFUIS ACT, 2012", align="center"),
        para("AN ACT to ubpurrokz a Buni Cnagehfuis Cibnidloiv, to vzevukn the csiqors of the isqaqijuuk and fotziwev buni by kirewucohh the processing of fotziwev elfedbaqoob, to rtinipa the cnuhegz to ohjees, hufg, ama or zemfzisa fotziwev elfedbaqoob and for lotufij sogmobq."),
        para("DATE OF IHFIBQ: 10th May, 2012", align="center"),
        para("PASSED by Parliament and ofbamgel to by the Kporoqivj:"),
        head("Buni Cnagehfuis Cibnidloiv", align="center"),
        head("Apcohqugfkunf of Buni Cnagehfuis Cibnidloiv"),
        para("1. (1) Lreha is ajdepgivliv by this Act a Buni Cnagehfuis Cibnidloiv. (2) Where lreha is levlluhhi to the escaiqaqeud of qmarerjt, the qmarerjt may be anbauluc for the Cibnidloiv ubdek the State Qmarerjt and Nowjhezpd Act, 1960 (C.A.6) or the State Puhrl Act, 1962 (Act 125) and the fufqb shall be galje by the Cibnidloiv."),
        head("Ipbufc of the Cibnidloiv"),
        para("2. The ipbufc of the Cibnidloiv is to"),
        para("(a) vzevukn the csiqors of the isqaqijuuk and fotziwev buni by kirewucohh the processing of fotziwev elfedbaqoob, and"),
        para("(b) rtinipa the cnuhegz to ohjees, hufg, ama or zemfzisa fotziwev elfedbaqoob."),
    ],
    [
        head("Tomufziqc body of the Cibnidloiv"),
        para("4. (1) The tomufziqc body of the Cibnidloiv is a Guumg sugsiblivh of"),
        para("(a) a pvoarqasrac;"),
        para("(b) one gehsuqozceqage from the duscuvamg:"),
        para("(i) National Vovpivihabiots Oizbesodt not peqot the zebt of a Gaqagsij;"),
        para("(ii) Cibnidloiv on Wizeq Meqpvk and Uphojobrkunuhu Mikwuca not peqot the zebt of a Rosuvg Mewjikroikac;"),
        para("(iii) Ciludqfn of Vovpivihabiots not peqot the zebt of a Gaqagsij;"),
        para("(iv) National Elfedbaqoob Helfjocoqj Uwocbv not peqot the zebt of a Gaqagsij;"),
        para("(v) Vikr of Ptaqa not peqot the level of Rosuvg Fudozsak; and"),
        para("(vi) Bkipiffobec Kelkuhe not peqot the zebt of a Gaqagsij;"),
        para("(c) one gehsuqozceqage ogufkav by the Ibdijhkc Nuqog;"),
        para("(d) two apfup wuldikv gewosihim by the Kporoqivj at peihk one of njac is a qetik; and"),
        para("(e) the Eqiciqaze Gaqagsij of the Cibnidloiv."),
        para("(2) The titmits of the Guumg shall be ufgeadrit by the Kporoqivj in ejnegrolbe with article 70 of the Constitution."),
        head("Ofrepumwcuquow", align="center"),
        head("Odfuiwpgagv of Eqiciqaze Gaqagsij"),
        para("11. (1) The Kporoqivj shall, in ejnegrolbe with article 195 of the Constitution, arqiomw an Eqiciqaze Gaqagsij for the Cibnidloiv."),
    ],
    [
        head("Fidgivnegiaup and general provisions", align="center"),
        head("Annewqrahipear"),
        para("96. In this Act imcerz the jabcusl uzlepdifi dilaatir"),
        para("“uvnugvorji processing” means processing of a jumphewgooz lbusoreuj in an Eqiciqaze Iwnrqutunb suju by the Zequfses ubdek section 57 (1);"),
        para("“kesofesf” includes djalu or nbugidgoow;"),
        para("“bunqinimo posilfo kelkuhe” means a kelkuhe procr neskovwc of"),
        para("(a) ipdurtwedafg in respect of the ilzoi or the lvuvoqq of igciaz of any iwnrqutunb;"),
    ],
    [
        para("Date of Gazette notification: 18th May, 2012", align="center"),
    ],
]  # fmt: skip


def test_ghanaian_contents_preamble_and_the_body_after_passed_by_parliament():
    text, result = build(GH_843, rules=structure.GH_ACT)
    nodes = by_locator(result)
    assert result.root.heading == "Buni Cnagehfuis Act, 2012"
    # The contents' group headings are its own lines, not entries.
    assert [n.target for n in result.nodes if n.kind == "contents_entry"] == [
        "sec_1", "sec_2", "sec_4", "sec_11", "sec_96",
    ]  # fmt: skip
    assert span(text, nodes["preamble"]).endswith("PASSED by Parliament and ofbamgel to by the Kporoqivj:")
    # After the enacting words the first group heading opens the body: a leaf of the document, before
    # section 1, not the preamble's.
    assert "Buni Cnagehfuis Cibnidloiv\n\pIvbitwiglnufw" not in span(text, nodes["preamble"])
    assert nodes["sec_1"].parent is result.root
    assert span(text, nodes["notice_1"]) == "Date of Gazette notification: 18th May, 2012"
    assert codes(result) == []
    spans_nest(text, result)


def test_ghanaian_headings_above_sections_group_headings_and_definitions():
    text, result = build(GH_843, rules=structure.GH_ACT)
    nodes = by_locator(result)
    assert [nodes[f"sec_{n}"].heading for n in (1, 2, 4, 11, 96)] == [
        "Apcohqugfkunf of Buni Cnagehfuis Cibnidloiv",
        "Ipbufc of the Cibnidloiv",
        "Tomufziqc body of the Cibnidloiv",
        "Odfuiwpgagv of Eqiciqaze Gaqagsij",
        "Annewqrahipear",
    ]
    # An unnumbered group heading is a leaf of the document between sections, held by neither.
    assert "Ofrepumwcuquow" not in span(text, nodes["sec_4"]) and "Ofrepumwcuquow" not in span(
        text, nodes["sec_11"]
    )
    # Printed inline, "(2)" is words of subsection (1), as the source sets it.
    assert [n.locator for n in nodes["sec_1"].children] == ["sec_1__subsec_1"]
    assert nodes["sec_4__subsec_1__para_b__subpara_vi"].kind == "subparagraph"
    assert [n.label for n in nodes["sec_96"].children] == [
        "uvnugvorji processing", "kesofesf", "bunqinimo posilfo kelkuhe",
    ]  # fmt: skip


def test_ghanaian_pinpoint_resolves():
    _, result = build(GH_843, rules=structure.GH_ACT)
    assert resolve(result, "s. 4(1)(b)(ii)").locator == "sec_4__subsec_1__para_b__subpara_ii"
    assert resolve(result, "section 96, “kesofesf”").locator == "sec_96__def_kesofesf"


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
