"""Structure rules for texts of articles (TXP-03, ArticleBuilder): the AU's charters and protocols, ECOWAS's
Supplementary Acts, and the laws of Côte d'Ivoire, Cameroon and Angola. Each family is tried on short
excerpts of an official text it was calibrated on, reviewed units as the workbench holds them after the
cloud reading (ingestion.calibrate): its front matter, preamble, divisions, articles, paragraphs or
alinéas, closing words and any stamp, and pinpoints resolved to their part.

The excerpts are a few provisions each, copied from the official files listed in
private-staging/structure-samples/manifest.tsv; never add more of their text than a test needs."""

from test_structure_families import resolve, spans_nest
from test_text_publication import build, by_locator, codes, head, para, span

from workbench import structure

# --- au-treaty-1 -------------------------------------------------------------------------------------

# African Charter on Human and Peoples' Rights (Nairobi, 27 June 1981), English and French editions as
# the AU publishes them (au.int, treaty 0011): its preamble, Part I, Chapter I and articles 1 and 7.
CHARTER_EN = [
    [para("ORGANIZATION OF AFRICAN UNITY"), para("AFRICAN CHARTER")],
    [
        head("AFRICAN CHARTER ON HUMAN AND PEOPLES' RIGHTS"),
        head("PREAMBLE"),
        para(
            "The African States members of the Organization of African Unity, parties to the present convention"
        ),
    ],
    [
        head("PART I : RIGHTS AND DUTIES"),
        head("CHAPTER I"),
        head("HUMAN AND PEOPLES' RIGHTS"),
        head("ARTICLE 1 :"),
        para(
            "The Member States of the Organization of African Unity parties to the present Charter shall recognize"
        ),
        head("ARTICLE 7"),
        para(
            "Every individual shall have the right to have his cause heard. This comprises :",
            "list_item",
            marker="1.",
        ),
        para("The right to an appeal to competent national organs against acts", "list_item"),
        para(
            "the right to be presumed innocent until proved guilty by a competent court or tribunal ;",
            "list_item",
        ),
        para(
            "No one may be condemned for an act or ommission which did not constitute",
            "list_item",
            marker="2.",
        ),
    ],
]
CHARTER_FR = [
    [
        head("CHARTE AFRICAINE DES DROITS DE L'HOMME ET DES PEUPLES"),
        head("PREAMBULE"),
        para("Les Etats africains membres de l'OUA, parties à la présente Charte qui porte le titre"),
    ],
    [
        head("PREMIERE PARTIE : DES DROITS ET DES DEVOIRS"),
        head("CHAPITRE I"),
        head("DES DROITS DE L'HOMME ET DES PEUPLES"),
        head("ARTICLE 1"),
        para(
            "Les Etats membres de l'Organisation de l'Unité Africaine, parties à la présente Charte, reconnaissent"
        ),
        head("DEUXIEME PARTIE : DES MESURES DE SAUVEGARDE"),
        head("CHAPITRE I"),
        head("DE LA COMPOSITION ET DE L'ORGANISATION DE LA COMMISSION AFRICAINE"),
        head("ARTICLE 30"),
        para("Il est créé auprès de l'Organisation de l'Unité Africaine une Commission Africaine des Droits"),
    ],
]

# Protocol on the Establishment of an African Court on Human and Peoples' Rights (Ouagadougou, 1998), in
# Portuguese: its enacting words and an article whose heading follows its number.
PROTOCOL_PT = [
    [
        head(
            "PROTOCOLO RELATIVO À CARTA AFRICANA DOS DIREITOS DO HOMEM E DOS POVOS SOBRE A CRIAÇÃO DE UM TRIBUNAL"
        ),
        para(
            "Os Estados Membros da Organização da Unidade Africana a seguir designada OUA, Estados Partes à Carta"
        ),
        head("ACORDARAM NO SEGUINTE:"),
        head("ARTIGO 1: CRIAÇÃO DO TRIBUNAL"),
        para(
            "É criado no seio da Organização da Unidade Africana um Tribunal Africano dos Direitos do Homem e dos Povos"
        ),
        head("ARTIGO 3: COMPETÊNCIAS DO TRIBUNAL"),
        para(
            "A jurisdição do Tribunal estende-se a todos os casos e disputas que lhe forem submetidos",
            "list_item",
            marker="1.",
        ),
        para(
            "Em caso de contestação quanto à competência ou não do Tribunal cabe a este decidir.",
            "list_item",
            marker="2.",
        ),
    ]
]


