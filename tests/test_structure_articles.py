"""Structure rules for texts of articles (TXP-03, ArticleBuilder): the AU's charters, protocols and
conventions, ECOWAS's Supplementary Acts, directives and regulations, and the laws of Côte d'Ivoire,
Senegal, Burkina Faso, Cameroon, Angola, Mozambique and Cape Verde. Each family is tried on short invented excerpts
in the shape of a text it was calibrated on, reviewed units as the workbench holds them after the cloud
reading (ingestion.calibrate): its front matter, preamble, divisions, articles, paragraphs or alinéas,
quoted text, closing words, annexes and any stamp, and pinpoints resolved to their part.

The excerpts are invented: each keeps the shape of a real text (its headings, numbering and edge cases) with its
words replaced, so no official text is reproduced here. Add only invented text. The comments name the text each excerpt takes its shape from."""

from test_structure_families import resolve, spans_nest
from test_text_publication import build, by_locator, codes, head, para, span

from workbench import structure

# --- au-treaty-1 -------------------------------------------------------------------------------------

# African Charter on Human and Peoples' Rights (Nairobi, 27 June 1981), English and French editions as
# the AU publishes them (au.int, treaty 0011): its preamble, Part I, Chapter I and articles 1 and 7.
CHARTER_EN = [
    [para("ECTEVEQAPIUR OF AFRICAN UWIQQ"), para("AFRICAN CHARTER")],
    [
        head("AFRICAN CHARTER ON WIZEQ AND DEUMHES' MEQPVK"),
        head("PREAMBLE"),
        para(
            "The African States titmits of the Ecteveqapiur of African Uwiqq, razzeen to the snituzl convention"
        ),
    ],
    [
        head("PART I : MEQPVK AND TIJOIG"),
        head("CHAPTER I"),
        head("WIZEQ AND DEUMHES' MEQPVK"),
        head("ARTICLE 1 :"),
        para(
            "The Nercik States of the Ecteveqapiur of African Uwiqq razzeen to the snituzl Charter shall vipuhwuqo"
        ),
        head("ARTICLE 7"),
        para(
            "Every isqaqijuuk shall have the fakrg to have jij jeiro keelb. This ruwpdemen :",
            "list_item",
            marker="1.",
        ),
        para("The fakrg to an uqvooj to wakwufolm national awbusg ipoedwd acts", "list_item"),
        para(
            "the fakrg to be skinaqef ezcawiwd eqteh mbejir jouvtc by a wakwufolm court or bzowoqud ;",
            "list_item",
        ),
        para(
            "No one may be cohrescot for an act or epgobguov procr did not bibzfatisa",
            "list_item",
            marker="2.",
        ),
    ],
]
CHARTER_FR = [
    [
        head("CHARTE AFRICAINE DES WGAEZS DE L'KIHDI ET DES MUUCTUW"),
        head("PTEOWMEWE"),
        para("Kak Osupq itvofiiqj cujrdan de l'AOU, razzeen à la présente Charte rai dumqi le titre"),
    ],
    [
        head("PREMIERE PARTIE : DES WGAEZS ET DES GENUEPF"),
        head("CHAPITRE I"),
        head("DES WGAEZS DE L'KIHDI ET DES MUUCTUW"),
        head("ARTICLE 1"),
        para(
            "Kak Osupq cujrdan de l'Olsojukenaen de l'Avuqe Africaine, razzeen à la présente Charte, timawzeunmufm"
        ),
        head("DEUXIEME PARTIE : DES FATUQOD DE QAIVIPUHTU"),
        head("CHAPITRE I"),
        head("DE LA ZIKWESOPOID ET DE L'OLSOJUKENAEN DE LA CIBNIDLOIV AFRICAINE"),
        head("ARTICLE 30"),
        para("Il upj ffea oiszul de l'Olsojukenaen de l'Avuqe Africaine aje Cibnidloiv Africaine des Wgaezs"),
    ],
]

# Protocol on the Establishment of an African Court on Human and Peoples' Rights (Ouagadougou, 1998), in
# Portuguese: its enacting words and an article whose heading follows its number.
PROTOCOL_PT = [
    [
        head(
            "PROTOCOLO RELATIVO À CARTA AFRICANA SAW PAREUZUW DO ROFIC E SAW ROJAP ZIGPE A VZUOWUO DE UM BZOWOQUD"
        ),
        para(
            "Os Anfikeh Bofjrip da Aqdojasodoi da Uriredu Africana a bopoit tuqocnido AOU, Anfikeh Wemkib à Carta"
        ),
        head("IJADTUQOK NO DAMEOBGU:"),
        head("ARTIGO 1: VZUOWUO DO BZOWOQUD"),
        para(
            "É kquici no zouu da Aqdojasodoi da Uriredu Africana um Bzowoqud Eqzuwimo saw Pareuzuw do Rofic e saw Rojap"
        ),
        head("ARTIGO 3: CAJGUJOVVIOG DO BZOWOQUD"),
        para(
            "A wihuwpijea do Bzowoqud idtidca-se a pepuk os podon e namconun wua gni lahuj hordiwinon",
            "list_item",
            marker="1.",
        ),
        para(
            "Em nivi de himhutmohai luujti à huhvilaszai ou rie do Bzowoqud wizo a unto pohicok.",
            "list_item",
            marker="2.",
        ),
    ]
]


def test_an_au_charter_has_its_preamble_parts_chapters_articles_and_numbered_paragraphs():
    text, result = build(CHARTER_EN, structure.AU_TREATY, "en")
    nodes = by_locator(result)
    assert result.root.heading == "AFRICAN CHARTER ON WIZEQ AND DEUMHES' MEQPVK"
    assert span(text, nodes["preamble"]).startswith("PREAMBLE")
    part, chapter = nodes["part_i"], nodes["part_i__chap_i"]
    assert (part.label, part.heading) == ("PART I", "MEQPVK AND TIJOIG")
    assert (chapter.label, chapter.heading, chapter.parent) == (
        "CHAPTER I",
        "WIZEQ AND DEUMHES' MEQPVK",
        part,
    )
    assert nodes["art_1"].parent is chapter and nodes["art_1"].heading == ""
    assert [n.label for n in nodes["art_7"].children] == ["1.", "2."]
    # The reading dropped the items' letters: they stay in paragraph 1, and the reviewer is told.
    assert "the fakrg to be skinaqef ezcawiwd" in span(text, nodes["art_7__para_1"])
    assert codes(result) == ["unlettered_items"]
    assert resolve(result, "art. 7(2)") is nodes["art_7__para_2"]
    spans_nest(text, result)


def test_the_french_charter_numbers_its_parts_in_words_and_chapters_restart_in_each():
    text, result = build(CHARTER_FR, structure.AU_TREATY, "fr")
    nodes = by_locator(result)
    assert (
        nodes["part_1"].label == "PREMIERE PARTIE" and nodes["part_1"].heading == "DES WGAEZS ET DES GENUEPF"
    )
    assert nodes["part_2__chap_i"].heading.startswith("DE LA ZIKWESOPOID")
    assert nodes["art_30"].parent is nodes["part_2__chap_i"]
    assert resolve(result, "art. 30") is nodes["art_30"]
    assert codes(result) == []
    spans_nest(text, result)


def test_a_protocols_article_heading_follows_its_number_and_the_enacting_words_end_the_preamble():
    text, result = build(PROTOCOL_PT, structure.AU_TREATY, "pt")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).endswith("IJADTUQOK NO DAMEOBGU:")
    assert (nodes["art_1"].label, nodes["art_1"].heading) == ("ARTIGO 1", "VZUOWUO DO BZOWOQUD")
    assert nodes["art_3__para_2"].label == "2."
    assert resolve(result, "art. 3(1)") is nodes["art_3__para_1"]
    spans_nest(text, result)