def test_an_au_charter_has_its_preamble_parts_chapters_articles_and_numbered_paragraphs():
    text, result = build(CHARTER_EN, structure.AU_TREATY, "en")
    nodes = by_locator(result)
    assert result.root.heading == "AFRICAN CHARTER ON HUMAN AND PEOPLES' RIGHTS"
    assert span(text, nodes["preamble"]).startswith("PREAMBLE")
    part, chapter = nodes["part_i"], nodes["part_i__chap_i"]
    assert (part.label, part.heading) == ("PART I", "RIGHTS AND DUTIES")
    assert (chapter.label, chapter.heading, chapter.parent) == (
        "CHAPTER I",
        "HUMAN AND PEOPLES' RIGHTS",
        part,
    )
    assert nodes["art_1"].parent is chapter and nodes["art_1"].heading == ""
    assert [n.label for n in nodes["art_7"].children] == ["1.", "2."]
    # The reading dropped the items' letters: they stay in paragraph 1, and the reviewer is told.
    assert "the right to be presumed innocent" in span(text, nodes["art_7__para_1"])
    assert codes(result) == ["unlettered_items"]
    assert resolve(result, "art. 7(2)") is nodes["art_7__para_2"]
    spans_nest(text, result)


def test_the_french_charter_numbers_its_parts_in_words_and_chapters_restart_in_each():
    text, result = build(CHARTER_FR, structure.AU_TREATY, "fr")
    nodes = by_locator(result)
    assert (
        nodes["part_1"].label == "PREMIERE PARTIE" and nodes["part_1"].heading == "DES DROITS ET DES DEVOIRS"
    )
    assert nodes["part_2__chap_i"].heading.startswith("DE LA COMPOSITION")
    assert nodes["art_30"].parent is nodes["part_2__chap_i"]
    assert resolve(result, "art. 30") is nodes["art_30"]
    assert codes(result) == []
    spans_nest(text, result)


def test_a_protocols_article_heading_follows_its_number_and_the_enacting_words_end_the_preamble():
    text, result = build(PROTOCOL_PT, structure.AU_TREATY, "pt")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).endswith("ACORDARAM NO SEGUINTE:")
    assert (nodes["art_1"].label, nodes["art_1"].heading) == ("ARTIGO 1", "CRIAÇÃO DO TRIBUNAL")
    assert nodes["art_3__para_2"].label == "2."
    assert resolve(result, "art. 3(1)") is nodes["art_3__para_1"]
    spans_nest(text, result)


def test_an_edition_not_calibrated_is_refused_never_read_by_another_languages_patterns():
    _, result = build(PROTOCOL_PT, structure.AU_TREATY, "ar")
    assert "no_structure_rules" in codes(result) and result.errors
    # Without its language, a text of a family of several editions is held until it is set; a family of
    # one reads it by that one.
    _, unknown = build(PROTOCOL_PT, structure.AU_TREATY, None)
    assert [i.message for i in unknown.errors] == [
        "aa-au/charter or protocol is read in en, fr, pt: set the document's language"
    ]
    _, ivorian = build(CI, structure.CI_LAW, None)
    assert not ivorian.errors and "art_54" in by_locator(ivorian)


# --- ecowas-act-1 ------------------------------------------------------------------------------------

# Supplementary Act A/SA.1/01/10 on Personal Data Protection within ECOWAS (Abuja, 16 February 2010), as
# ECOWAS publishes it signed: the logo text printed on every page, a definition printed as a heading, and
# the signatures.
ECOWAS = [
    [
        head("SUPPLEMENTARY ACT A/SA.1/01/10 ON PERSONAL DATA PROTECTION WITHIN ECOWAS"),
        para("THE HIGH CONTRACTING PARTIES, MINDFUL of Articles 7, 8 and 9 of the ECOWAS Treaty as revised"),
        head("HEREBY AGREE AS FOLLOWS:"),
        head("CHAPTER I"),
        head("GENERAL PROVISIONS"),
        head("Article 1: Definitions"),
        para("For the purposes of this Supplementary Act:"),
        head("Authority of Protection"),
        para("The data protection authority shall be an independent administrative authority"),
    ],
    [
        para("CEDEAO ECOWAS and revision proposals on expiry of a period of three (3) months"),
        head("Article 49: Depository authority"),
        para(
            "This Supplementary Act shall be deposited with the Commission which shall transmit certified true copies"
        ),
    ],
    [para("CEDEAO ECOWAS H. E. Mr. Jean Marie EHOUZOU Minister of Foreign Affairs, African Integration")],
]