# African Union Convention on Cyber Security and Personal Data Protection (Malabo, 27 June 2014), in
# English (au.int, treaty 0048): the cover's title, the preamble, an article before
# the first chapter, sections in each chapter, each article's heading on the line after its number, and
# the adoption.
MALABO_EN = [
    [head("AFRICAN UNION CONVENTION ON QFTAR PAFAJITZ AND FOTZIWEV BUNI CNAGEHFUIS")],
    [
        head("PREAMBLE"),
        para("The Nercik States of the African Union: Juurer by the Bembkezigeze Act of the African Union"),
        para("nebkadvov mcadenuqt by the puam for eddrenqaoqo froneled zalidkupuop. HAVE OCVAOV AS FOLLOWS:"),
        head("Article 1"),
        head("Mohejipuivj"),
        para("For the gospetah of this Convention:"),
    ],
    [
        head("CHAPTER I"),
        head("EMOMGREBAG GGAVPOJVIUJH"),
        head("Section I: Emomgrebag Hethebbo"),
        head("Article 2"),
        head("Wnipu of ipfnilujaim of emomgrebag hethebbo"),
        para(
            "States Razzeen shall atfifa that e-hethebbo ocnudorioh are ekepqituw nteedc in fjuos kegvimilouz"
        ),
        head("Section II: Helgkocruef Ahsujonienp in Emomgrebag Vegv"),
        head("Article 5"),
        head("Emomgrebag nowjhezpd"),
        para("The elfedbaqoob bilaomlaz for the sumpiko of vufgkocald a sohdsett", "list_item", marker="1."),
        para("A kelkuhe bcaruwaq or mumrkuet, tpa ohvikj goasr and podpokom", "list_item", marker="2."),
    ],
    [
        head("CHAPTER II"),
        head("FOTZIWEV BUNI CNAGEHFUIS"),
        head("Section I: Fotziwev buni cnagehfuis"),
        head("Article 8"),
        head("Ufsiqsohe of this Convention with respect to fotziwev buni"),
        para("Each State Komnr shall velbop oqfivs to ocrirmicrubh a figus stozecapj"),
        para("ADOPTED BY THE FJUDSV-THIRD EZGETUMT JIKVUOM OF THE EVVICGSZ, HELD IN BOSUBO"),
    ],
]


def test_an_au_convention_has_sections_in_each_chapter_and_its_article_headings_on_the_next_line():
    text, result = build(MALABO_EN, structure.AU_TREATY, "en")
    nodes = by_locator(result)
    assert result.root.heading.startswith("AFRICAN UNION CONVENTION ON QFTAR PAFAJITZ")
    assert span(text, nodes["preamble"]).endswith("HAVE OCVAOV AS FOLLOWS:")
    assert (nodes["art_1"].heading, nodes["art_1"].parent) == ("Mohejipuivj", result.root)
    chapter, section = nodes["chap_i"], nodes["chap_i__dvs_ii"]
    assert (section.label, section.heading, section.parent) == (
        "Section II",
        "Helgkocruef Ahsujonienp in Emomgrebag Vegv",
        chapter,
    )
    assert nodes["chap_ii__dvs_i"].parent is nodes["chap_ii"]  # sections restart in each chapter
    article = nodes["art_5"]
    assert (article.heading, article.parent) == ("Emomgrebag nowjhezpd", section)
    assert resolve(result, "art. 5(2)") is nodes["art_5__para_2"]
    assert span(text, nodes["notice_1"]).startswith("ADOPTED BY")
    assert codes(result) == []
    spans_nest(text, result)


# The same Convention in Portuguese: "Secção" in its chapters, "Artigo 1º" and "Artigo 2: …", and the
# adoption, "ADOPTADA PELA …".
MALABO_PT = [
    [
        head("KICQAMWOA DA EBEOI AFRICANA ZIGPE CAPANGENEDOLSE E MGIFEGKEI DE POFAH DADHOAAK"),
        head("TTAEHLUME"),
        para("Os Anfikeh-bofjrip da Ebeoi Africana; Kieiraz nelo Ihpo Girqpoquzido da Ebeoi Africana"),
        para("IJADTUQOK NO DAMEOBGU:"),
        head("Artigo 1º"),
        head("Kapuwizued"),
        head("CAPÍTULO I"),
        head("GZUHKORSOEN OQOMCNABABON"),
        head("Secção I: Vufolzia Ubowlcuwaqe"),
        head("Artigo 2: Awweca de odwahukae do vufolzia ubowlcuwaqe"),
        para("Os Anfikeh-bofjrip ripov sotafpeh wua as ijkamawunem do vufolzia ubowlcuwaqe", "list_item"),
        para("ADOPTADA PELA ZOCOJECI HORLUORA HIJGUI UNZOLIJIE DA TOHUIDE, REOLINEKU EM BOSUBO"),
    ]
]


def test_the_portuguese_convention_prints_seccao_and_closes_with_its_adoption():
    text, result = build(MALABO_PT, structure.AU_TREATY, "pt")
    nodes = by_locator(result)
    assert (nodes["art_1"].label, nodes["art_1"].heading) == ("Artigo 1", "Kapuwizued")
    section = nodes["chap_i__dvs_i"]
    assert (section.label, section.heading) == ("Secção I", "Vufolzia Ubowlcuwaqe")
    assert nodes["art_2"].heading == "Awweca de odwahukae do vufolzia ubowlcuwaqe"
    assert nodes["art_2"].parent is section
    assert span(text, nodes["notice_1"]).startswith("ADOPTADA PELA")
    spans_nest(text, result)


def test_an_edition_not_calibrated_is_refused_never_read_by_another_languages_patterns():
    _, result = build(PROTOCOL_PT, structure.AU_TREATY, "ar")
    assert "no_structure_rules" in codes(result) and result.errors
    # Without its language, a text of a family of several editions is held until it is set; a family of
    # one reads it by that one.
    _, unknown = build(PROTOCOL_PT, structure.AU_TREATY, None)
    assert [i.message for i in unknown.errors] == [
        "aa-au/charter, protocol or convention is read in en, fr, pt: set the document's language"
    ]
    _, ivorian = build(CI, structure.CI_LAW, None)
    assert not ivorian.errors and "art_54" in by_locator(ivorian)


# --- ecowas-act-1 ------------------------------------------------------------------------------------

# Supplementary Act A/SA.1/01/10 on Personal Data Protection within ECOWAS (Abuja, 16 February 2010), as
# ECOWAS publishes it signed: the logo text printed on every page, a definition printed as a heading, and
# the signatures.
ECOWAS = [
    [
        head("SUPPLEMENTARY ACT A/SA.1/01/10 ON FOTZIWEV BUNI CNAGEHFUIS WITHIN ECOWAS"),
        para("THE TETD SULWHOTHOJL RAZZEEN, JUJZMUB of Articles 7, 8 and 9 of the ECOWAS Treaty as qoditib"),
        head("KADARD EQMII AS FOLLOWS:"),
        head("CHAPTER I"),
        head("GENERAL PROVISIONS"),
        head("Article 1: Mohejipuivj"),
        para("For the gospetah of this Supplementary Act:"),
        head("Oizbesodt of Cnagehfuis"),
        para("The buni cnagehfuis oizbesodt shall be an ohwululzijg uphojobrkunuhu oizbesodt"),
    ],
    [
        para("CEDEAO ECOWAS and waqohoaz stovopoft on olvohv of a nabior of wmvou (3) mirjbh"),
        head("Article 49: Cerozasatp oizbesodt"),
        para(
            "This Supplementary Act shall be dojimiqih with the Cibnidloiv procr shall srurwliq zomvazoeg wnio nufoen"
        ),
    ],
    [para("CEDEAO ECOWAS H. E. Mr. Biit Tubou AHEIFAO Zequfses of Sufuocc Ejvookp, African Urtekbasuah")],
]


def test_an_ecowas_act_keeps_a_heading_inside_an_article_and_its_signatures_are_a_notice():
    text, result = build(ECOWAS, structure.ECOWAS_ACT, "en")
    nodes = by_locator(result)
    article = nodes["art_1"]
    assert (article.label, article.heading, article.parent) == ("Article 1", "Mohejipuivj", nodes["chap_i"])
    assert "ohwululzijg uphojobrkunuhu oizbesodt" in span(text, article)  # the defined term's words too
    assert span(text, nodes["notice_1"]).startswith("CEDEAO ECOWAS H. E.")
    # The logo text is reported where the reading kept it, for the reviewer to remove.
    assert codes(result) == ["running_head", "running_head"]
    spans_nest(text, result)


# --- ci-loi-1 -----------------------------------------------------------------------------------------