def test_an_ecowas_act_keeps_a_heading_inside_an_article_and_its_signatures_are_a_notice():
    text, result = build(ECOWAS, structure.ECOWAS_ACT, "en")
    nodes = by_locator(result)
    article = nodes["art_1"]
    assert (article.label, article.heading, article.parent) == ("Article 1", "Definitions", nodes["chap_i"])
    assert "independent administrative authority" in span(text, article)  # the defined term's words too
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
        head("PARTIE OFFICIELLE"),
        head("PRESIDENCE DE LA REPUBLIQUE"),
        para(
            "LOI n° 2013-450 du 19 juin 2013 relative à la protection des données à caractère personnel. "
            "L'ASSEMBLEE NATIONALE a adopté, LE PRESIDENT DE LA REPUBLIQUE promulgue la loi dont la teneur suit :"
        ),
        head("CHAPITRE 1"),
        head("Définitions"),
        para("Article premier. — Les définitions des instruments juridiques de la CEDEAO sont applicables."),
        para(
            "— destinataire d'un traitement des données à caractère personnel, toute personne habilitée",
            "list_item",
        ),
        para(
            "Art. 2. — La présente loi a pour objet de régir la protection des données à caractère personnel."
        ),
    ],
    [
        para(
            "Art. 54. — La présente loi sera publiée au Journal officiel de la République de Côte d'Ivoire."
        ),
        para("Fait à Abidjan, le 19 juin 2013. Alassane OUATTARA."),
        para("DECRET n° 2012-525 du 20 juin 2012 portant composition du conseil d'administration"),
        para(
            "Article premier. — Le conseil d'administration de la société d'Etat dénommée BNETD est composé :"
        ),
    ],
]


def test_an_ivorian_law_counts_its_alineas_and_its_closing_words_hold_the_next_act():
    text, result = build(CI, structure.CI_LAW, "fr")
    nodes = by_locator(result)
    assert result.root.heading.startswith("LOI n° 2013-450")
    first = nodes["art_1"]
    assert (first.label, first.parent) == ("Article premier", nodes["chap_1"])
    assert nodes["chap_1"].heading == "Définitions"
    assert span(text, nodes["art_1__al_1"]).startswith("Les définitions")  # after "Article premier. —"
    assert span(text, nodes["art_1__al_2"]).startswith("— destinataire")
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
    "PRESIDENCE DE LA REPUBLIQUE PRESIDENCY OF THE REPUBLIC SECRETARIAT GENERAL SERVICE DU FICHIER LEGISLATIF"
)
CM_EN = [
    [
        head("RELATING TO PERSONAL DATA PROTECTION IN CAMEROON"),
        para(
            "The Parliament deliberated and adopted, the President of the Republic hereby enacts the law set out below:"
        ),
    ],
    [
        para(STAMP),
        head("PART II"),
        head("LEGAL REGIME OF THE PROCESSING OF PERSONAL DATA"),
        head("CHAPTER I"),
        head("PERSONAL DATA PROCESSING PRINCIPLES"),
        para("SECTION 6: The processing of personal data shall respect individual privacy."),
        para(
            "SECTION 8: (1) The processing of personal data shall be subject to the prior consent of the data subject."
        ),
        para(
            "(2) However, the requirement of prior consent may be waived where such processing is necessary in order to:"
        ),
        para("comply with an obligation that is legally binding upon the controller;", "list_item"),
    ],
]
CM_FR = [
    [
        head("LOI N° 2024/017 DU 23 DEC 2024"),
        head("RELATIVE A LA PROTECTION DES DONNEES A CARACTERE PERSONNEL AU CAMEROUN"),
        para(
            "Le Parlement a délibéré et adopté, le Président de la République promulgue la loi dont la teneur suit :"
        ),
    ],
    [
        head("TITRE II"),
        head("DU REGIME JURIDIQUE DU TRAITEMENT DES DONNEES A CARACTERE PERSONNEL"),
        head("CHAPITRE II DES FORMALITES PREALABLES"),
        para(
            "ARTICLE 19.- (1) Le traitement des données à caractère personnel est soumis à autorisation préalable."
        ),
        para(
            "(2) Toute interconnexion de fichiers contenant des données sensibles relatives aux mineurs est soumise"
        ),
    ],
]


def test_cameroons_english_text_prints_sections_with_subsections_and_a_stamp_on_every_page():
    text, result = build(CM_EN, structure.CM_LAW, "en")
    nodes = by_locator(result)
    assert result.root.heading == "RELATING TO PERSONAL DATA PROTECTION IN CAMEROON"
    assert nodes["part_ii__chap_i"].heading == "PERSONAL DATA PROCESSING PRINCIPLES"
    section = nodes["sec_8"]
    assert (section.kind, section.label, section.parent) == ("section", "8", nodes["part_ii__chap_i"])
    assert span(text, nodes["sec_8__subsec_1"]).startswith("(1) The processing")  # after "SECTION 8:"
    assert "legally binding" in span(text, nodes["sec_8__subsec_2"])
    assert resolve(result, "s. 8(2)") is nodes["sec_8__subsec_2"]
    assert codes(result) == ["running_head", "unlettered_items"]
    spans_nest(text, result)


def test_cameroons_french_text_prints_articles_and_numbered_paragraphs_in_titles_and_chapters():
    text, result = build(CM_FR, structure.CM_LAW, "fr")
    nodes = by_locator(result)
    chapter = nodes["title_ii__chap_ii"]
    assert (chapter.label, chapter.heading) == ("CHAPITRE II", "DES FORMALITES PREALABLES")
    assert nodes["art_19"].parent is chapter and nodes["art_19"].label == "ARTICLE 19"
    assert span(text, nodes["art_19__para_1"]).startswith("(1) Le traitement")
    assert resolve(result, "art. 19(2)") is nodes["art_19__para_2"]
    assert codes(result) == []
    spans_nest(text, result)


# --- ao-lei-1 -----------------------------------------------------------------------------------------

# Lei n.º 22/11 de 17 de Junho, da Protecção de Dados Pessoais, as the Diário da República prints it: the
# issue's front matter, the law's preamble, a chapter and its section, articles with the heading in
# brackets on the next line (or before the first words), números and alíneas, and the closing words.
AO = [
    [
        head("DIÁRIO DA REPÚBLICA"),
        head("SUMÁRIO"),
        para(
            "Lei n.º 22/11: Da Protecção de Dados Pessoais. — Revoga toda a legislação que contrarie a presente lei."
        ),
        head("Lei n.º 22/11"),
        head("de 17 de Junho"),
        para(
            "A protecção dos dados pessoais, da confidencialidade e da reserva da vida privada assume uma relevância"
        ),
        head("CAPÍTULO II"),
        head("Princípios e Condições de Legitimidade"),
        head("SECÇÃO I"),
        head("Princípios Gerais"),
        head("ARTIGO 3.º"),
        para("(Âmbito de aplicação subjectiva e territorial)"),
        para(
            "Estão sujeitos à presente lei os tratamentos de dados pessoais efectuados",
            "list_item",
            marker="1.",
        ),
        para("A presente lei aplica-se ao tratamento de dados pessoais efectuado:", "list_item", marker="2."),
        para("a) por responsável pelo tratamento sedeado na República de Angola;", "list_item"),
        head("ARTIGO 67.º"),
        para(
            "(Entrada em vigor) A presente lei entra em vigor à data da sua publicação. Vista e aprovada pela "
            "Assembleia Nacional, em Luanda, aos 24 de Maio de 2011."
        ),
    ]
]


def test_an_angolan_law_has_bracketed_article_headings_numeros_and_alineas():
    text, result = build(AO, structure.AO_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei n.º 22/11"
    assert span(text, nodes["preamble"]).startswith("de 17 de Junho")
    section = nodes["chap_ii__dvs_i"]
    assert (section.label, section.heading) == ("SECÇÃO I", "Princípios Gerais")
    article = nodes["art_3"]
    assert (article.label, article.heading, article.parent) == (
        "ARTIGO 3",
        "Âmbito de aplicação subjectiva e territorial",
        section,
    )
    assert nodes["art_3__para_2__subpara_a"].label == "a)"
    assert resolve(result, "art. 3(2)(a)") is nodes["art_3__para_2__subpara_a"]
    assert nodes["art_67"].heading == "Entrada em vigor"
    assert codes(result) == []
    spans_nest(text, result)


def test_the_families_are_read_by_jurisdiction_and_type_and_nothing_else():
    rules = structure.rules_for
    assert rules({"jurisdiction": "aa-au", "document_type": "charter"}) == structure.AU_TREATY
    assert rules({"jurisdiction": "aa-au", "document_type": "protocol"}) == structure.AU_TREATY
    assert rules({"jurisdiction": "aa-au", "document_type": "convention"}) is None  # not yet calibrated
    assert rules({"jurisdiction": "aa-ecowas", "document_type": "supplementary_act"}) == structure.ECOWAS_ACT
    assert rules({"jurisdiction": "aa-ecowas", "document_type": "directive"}) is None
    assert rules({"jurisdiction": "ci", "document_type": "law"}) == structure.CI_LAW
    assert rules({"jurisdiction": "cm", "document_type": "law"}) == structure.CM_LAW
    assert rules({"jurisdiction": "ao", "document_type": "law"}) == structure.AO_LAW