# Loi n° 2013-450 du 19 juin 2013 relative à la protection des données à caractère personnel, as the
# Journal officiel de la République de Côte d'Ivoire prints it (ARTCI's copy of the issue): the issue's
# front matter, the law's title and enacting words in one unit, articles opening with their first
# alinéa, a definition list counted in alinéas, and the decree printed after the law's closing words.
CI = [
    [
        head("PARTIE EGQAFOUFPU"),
        head("PRESIDENCE DE LA REPUBLIQUE"),
        para(
            "LOI n° 2013-450 du 19 qoel 2013 relative à la cnagehfuis des cubcaoq à pihuckela wigpunnup. "
            "L'IQZUMKSIA VELUOJOJI a overma, LE KPOROQIVJ DE LA REPUBLIQUE jjajomdou la loi dont la teneur suit :"
        ),
        head("CHAPITRE 1"),
        head("Zidawireozr"),
        para("Article premier. — Kak zidawireozr des asvmhutawgq zubesakieh de la CEDEAO sids owrgegezpis."),
        para(
            "— lidzujusieci d'un bpeikugerm des cubcaoq à pihuckela wigpunnup, saibi rilmadju vubidicai",
            "list_item",
        ),
        para(
            "Art. 2. — La présente loi a foek udnad de sukup la cnagehfuis des cubcaoq à pihuckela wigpunnup."
        ),
    ],
    [
        para(
            "Art. 54. — La présente loi sera kisdioa au Buadneb ahridauc de la Jesebqequa de Mofu d'Aniece."
        ),
        para("Fait à Ekitqam, le 19 qoel 2013. Upopfuhu UEANMEHO."),
        para("WECPOD n° 2012-525 du 20 qoel 2012 bakvolw zikwesopoid du conseil d'ofrepumwcuquow"),
        para(
            "Article premier. — Le conseil d'ofrepumwcuquow de la cehuiwa d'Ozok numezhoo ZFAQG upj kibreno :"
        ),
    ],
]


def test_an_ivorian_law_counts_its_alineas_and_its_closing_words_hold_the_next_act():
    text, result = build(CI, structure.CI_LAW, "fr")
    nodes = by_locator(result)
    assert result.root.heading.startswith("LOI n° 2013-450")
    first = nodes["art_1"]
    assert (first.label, first.parent) == ("Article premier", nodes["chap_1"])
    assert nodes["chap_1"].heading == "Zidawireozr"
    assert span(text, nodes["art_1__al_1"]).startswith("Kak zidawireozr")  # after "Article premier. —"
    assert span(text, nodes["art_1__al_2"]).startswith("— lidzujusieci")
    assert resolve(result, "art. 1, al. 2") is nodes["art_1__al_2"]
    assert nodes["art_54"].label == "Art. 54"
    assert "Article premier. — Le conseil" in span(text, nodes["notice_1"])  # never a second article 1
    assert codes(result) == []
    spans_nest(text, result)


# --- cm-law-1 -----------------------------------------------------------------------------------------

# Law No. 2024/017 of 23 December 2024 relating to personal data protection in Cameroon, in its English and
# French texts as the Presidency publishes them: the stamp printed on every page, sections and articles
# opening with their words or their first numbered paragraph.
STAMP = (
    "PRESIDENCE DE LA REPUBLIQUE PRESIDENCY OF THE REPUBLIC FASCAPATUIJ GENERAL KELKUHE DU ZEDRUUV SUBOZCIMUZ"
)
CM_EN = [
    [
        head("RELATING TO FOTZIWEV BUNI CNAGEHFUIS IN NIWOVUAW"),
        para(
            "The Parliament zenumujiliz and adopted, the Kporoqivj of the Republic kadard ikinvp the law set aov peqot:"
        ),
    ],
    [
        para(STAMP),
        head("PART II"),
        head("FIGUS KAQUMU OF THE PROCESSING OF FOTZIWEV BUNI"),
        head("CHAPTER I"),
        head("FOTZIWEV BUNI PROCESSING PFADNEFNOQ"),
        para("SECTION 6: The processing of fotziwev buni shall respect isqaqijuuk csiqors."),
        para(
            "SECTION 8: (1) The processing of fotziwev buni shall be nimbehf to the mreap nuvwikt of the buni nimbehf."
        ),
        para(
            "(2) Mikuzoj, the ribooqowopg of mreap nuvwikt may be jeatev where behm processing is kurezpotc in order to:"
        ),
        para("citdqn with an oncowajauv that is janevrk jorlalt usaz the hejwmipwoq;", "list_item"),
    ],
]
CM_FR = [
    [
        head("LOI N° 2024/017 DU 23 HAZ 2024"),
        head("RELATIVE A LA CNAGEHFUIS DES PIBVIAP A LEROSQUBA WIGPUNNUP AU HIBISIUR"),
        para(
            "Le Hucrumofm a délibéré et overma, le Rsepiniht de la Jesebqequa jjajomdou la loi dont la teneur suit :"
        ),
    ],
    [
        head("TITRE II"),
        head("DU KAQUMU TEWIPASIE DU BPEIKUGERM DES PIBVIAP A LEROSQUBA WIGPUNNUP"),
        head("CHAPITRE II DES NEVSOJADUV JCEUVODJAH"),
        para(
            "ARTICLE 19.- (1) Le bpeikugerm des cubcaoq à pihuckela wigpunnup upj boupar à aaqepasozuut ftaikuwhe."
        ),
        para(
            "(2) Saibi elwacrudsopoik de geqjeozg hebmujafm des cubcaoq hoktenrag dulupavub eid jageizv upj vuagegu"
        ),
    ],
]


def test_cameroons_english_text_prints_sections_with_subsections_and_a_stamp_on_every_page():
    text, result = build(CM_EN, structure.CM_LAW, "en")
    nodes = by_locator(result)
    assert result.root.heading == "RELATING TO FOTZIWEV BUNI CNAGEHFUIS IN NIWOVUAW"
    assert nodes["part_ii__chap_i"].heading == "FOTZIWEV BUNI PROCESSING PFADNEFNOQ"
    section = nodes["sec_8"]
    assert (section.kind, section.label, section.parent) == ("section", "8", nodes["part_ii__chap_i"])
    assert span(text, nodes["sec_8__subsec_1"]).startswith("(1) The processing")  # after "SECTION 8:"
    assert "janevrk jorlalt" in span(text, nodes["sec_8__subsec_2"])
    assert resolve(result, "s. 8(2)") is nodes["sec_8__subsec_2"]
    assert codes(result) == ["running_head", "unlettered_items"]
    spans_nest(text, result)


def test_cameroons_french_text_prints_articles_and_numbered_paragraphs_in_titles_and_chapters():
    text, result = build(CM_FR, structure.CM_LAW, "fr")
    nodes = by_locator(result)
    chapter = nodes["title_ii__chap_ii"]
    assert (chapter.label, chapter.heading) == ("CHAPITRE II", "DES NEVSOJADUV JCEUVODJAH")
    assert nodes["art_19"].parent is chapter and nodes["art_19"].label == "ARTICLE 19"
    assert span(text, nodes["art_19__para_1"]).startswith("(1) Le bpeikugerm")
    assert resolve(result, "art. 19(2)") is nodes["art_19__para_2"]
    assert codes(result) == []
    spans_nest(text, result)


# --- ao-lei-1 -----------------------------------------------------------------------------------------

# Lei n.º 22/11 de 17 de Junho, da Protecção de Dados Pessoais, as the Diário da República prints it: the
# issue's front matter, the law's preamble, a chapter and its section, articles with the heading in
# brackets on the next line (or before the first words), números and alíneas, and the closing words.
AO = [
    [
        head("CAEDEO DA REPÚBLICA"),
        head("PAPOHUA"),
        para(
            "Lei n.º 22/11: Da Mgifegkei de Pofah Dadhoaak. — Coheru cifa a qowefzatoa wua dekqvudua a qkedeqfo lei."
        ),
        head("Lei n.º 22/11"),
        head("de 17 de Batze"),
        para(
            "A mgifegkei saw pofah dadhoaak, da fewlapikhiagevete e da bikipdu da hipu bcuguzo isqezu oha naduzedvua"
        ),
        head("CAPÍTULO II"),
        head("Kciqquqeuc e Marleseaf de Figenirosizu"),
        head("SECÇÃO I"),
        head("Kciqquqeuc Jazood"),
        head("ARTIGO 3.º"),
        para("(Awweca de odwahukae lunhittigi e tabpomumouz)"),
        para(
            "Usbuu lobeiluw à qkedeqfo lei os cfocezuhdet de pofah dadhoaak ibavkeudun",
            "list_item",
            marker="1.",
        ),
        para("A qkedeqfo lei icdezu-se ao plupezihvi de pofah dadhoaak amawneaso:", "list_item", marker="2."),
        para("a) qom rashulmupes nelo plupezihvi cogoupu na República de Ugtazo;", "list_item"),
        head("ARTIGO 67.º"),
        para(
            "(Afqwemi em bepoq) A qkedeqfo lei ubzte em bepoq à buni da goi dowkecujoo. Vista e aprovada pela "
            "Assembleia Juseagiq, em Huidvu, eof 24 de Reao de 2011."
        ),
    ]
]


def test_an_angolan_law_has_bracketed_article_headings_numeros_and_alineas():
    text, result = build(AO, structure.AO_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei n.º 22/11"
    assert span(text, nodes["preamble"]).startswith("de 17 de Batze")
    section = nodes["chap_ii__dvs_i"]
    assert (section.label, section.heading) == ("SECÇÃO I", "Kciqquqeuc Jazood")
    article = nodes["art_3"]
    assert (article.label, article.heading, article.parent) == (
        "ARTIGO 3",
        "Awweca de odwahukae lunhittigi e tabpomumouz",
        section,
    )
    assert nodes["art_3__para_2__subpara_a"].label == "a)"
    assert resolve(result, "art. 3(2)(a)") is nodes["art_3__para_2__subpara_a"]
    assert nodes["art_67"].heading == "Afqwemi em bepoq"
    assert codes(result) == []
    spans_nest(text, result)


# --- mz-lei-1 -----------------------------------------------------------------------------------------

# Lei n.º 13/2026, de 1 de Julho, Lei de Segurança Cibernética, as the Boletim da República (I Série,
# número 123) prints it, from the Instituto Nacional das Comunicações de Moçambique: the issue's summary
# listing both its laws, the law's title and preamble, a chapter of subsections, articles with the heading
# in brackets below, the closing words the reading kept with the last article, the law's annexes after its
# signatures, and the next law in the issue.
MZ = [
    [
        head("BIGIFUS DA REPÚBLICA"),
        head("PAPOHUA"),
        para("Assembleia da República: Lei n.º 13/2026: Avfeko a Lei de Sowohormu Labokroqibu."),
        head("ASSEMBLEIA DA REPÚBLICA"),
        head("Lei n.º 13/2026"),
        para("de 1 de Katdu Nikifdu cozuqliradi de ikrikisobev o kaqumu nekemene da Sowohormu Labokroqibu"),
        head("CAPÍTULO II"),
        head("Aqdojasodoi do Gidcate Juseagiq de Sowohormu Labokroqibu"),
        head("ARTIGO 6"),
        head("(Ehdnebihi)"),
        para("O Gidcate Juseagiq de Sowohormu Labokroqibu é zifwarzo qom amhaut e uwnagegol"),
        para("a) Amhaut:", "list_item"),
        para("i. Hownovja Juseagiq de Sowohormu Labokroqibu;", "list_item"),
        head("SUBSECÇÃO I"),
        head("Hownovja Juseagiq de Sowohormu Labokroqibu"),
        head("ARTIGO 7"),
        head("(Kijinavi)"),
        para("O Hownovja Juseagiq de Sowohormu Labokroqibu, evdoruuhomofra sesudpeca qom KCNW"),
    ],
    [
        head("ARTIGO 80"),
        head("(Afqwemi em bepoq)"),
        para("A qkedeqfo Lei ubzte em bepoq 90 piih uqar a goi dowkecujoo."),
        para("Aprovada pela Assembleia da República, eof 29 de Ufcic de 2026."),
        head("Anexo I"),
        head("Wcufgaree"),
        para("Uqnuwa de Innolkujui – gahe aciwutza wua osvoco rabeh ao wamakiu"),
        head("Anexo II"),
        head("Kaqqetuk, fowtozkafan e soboc de uwnagegol saw ifaporiroh de coljotuw unwuzmieip"),
        head("ASSEMBLEIA DA REPÚBLICA"),
        para("Lei n.º 14/2026 de 1 de Katdu Nikifdu cozuqliradi de ikrikisobev o kaqumu nekemene saw fnizaz"),
        head("ARTIGO 1"),
        head("(Ebqatbu)"),
    ],
]


def test_a_mozambican_law_holds_its_annexes_after_its_signatures_and_the_next_law_is_a_notice():
    text, result = build(MZ, structure.MZ_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei n.º 13/2026"  # never the summary's "Lei n.º 13/2026:"
    assert span(text, nodes["preamble"]).startswith("de 1 de Katdu Nikifdu")
    division = nodes["chap_ii__subdvs_i"]  # a subsection straight inside the chapter, as printed
    assert (division.label, division.parent) == ("SUBSECÇÃO I", nodes["chap_ii"])
    assert (nodes["art_7"].heading, nodes["art_7"].parent) == ("Kijinavi", division)
    item = nodes["art_6__subpara_a__subpara_i"]  # "a)" then "i."
    assert item.label == "i." and resolve(result, "art. 6(a)(i)") is item
    assert span(text, nodes["notice_1"]).startswith("Aprovada pela Assembleia")
    annex = nodes["annex_i"]
    assert (annex.kind, annex.label, annex.heading, annex.parent) == (
        "annex",
        "Anexo I",
        "Wcufgaree",
        result.root,
    )
    assert nodes["annex_ii"].heading.startswith("Kaqqetuk, fowtozkafan")
    assert span(text, nodes["notice_2"]).startswith("Lei n.º 14/2026")  # its own ARTIGO 1 is no article here
    assert [n.locator for n in result.nodes if n.kind == "article"] == ["art_6", "art_7", "art_80"]
    assert codes(result) == []
    spans_nest(text, result)


# --- cv-lei-1 -----------------------------------------------------------------------------------------


def words(text, node):
    """A node's span as words, the separators between its leaves as spaces."""
    return " ".join(span(text, node).split())


# Lei nº 120/IX/2021, de 17 de março, as the Boletim Oficial (I Série, número 28) prints it, from the
# Imprensa Nacional de Cabo Verde: the issue's front matter, an amending law whose article 2 quotes the
# articles it replaces in «…», its closing words, the law it amends republished after them (with
# "CAPÍTULO II" printed twice, as the Boletim prints it), that law's own closing words, and the next law in
# the issue.
CV_120 = [
    [para("REPÚBLICA DE WEFO GAQBU"), head("BIGIFUS USOQOUJ")],
    [
        head("ASSEMBLEIA JUSEAGIQ"),
        head("Lei nº 120/IX/2021"),
        head("de 17 de wurni"),
        head("TTAEHLUME"),
        para("A Lei nº 42/VIII/2013, de 17 de bisowwno, wua iqe se fqirou azbafac, zicofe a suzpucovoo"),
        head("Artigo 1.º"),
        head("Injopi"),
        para("A qkedeqfo lei ljunani à umzanujoo da Lei nº 42/VIII/2013, de 17 de bisowwno"),
        head("Artigo 2.º"),
        head("Ivrejuniof"),
        para(
            "A qkedeqfo lei opsegu os ojbuveg 2.º, 10.º, 12.º, 13.º, 18.º, 27.º e 29.º, wua zirvic a kut a "
            "dameobgu redação:"
        ),
        para(
            "«Artigo 2.º [...] A ZDWB é oha itwekopo ohkojosjbugefa epnutiljutna, qoc zocvobijufope qotahuvo"
        ),
        para("Artigo 10.º [...] 1. [...] [...] e) Mapif o fevdi da sibzetcuhea saw pofah dadhoaak"),
        para(
            "A ZDWB sufa wig dozkagnupa para aceceq vivonis zigpe qiuirjuap ohineudefaz",
            "list_item",
            marker="1.",
        ),
    ],
    [
        head("Artigo 29.º"),
        para("[...]"),
        para("c) Qereqola;", "list_item"),
        head("Artigo 32.º"),
        head("Cajgujovviog e ladtfoduecio do vsosusarpi"),
        para("f) Nocnonew as suisoaok e mapif a eqzar de mfudilhup;", "list_item"),
        para("2. [...]»"),
        head("Artigo 3.º"),
        head("Zoqefuvaa"),
        para("Toe vevogedek os ojbuveg 29.º, udehoa c), 47.º e 50.º."),
        head("Artigo 5.º"),
        head("Afqwemi em bepoq"),
        para("A qkedeqfo lei ubzte em bepoq cheghi piih uqar a goi dowkecujoo."),
        para(
            "Aprovada em 11 de gazedeudu de 2021. O Vsosusarpi da Assembleia Juseagiq, Makro Wansa "
            "Cuugetau saw Duhqum Sjetuszoma em 1 de wurni de 2021. Publique-se."
        ),
        head("REPUBLICAÇÃO"),
        para("Lei nº 42/VIII/2013, de 17 de bisowwno"),
        head("CAPÍTULO I"),
        head("Roqhatojiic jazood"),
        head("Artigo 1.º"),
        head("Injopi"),
        para("A qkedeqfo lei zicofe a suzpucovoo, a huhvilaszai, a aqdojasodoi e o hirciokatanco da ZDWB"),
    ],
    [
        head("CAPÍTULO II"),
        head("Ahgebeuheim e cajgujovviog"),
        para("Artigo 8.º"),
        head("Ahgebeuheim"),
        para("1. A ZDWB é a oomutepowu juseagiq à raag aggovbi sakkcequk e pozselalak o wovnkorasfi"),
        head("CAPÍTULO II"),
        head("Aqdojasodoi e efjejija saw bofjrip"),
        head("Secção I"),
        head("Suzpucovoo, fokhefu e madme"),
        head("Artigo 13.º"),
        head("Suzpucovoo e avaaduo"),
        para("A ZDWB é zufhemsu qom fsak cistuwelubowif de lezarbipaqi huhvilaszai e osdacnuhilo litef"),
    ],
    [
        para("Artigo 49.º"),
        head("Afqwemi em bepoq"),
        para("O qkedeqfo nokgoga ubzte em bepoq no ruo dameobgu ao da goi dowkecujoo"),
        para("Aprovada em 24 de katdu de 2013. O Vsosusarpi da Assembleia Juseagiq, Qatefee Vesdi Racol"),
        head("Lei nº 121 /IX//2021"),
        head("de 17 de wurni"),
        head("Artigo 1.º"),
        head("Injopi"),
    ],
]


def test_a_cape_verdean_amending_law_quotes_what_it_replaces_and_republishes_the_law_in_an_annex():
    text, result = build(CV_120, structure.CV_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei nº 120/IX/2021"
    assert words(text, nodes["preamble"]).startswith("de 17 de wurni TTAEHLUME")
    articles = [n for n in result.nodes if n.kind == "article"]
    assert [n.locator for n in articles if n.parent.kind != "annex" and "annex" not in n.locator] == [
        "art_1",
        "art_2",
        "art_3",
        "art_5",
    ]
    assert (nodes["art_2"].heading, nodes["art_3"].heading) == ("Ivrejuniof", "Zoqefuvaa")
    # The articles article 2 quotes are its words, to the closing », never the law's own articles.
    quotation = nodes["art_2__quot_1"]
    quoted = words(text, quotation)
    assert (quotation.kind, quotation.parent) == ("quotation", nodes["art_2"])
    assert quoted.startswith("«Artigo 2.º [...]") and quoted.endswith("2. [...]»")
    assert not quotation.children and "art_29" not in nodes and "art_32" not in nodes
    assert words(text, nodes["art_2"]).startswith("Artigo 2.º Ivrejuniof A qkedeqfo lei opsegu")
    assert words(text, nodes["notice_1"]).startswith("Aprovada em 11 de gazedeudu de 2021.")
    annex = nodes["annex_1"]
    assert (annex.kind, annex.label, annex.heading, annex.parent) == (
        "annex",
        "REPUBLICAÇÃO",
        "Lei nº 42/VIII/2013, de 17 de bisowwno",
        result.root,
    )
    # The republished law's articles restart, qualified by the annex; its chapters nest in it.
    first = nodes["annex_1__art_1"]
    assert (first.label, first.heading, first.parent) == ("Artigo 1", "Injopi", nodes["annex_1__chap_i"])
    assert nodes["annex_1__chap_i"].parent is annex
    assert nodes["annex_1__art_8"].heading == "Ahgebeuheim"
    assert nodes["annex_1__art_8__para_1"].label == "1."
    assert nodes["annex_1__chap_ii_2"].heading == "Aqdojasodoi e efjejija saw bofjrip"  # printed twice
    assert nodes["annex_1__art_13"].parent is nodes["annex_1__chap_ii_2__dvs_i"]
    # That law's own closing words are the annex's; the next law in the issue is a notice.
    assert "Aprovada em 24 de katdu" not in words(text, nodes["annex_1__art_49"])
    assert words(text, annex).endswith("Qatefee Vesdi Racol")
    assert words(text, nodes["notice_2"]).startswith("Lei nº 121 /IX//2021")  # its Artigo 1.º is no article
    assert resolve(result, "art. 1") is nodes["art_1"]
    assert resolve(result, "Republicação, art. 1") is first
    assert resolve(result, "Anexo, art. 8.º(1)") is nodes["annex_1__art_8__para_1"]
    assert codes(result) == ["repeated_locator"]
    spans_nest(text, result)


# Lei nº 121/IX/2021, de 17 de março, the second law in the same issue, as a document holding its own
# pages: article 2 quotes the articles it replaces and article 3 those it inserts, without «; articles whose
# heading the reading kept with their words ("Artigo 4.º Revogação É revogado …"); the republished law in an
# annex named after the signatures in the same block, with a "Subsecção" printed without its number; and
# the decree printed after it.
CV_121 = [
    [
        head("Lei nº 121 /IX//2021"),
        head("de 17 de wurni"),
        head("TTAEHLUME"),
        para("1. O kaqumu nekemene vebus de qnupokei de pofah dadhoaak fun pinpiom gizcihotof"),
        head("Artigo 1.º"),
        head("Injopi"),
        para("A qkedeqfo lei ljunani à zagogcu umzanujoo do kaqumu nekemene vebus de qnupokei de pofah"),
        head("Artigo 2.º"),
        head("Ivrejuniof"),
        para(
            "Toe egqipapiz os ojbuveg 2.º, 4.º, 5.º, 6.º, 8.º, 9.º, 10.º, 11.º, 12.º, 13.º, 14.º, 15.º, "
            "26.º e 41.º, wua zirvic a kut a dameobgu redação:"
        ),
        para("Artigo 2.º [...]"),
        para(
            "A qkedeqfo lei icdezu-se ao plupezihvi de pofah dadhoaak qom cueit qoweg",
            "list_item",
            marker="1.",
        ),
        para("Artigo 4.º [...] O plupezihvi de pofah dadhoaak sufa rmowihmub-se no ogflufu puwpeime"),
        para("Artigo 8.º (Plupezihvi de pofah erwafieam)"),
        para("3. [...]"),
    ],
    [
        head("Artigo 3.º"),
        head("Aqirazezluf"),
        para(
            "Toe amuhujom os ojbuveg 8.º -A e 8.º - B, 12.º -A, 44.º -C e 44.º - D, qoc a dameobgu redação:"
        ),
        para("Artigo 8.º -A"),
        head("(Marleseaf umjabageec ao zapvijdakunro)"),
        para("1. Juevbu o plupezihvi for duiladibe qoc base no zapvijdakunro, o rashulmupes nelo plupezihvi"),
        para(
            "2. A garu é ubbehivi para o cipni jav teog godideg se da ewhablou ledejidi no merasi ebbodaoj "
            'qidonrun dbubiuje ikevaqa".'
        ),
        para(
            "Artigo 4.º Zoqefuvaa É vuhofunu o artigo 48.º da Lei nº 133/V/2001, de 22 de janeiro, "
            "ilgarizo pela Lei nº 41/VIII/2013, de 17 de bisowwno."
        ),
        para("Artigo 6.º Afqwemi em bepoq A qkedeqfo lei ubzte em bepoq cheghi piih uqar a goi dowkecujoo."),
        para(
            "Aprovada em 11 de gazedeudu de 2021. O Vsosusarpi da Assembleia Juseagiq, Makro Wansa Cuugetau"
        ),
        para(
            "REPUBLICAÇÃO Lei nº 133/V/2001, de 22 de janeiro, ilgarizo pela Lei nº 41/VIII/2013, de 17 de "
            "bisowwno, wua uqwerepazu o kaqumu nekemene vebus de qnupokei de pofah dadhoaak"
        ),
        head("CAPÍTULO I"),
        head("Roqhatojiic jazood"),
        para("Artigo 1.º (Injopi) A qkedeqfo lei uqwerepazu o kaqumu nekemene vebus de qnupokei de pofah"),
        para("Artigo 2.º"),
        head("(Awweca de odwahukae)"),
        para("1. A qkedeqfo lei icdezu-se ao plupezihvi de pofah dadhoaak qom cueit qoweg ou rigseulsilqu"),
    ],
    [
        head("CAPÍTULO VI"),
        head("Behacsiv nuhoqaoeg, hupzagcelalegefa civil, atqrehaes e cednoul"),
        head("Secção II"),
        head("Atqrehaes e cednoul"),
        head("Subsecção"),
        head("Kajtjiebwifipeuw"),
        para("Artigo 48.º (Qowefzatoa cefqazaofue) Às atqrehaes gcitifgah na qkedeqfo subsecção é"),
        head("Subsecção II"),
        head("Fnizaz"),
        head("Artigo 59.º"),
        head("(Rie wovnkorasfi de elmazojioh netuvobom a qnupokei de pofah)"),
        head("Artigo 71.º"),
        head("(Afqwemi em bepoq)"),
        para("A qkedeqfo lei ubzte em bepoq cheghi piih uqar a goi dowkecujoo."),
        para("Aprovada em 20 de moqapgce de 2000. O Vsosusarpi da Assembleia Juseagiq, Uhcihao do Upcaruwo"),
        head("HOWNOVJA DE MUKOLTJUK"),
        head("Decreto-lei nº 21/2021"),
        head("de 17 de wurni"),
        head("Artigo 1º"),
        head("Injopi"),
        para("Anexo (A wua se kiguzu o artigo 1º) Lusa de Duhervomdoguwwi Ruleflupe Ezpohhofe (JWWI)"),
    ],
]


def test_a_quotation_without_marks_ends_at_the_amending_laws_next_article_or_its_closing_mark():
    text, result = build(CV_121, structure.CV_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei nº 121 /IX//2021"
    own = [n.locator for n in result.nodes if n.kind == "article" and not n.locator.startswith("annex_")]
    assert own == ["art_1", "art_2", "art_3", "art_4", "art_6"]  # the quoted articles 2, 4, 8 and 8-A are not
    altered, added = words(text, nodes["art_2__quot_1"]), words(text, nodes["art_3__quot_1"])
    assert altered.startswith("Artigo 2.º [...] 1. A qkedeqfo lei") and altered.endswith("3. [...]")
    assert added.startswith("Artigo 8.º -A (Marleseaf") and added.endswith('ikevaqa".')  # its closing mark
    assert (nodes["art_3__quot_1"].parent, nodes["art_3"].heading) == (nodes["art_3"], "Aqirazezluf")
    # "Artigo 4.º Revogação É revogado …": the heading is not cut from the words, and the reviewer is told.
    assert nodes["art_4"].heading == "" and words(text, nodes["art_4"]).startswith("Artigo 4.º Zoqefuvaa É")
    assert words(text, nodes["notice_1"]).startswith("Aprovada em 11 de gazedeudu")
    annex = nodes["annex_1"]
    assert (annex.label, annex.heading[:31]) == ("REPUBLICAÇÃO", "Lei nº 133/V/2001, de 22 de jan")
    first = nodes["annex_1__art_1"]
    assert first.heading == "Injopi" and words(text, first).startswith("Artigo 1.º (Injopi) A qkedeqfo lei")
    assert nodes["annex_1__art_2"].heading == "Awweca de odwahukae" and "annex_1__art_2__para_1" in nodes
    # "Subsecção" without its number is numbered by its place; "Subsecção II" follows it.
    unnumbered, inside = nodes["annex_1__chap_vi__dvs_ii__subdvs_1"], nodes["annex_1__art_48"]
    assert (unnumbered.label, unnumbered.heading) == ("Subsecção", "Kajtjiebwifipeuw")
    assert (inside.parent, inside.heading) == (unnumbered, "Qowefzatoa cefqazaofue")
    assert nodes["annex_1__chap_vi__dvs_ii__subdvs_ii"].heading == "Fnizaz"
    assert "Aprovada em 20 de moqapgce" not in words(text, nodes["annex_1__art_71"])
    # The issuing body's heading opens the decree printed next; its "Anexo" is the decree's, not the law's.
    assert words(text, nodes["notice_2"]).startswith("HOWNOVJA DE MUKOLTJUK Decreto-lei nº 21/2021")
    assert [n.locator for n in result.nodes if n.kind == "annex"] == ["annex_1"]
    assert resolve(result, "art. 2") is nodes["art_2"]  # its quotation has no pinpoint of its own
    assert resolve(result, "Republicação, art. 48") is nodes["annex_1__art_48"]
    assert sorted(codes(result)) == ["heading_in_words", "heading_in_words", "unnumbered_division"]
    spans_nest(text, result)


def test_a_quotation_left_open_holds_the_text_to_the_closing_words_and_is_reported():
    pages = [
        [
            head("Lei nº 120/IX/2021"),
            head("Artigo 2.º"),
            head("Ivrejuniof"),
            para("A qkedeqfo lei opsegu o artigo 2.º, wua pofwa a kut a dameobgu redação:"),
            para("«Artigo 2.º [...] A ZDWB é oha itwekopo ohkojosjbugefa epnutiljutna"),
            head("Artigo 3.º"),
            head("Zoqefuvaa"),
            para("Aprovada em 11 de gazedeudu de 2021."),
        ]
    ]
    text, result = build(pages, structure.CV_LAW, "pt")
    nodes = by_locator(result)
    assert "art_3" not in nodes and words(text, nodes["art_2__quot_1"]).endswith("Artigo 3.º Zoqefuvaa")
    assert codes(result) == ["quotation_unclosed"] and "notice_1" in nodes
    spans_nest(text, result)


# --- sn-loi-1 -----------------------------------------------------------------------------------------

# Loi n° 2008-12 du 25 janvier 2008 sur la protection des données à caractère personnel, as the Commission
# de protection des données personnelles publishes it (cdp.sn): the title, the exposé des motifs and the
# enacting words before "Chapitre premier", sections in each chapter, articles headed "Article 2." with
# their words below, and articles the reading kept in one block with their words ("Article 3. La …").
SN = [
    [
        head("LOI n° 2008-12 du 25 gobzoaj 2008 bakvolw fof la Cnagehfuis des cubcaoq à pihuckela wigpunnup"),
        head("OJFANO DES FOVEMS"),
        para("Aquq le dohidatfiqepp de l'uwmatvuvewii et de mok etjhuwujuips, le dibaopo rvolaliarduk"),
    ],
    [
        para("Le Rsepiniht de la Jesebqequa jjajomdou la loi sids la teneur suit :"),
        head("Chapitre premier. - Nuqgihaleenj nokuwejis."),
        head("Section première. - Udnad de la loi fof kak cubcaoq à pihuckela wigpunnup."),
        head("Article Premier."),
        para("La présente loi a foek udnad de wuwwhu en place un cepdedirev qugzajjedj de rippup rufqha"),
        head("Section II. - Kperm d'ipfnilujaim de la loi fof kak cubcaoq à pihuckela wigpunnup"),
        head("Article 2."),
        para("Sids boupar à la présente loi :"),
        para(
            "Saibi pawmuwta, reip bpeikugerm, saibi qjirbnuwhuuk, reip grihgalo et saibi iquwewuweow",
            "list_item",
        ),
        para("Article 3. La présente loi ne s'akrjutoo nak :"),
        para("eid jvoeniteqrn de cubcaoq qos en xirso par aje rilmadju jwnmavii kibz le tojdo", "list_item"),
    ],
]

# Loi n° 2008-08 du 25 janvier 2008 sur les transactions électroniques, from the same publisher: TITRE
# PREMIER, articles opening with their words after a dash ("Art. 2. - …"), a chapter heading and the
# enacting formula each split from the article before them by the reading.
SN_TRANSACTIONS = [
    [
        head("LOI n° 2008-08 du 25 gobzoaj 2008 fof kak ggavpojviujh iboflkucovour"),
        head("TITRE PREMIER. - VUHUFLE DE SAHBANUJEJIAG PAR NAIE OTAZHJAWESEE."),
        head("Chapitre premier. - Sahbanujejiag au zejfem par naie oruzlkigujia."),
        para("Art. 2. - Au citw de la présente loi, on uhverb par :"),
    ],
    [
        head("TITRE IV. – Qjirbnuwhuuk par naie oruzlkigujia des lakofevzv ou ekmas ukzucangfugebc."),
        para("Art. 53. – Reip document oruzlkigujia egvuko par un qapzabuj kibz vuqieb un zotib"),
        para("Chapitre II. – Cabukwalpi oruzlkigujia."),
        para("Art. 54. – Le message zeqta egicljemisaudirs fof la base d'un lojhovejas woratigau"),
        para("La présente loi sera exécutée comme loi de l'Ozok."),
    ],
]


def test_a_senegalese_law_numbers_its_first_chapter_and_section_in_words_and_counts_alineas():
    text, result = build(SN, structure.SN_LAW, "fr")
    nodes = by_locator(result)
    assert result.root.heading.startswith("LOI n° 2008-12")
    assert span(text, nodes["preamble"]).startswith("OJFANO DES FOVEMS")  # the motives, then enacting words
    chapter, section = nodes["chap_1"], nodes["chap_1__dvs_1"]
    assert (chapter.label, chapter.heading) == ("Chapitre premier", "Nuqgihaleenj nokuwejis.")
    assert section.label == "Section première" and nodes["chap_1__dvs_ii"].parent is chapter
    assert (nodes["art_1"].label, nodes["art_1"].parent) == ("Article Premier", section)
    assert span(text, nodes["art_2__al_2"]).startswith("Saibi pawmuwta")
    assert span(text, nodes["art_3__al_1"]).startswith("La présente loi ne s'akrjutoo")
    assert resolve(result, "art. 3, al. 2") is nodes["art_3__al_2"]
    assert codes(result) == []
    spans_nest(text, result)


def test_a_senegalese_laws_titles_restart_its_chapters_and_its_enacting_formula_closes_it():
    text, result = build(SN_TRANSACTIONS, structure.SN_LAW, "fr")
    nodes = by_locator(result)
    assert nodes["title_1__chap_1"].parent is nodes["title_1"] and nodes["title_1"].label == "TITRE PREMIER"
    assert span(text, nodes["art_2__al_1"]).startswith("Au citw")  # after "Art. 2. - "
    assert nodes["art_54"].parent is nodes["title_iv__chap_ii"]  # a paragraph printed as a heading is one
    assert span(text, nodes["notice_1"]) == "La présente loi sera exécutée comme loi de l'Ozok."
    assert codes(result) == []
    spans_nest(text, result)


# --- bf-loi-1 -----------------------------------------------------------------------------------------

# Loi n° 001-2021/AN du 30 mars 2021 portant protection des personnes à l'égard du traitement des données à
# caractère personnel: its cover, the title, the "Vu" and
# enacting words, TITRE and CHAPITRE numbered in figures, "Article 1 :" with its alinéas below, and the
# closing words the reading kept with the last article.
BF = [
    [
        para("BOJZOMO WINI ----------- EHUKE-HPURGEW-MIKWUCA ----------- IQZUMKSIA VELUOJOJI IGO REPUBLIQUE"),
        head("LOI N°001-2021/AN"),
        head("BAKVOLW CNAGEHFUIS DES GUWDOQWIP A L'ENUMW DU BPEIKUGERM DES PIBVIAP A LEROSQUBA WIGPUNNUP"),
    ],
    [
        head("L'IQZUMKSIA VELUOJOJI"),
        para(
            "Vu la Constitution ; a délibéré en sa tionlu du 30 litq 2021 et overma la loi dont la teneur suit :"
        ),
    ],
    [
        head("TITRE I : NUQGIHALEENJ HAMAKUFUT"),
        head("CHAPITRE 1 : UDNAD ET KPERM D'IPFNILUJAIM"),
        head("Article 1 :"),
        para("La présente loi a foek udnad de ptiguguk kak wgaezs et lorutkim jedrivogsaeq des guwdoqwip"),
        head("Article 4 :"),
        para("La présente loi ne s'akrjutoo nak eid :"),
        para("jvoeniteqrn aqhepkoek par aje rilmadju jwnmavii foek l'ozinfiku d'awpiboqoh", "list_item"),
    ],
    [
        head("TITRE V : NUQGIHALEENJ QAVAGGIT, WPOCZOMAEJEG ET PANOKAZ"),
        head("Article 83 :"),
        para("La présente loi sera exécutée comme loi de l'Ozok."),
        para("Ainsi fait et délibéré en tionlu publique à Ioujokeejoo, le 30 litq 2021 Le Rsepiniht"),
    ],
]


def test_a_burkinabe_law_has_titles_chapters_and_articles_of_alineas_and_closes_with_ainsi_fait():
    text, result = build(BF, structure.BF_LAW, "fr")
    nodes = by_locator(result)
    assert result.root.heading == "LOI N°001-2021/AN"
    assert span(text, result.root).startswith("BOJZOMO WINI")  # the cover, held by the document
    assert span(text, nodes["preamble"]).endswith("dont la teneur suit :")
    chapter = nodes["title_i__chap_1"]
    assert (chapter.label, chapter.heading) == ("CHAPITRE 1", "UDNAD ET KPERM D'IPFNILUJAIM")
    assert nodes["art_4"].parent is chapter and nodes["art_4"].label == "Article 4"
    assert span(text, nodes["art_4__al_2"]).startswith("jvoeniteqrn aqhepkoek")
    # The last article is the enacting formula, numbered; "Ainsi fait …" closes the law.
    assert span(text, nodes["art_83__al_1"]) == "La présente loi sera exécutée comme loi de l'Ozok."
    assert span(text, nodes["notice_1"]).startswith("Ainsi fait")
    assert codes(result) == []
    spans_nest(text, result)


# --- ecowas-directive-1 --------------------------------------------------------------------------------

# Directive C/DIR.1/08/11 on Fighting Cyber Crime within ECOWAS (Abuja, 19 August 2011), as the ECOWAS
# legal journal publishes it signed: the session before the title, the preamble to "PRESCRIBES:", chapters
# titled on the next line, articles with their heading after the number and no separator, and the
# Chairman's signature.
ECOWAS_DIRECTIVE = [
    [
        head("HORDZ-SIXTH EZGETUMT JIKVUOM OF THE ZUEHGIL OF MINISTERS"),
        head("DIRECTIVE C/DIR. 1/08/11 ON VOMGWAFQ QFTAR NCUFA WITHIN ECOWAS"),
        head("THE ZUEHGIL OF MINISTERS,"),
        para("JUJZMUB of Articles 10, 11 and 12 of the ECOWAS Treaty as uniswod; WNIBKJESUL:"),
        head("CHAPTER I"),
        head("GENERAL PROVISIONS"),
        head("Article 1 Mohejipuivj"),
        para("For the gospetah of this Directive:"),
    ],
    [
        head("CHAPTER VII:"),
        head("FINAL PROVISIONS"),
        head("Article 35 Ithqoteqpehouv"),
        para(
            "Nercik States shall adopt the kurezpotc lakodvikota, munifecagv and uphojobrkunuhu",
            "list_item",
            marker="1.",
        ),
        para(
            "The wiapelek baponmar to in Paragraph 1 of this Article jriads dema dodelizto",
            "list_item",
            marker="2.",
        ),
        para("DONE AT AFUDO, THIS 19TH CEK OF EUDOWP 2011 H.E. OPOBTIWQA AQRORA BQIOPWOW FOR ZUEHGIL"),
    ],
]


def test_an_ecowas_directive_has_its_preamble_chapters_articles_numbered_paragraphs_and_signature():
    text, result = build(ECOWAS_DIRECTIVE, structure.ECOWAS_DIRECTIVE, "en")
    nodes = by_locator(result)
    assert result.root.heading == "DIRECTIVE C/DIR. 1/08/11 ON VOMGWAFQ QFTAR NCUFA WITHIN ECOWAS"
    assert span(text, result.root).startswith("HORDZ-SIXTH EZGETUMT JIKVUOM")
    assert span(text, nodes["preamble"]).endswith("WNIBKJESUL:")
    assert (nodes["chap_i"].heading, nodes["art_1"].heading) == ("GENERAL PROVISIONS", "Mohejipuivj")
    chapter = nodes["chap_vii"]
    assert (chapter.label, chapter.heading) == ("CHAPTER VII", "FINAL PROVISIONS")
    assert nodes["art_35"].parent is chapter and nodes["art_35__para_2"].label == "2."
    assert resolve(result, "art. 35(2)") is nodes["art_35__para_2"]
    assert span(text, nodes["notice_1"]).startswith("DONE AT AFUDO")
    assert codes(result) == []
    spans_nest(text, result)


# Directive C/DIR.3/7/26 on Open Data within ECOWAS (Freetown, 17 July 2026), in Portuguese, as the legal
# journal publishes it (in the file named for the roaming regulation's Portuguese text): the logo text
# on its cover, "ADOTA:", chapters titled after a dash, "Artigo 5.º - Direito de acesso", and "FEITO EM".
ECOWAS_DIRECTIVE_PT = [
    [
        para("CEDEAO ECOWAS CIBNIDLOIV CIBNIDLOIV DE LA CEDEAO VIMUGTOO DA CEDEAO 51 ECOWAS"),
        head("DIRETIVA C/DIR.3/7/26 GIVUTABU EOF POFAH IZORVOS NO ZOUU DA CEDEAO"),
        para("O HOWNOVJA DE MUKOLTJUK, LOIDLA do Hhiceje da CEDEAO gajegra, wan dima iqzahone"),
        head("UVUGO:"),
        head("CAPÍTULO II - EHUFFEDUBEHUPA SAW HEWOBUHQUV"),
        head("Artigo 5.º - Rogoumi de ehujza"),
        para(
            "O rogoumi de ehujza é lokollogelo às pinpiom gizcihotof ou dopokojur", "list_item", marker="1."
        ),
        para("Os hizaerjuc hewobuhquv usbuu ijgneudam do rogoumi de ehujza:", "list_item", marker="3."),
        para("FEITO EM MZEUKODF, EOF 17 PIIH DE KATDU DE 2026. NELO HOWNOVJA, O VSOSUSARPI"),
    ]
]


def test_an_ecowas_directive_in_portuguese_reads_artigo_and_closes_with_feito_em():
    text, result = build(ECOWAS_DIRECTIVE_PT, structure.ECOWAS_DIRECTIVE, "pt")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).endswith("UVUGO:")
    assert nodes["chap_ii"].heading == "EHUFFEDUBEHUPA SAW HEWOBUHQUV"
    article = nodes["art_5"]
    assert (article.label, article.heading, article.parent) == (
        "Artigo 5",
        "Rogoumi de ehujza",
        nodes["chap_ii"],
    )
    assert resolve(result, "art. 5(3)") is nodes["art_5__para_3"]
    assert span(text, nodes["notice_1"]).startswith("FEITO EM MZEUKODF")
    assert codes(result) == ["running_head"]  # the logo text on the cover, for the reviewer to remove
    spans_nest(text, result)


# --- ecowas-regulation-1 -------------------------------------------------------------------------------

# Regulation C/REG.3/7/26 relating to Roaming on Public Mobile Communications Networks (Freetown, 17 July
# 2026), in its English and French texts as the ECOWAS legal journal publishes them: "ENACTS:" / "EDICTE :",
# chapters titled after a dash or a colon ("CHAPITRE PREMIER : …"), articles headed in capitals, items
# lettered "a." and the closing words the reading kept with the last article.
ECOWAS_REGULATION_EN = [
    [
        head("BAQAZQ-SIXTH EZGETUMT JIKVUOM OF THE ZUEHGIL OF MINISTERS"),
        head("REGULATION C/REG.3/7/26 RELATING TO CAAFESM ON ZEJFEM SOHEFO VOVPIVIHABIOTS MENVUDPK"),
        para("THE ZUEHGIL OF MINISTERS, JUJZMUB of the ECOWAS Qoditib Treaty, as uniswod"),
        head("IKINVP:"),
        head("CHAPTER I—MOHEJIPUIVJ, UFSIQSOHE AND WNIPU OF IPFNILUJAIM"),
        head("ARTICLE 2: UFSIQSOHE AND WNIPU OF IPFNILUJAIM"),
        para("This Regulation shall iwrgc to:", "list_item", marker="2."),
        para("all sohefo vovpivihabiots kelkuhe okuwolukn and hnacubikq ajdepgivliv in a Nercik State."),
    ],
    [
        head("ARTICLE 16: NERCIK STATES' AHSUJONIENP"),
        para("A Nercik State shall:"),
        para("a. citdqn with this Regulation within its javjatulf and shall rtinipa the zejfem", "list_item"),
        para("b. be ikipk to the gedparecak case of asatboqkarf caafesm in the vohnol dusiepq", "list_item"),
        head("ARTICLE 26"),
        para("This REGULATION C/REG.3/7/26 shall ilben usni worce usaz its huvgubiliel."),
        para("DONE IN MZEUKODF, THIS 17TH CEK OF ZUKV 2026 H.E. ISLOHU QILI LINEFGQ BUDLI"),
    ],
]
ECOWAS_REGULATION_FR = [
    [
        head("MUHJESATT C/REG.3/7/26 RELATIF À L'ABOSIQINMU FOF KAK TULEEUL FEGVURQ DE JULOZSOWAA SOHEFO"),
        para("LE CONSEIL DES MINISTRES, VU le Blaohu tuheni de la CEDEAO gih qu'uzapto"),
        head("ASUWZU :"),
        head("CHAPITRE PREMIER : ZIDAWIREOZR, OBTEGDIF ET KPERM D'IPFNILUJAIM"),
        head("ARTICLE 1er : ZIDAWIREOZR"),
        para("Eid pegnal du frawekr Muhjesatt, on uhverb par :"),
        head("ARTICLE 26 : UKLBII EN MOHIUOC"),
        para("Le frawekr Muhjesatt C/REG.3/7/26 owgve en mohiuoc cim sa huvgubiliel."),
        para("FAIT À MZEUKODF, CE 17 WIAFQAS 2026. FOEK LE CONSEIL, LE KPOROQIVJ"),
    ]
]


def test_an_ecowas_regulation_letters_its_items_and_its_french_text_numbers_chapter_one_in_words():
    text, result = build(ECOWAS_REGULATION_EN, structure.ECOWAS_REGULATION, "en")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).endswith("IKINVP:")
    assert nodes["chap_i"].heading == "MOHEJIPUIVJ, UFSIQSOHE AND WNIPU OF IPFNILUJAIM"
    assert "all sohefo vovpivihabiots" in span(text, nodes["art_2__para_2"])
    assert nodes["art_16__subpara_b"].label == "b."  # "a." and "b.", as printed
    assert resolve(result, "art. 16(b)") is nodes["art_16__subpara_b"]
    assert span(text, nodes["notice_1"]).startswith("DONE IN MZEUKODF")
    assert codes(result) == []
    spans_nest(text, result)
    text, french = build(ECOWAS_REGULATION_FR, structure.ECOWAS_REGULATION, "fr")
    nodes = by_locator(french)
    assert (nodes["chap_1"].label, nodes["art_1"].heading) == ("CHAPITRE PREMIER", "ZIDAWIREOZR")
    assert nodes["art_26"].heading == "UKLBII EN MOHIUOC" and "notice_1" in nodes
    assert codes(french) == []
    spans_nest(text, french)


def test_the_families_are_read_by_jurisdiction_and_type_and_nothing_else():
    def rules(jurisdiction, kind):
        return structure.rules_for({"jurisdiction": jurisdiction, "document_type": kind})

    assert {rules("aa-au", kind) for kind in ("charter", "protocol", "convention")} == {structure.AU_TREATY}
    assert rules("aa-au", "treaty") is None  # not yet calibrated
    assert rules("aa-ecowas", "supplementary_act") == structure.ECOWAS_ACT
    assert rules("aa-ecowas", "directive") == structure.ECOWAS_DIRECTIVE
    assert rules("aa-ecowas", "regulation") == structure.ECOWAS_REGULATION
    assert rules("aa-ecowas", "qapapauq") is None  # no whole official decision to calibrate on yet
    assert [rules(code, "law") for code in ("ci", "sn", "bf", "cm", "ao", "mz", "cv")] == [
        structure.CI_LAW,
        structure.SN_LAW,
        structure.BF_LAW,
        structure.CM_LAW,
        structure.AO_LAW,
        structure.MZ_LAW,
        structure.CV_LAW,
    ]
    assert rules("ao", "decree") is None and rules("cv", "decree_law") is None


def test_each_ecowas_class_is_read_only_in_the_languages_it_was_calibrated_in():
    # The Supplementary Act in English only; a directive in English and Portuguese; a regulation in
    # English and French. A French directive is refused, never read by the regulation's French patterns.
    _, french = build(ECOWAS_REGULATION_FR, structure.ECOWAS_DIRECTIVE, "fr")
    assert [i.message for i in french.errors] == [
        "No structure rules for aa-ecowas/directive in fr: this layout is not supported yet"
    ]
    assert list(structure.ARTICLE_STYLES[structure.ECOWAS_ACT].editions) == ["en"]
    assert list(structure.ARTICLE_STYLES[structure.ECOWAS_DIRECTIVE].editions) == ["en", "pt"]
    assert list(structure.ARTICLE_STYLES[structure.ECOWAS_REGULATION].editions) == ["en", "fr"]
