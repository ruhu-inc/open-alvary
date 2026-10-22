"""Structure rules for texts of articles (TXP-03, ArticleBuilder): the AU's charters, protocols and
conventions, ECOWAS's Supplementary Acts, directives and regulations, and the laws of Côte d'Ivoire,
Senegal, Burkina Faso, Cameroon, Angola, Mozambique and Cape Verde. Each family is tried on short excerpts
of an official text it was calibrated on, reviewed units as the workbench holds them after the cloud
reading (ingestion.calibrate): its front matter, preamble, divisions, articles, paragraphs or alinéas,
quoted text, closing words, annexes and any stamp, and pinpoints resolved to their part.

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


# African Union Convention on Cyber Security and Personal Data Protection (Malabo, 27 June 2014), in
# English as the AU publishes it (au.int, treaty 0048): the cover's title, the preamble, an article before
# the first chapter, sections in each chapter, each article's heading on the line after its number, and
# the adoption.
MALABO_EN = [
    [head("AFRICAN UNION CONVENTION ON CYBER SECURITY AND PERSONAL DATA PROTECTION")],
    [
        head("PREAMBLE"),
        para("The Member States of the African Union: Guided by the Constitutive Act of the African Union"),
        para("reflected primarily by the need for appropriate criminal legislation. HAVE AGREED AS FOLLOWS:"),
        head("Article 1"),
        head("Definitions"),
        para("For the purposes of this Convention:"),
    ],
    [
        head("CHAPTER I"),
        head("ELECTRONIC TRANSACTIONS"),
        head("Section I: Electronic Commerce"),
        head("Article 2"),
        head("Scope of application of electronic commerce"),
        para(
            "States Parties shall ensure that e-commerce activities are exercised freely in their territories"
        ),
        head("Section II: Contractual Obligations in Electronic Form"),
        head("Article 5"),
        head("Electronic contracts"),
        para("The information requested for the purpose of concluding a contract", "list_item", marker="1."),
        para("A service provider or supplier, who offers goods and services", "list_item", marker="2."),
    ],
    [
        head("CHAPTER II"),
        head("PERSONAL DATA PROTECTION"),
        head("Section I: Personal data protection"),
        head("Article 8"),
        head("Objective of this Convention with respect to personal data"),
        para("Each State Party shall commit itself to establishing a legal framework"),
        para("ADOPTED BY THE TWENTY-THIRD ORDINARY SESSION OF THE ASSEMBLY, HELD IN MALABO"),
    ],
]


def test_an_au_convention_has_sections_in_each_chapter_and_its_article_headings_on_the_next_line():
    text, result = build(MALABO_EN, structure.AU_TREATY, "en")
    nodes = by_locator(result)
    assert result.root.heading.startswith("AFRICAN UNION CONVENTION ON CYBER SECURITY")
    assert span(text, nodes["preamble"]).endswith("HAVE AGREED AS FOLLOWS:")
    assert (nodes["art_1"].heading, nodes["art_1"].parent) == ("Definitions", result.root)
    chapter, section = nodes["chap_i"], nodes["chap_i__dvs_ii"]
    assert (section.label, section.heading, section.parent) == (
        "Section II",
        "Contractual Obligations in Electronic Form",
        chapter,
    )
    assert nodes["chap_ii__dvs_i"].parent is nodes["chap_ii"]  # sections restart in each chapter
    article = nodes["art_5"]
    assert (article.heading, article.parent) == ("Electronic contracts", section)
    assert resolve(result, "art. 5(2)") is nodes["art_5__para_2"]
    assert span(text, nodes["notice_1"]).startswith("ADOPTED BY")
    assert codes(result) == []
    spans_nest(text, result)


# The same Convention in Portuguese: "Secção" in its chapters, "Artigo 1º" and "Artigo 2: …", and the
# adoption, "ADOPTADA PELA …".
MALABO_PT = [
    [
        head("CONVENÇÃO DA UNIÃO AFRICANA SOBRE CIBERSEGURANÇA E PROTECÇÃO DE DADOS PESSOAIS"),
        head("PREÂMBULO"),
        para("Os Estados-membros da União Africana; Guiados pelo Acto Constitutivo da União Africana"),
        para("ACORDARAM NO SEGUINTE:"),
        head("Artigo 1º"),
        head("Definições"),
        head("CAPÍTULO I"),
        head("TRANSACÇÕES ELECTRÓNICAS"),
        head("Secção I: Comércio Electrónico"),
        head("Artigo 2: Âmbito de aplicação do comércio electrónico"),
        para("Os Estados-membros devem garantir que as actividades do comércio electrónico", "list_item"),
        para("ADOPTADA PELA VIGÉSIMA TERCEIRA SESSÃO ORDINÁRIA DA CIMEIRA, REALIZADA EM MALABO"),
    ]
]


def test_the_portuguese_convention_prints_seccao_and_closes_with_its_adoption():
    text, result = build(MALABO_PT, structure.AU_TREATY, "pt")
    nodes = by_locator(result)
    assert (nodes["art_1"].label, nodes["art_1"].heading) == ("Artigo 1", "Definições")
    section = nodes["chap_i__dvs_i"]
    assert (section.label, section.heading) == ("Secção I", "Comércio Electrónico")
    assert nodes["art_2"].heading == "Âmbito de aplicação do comércio electrónico"
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


# --- mz-lei-1 -----------------------------------------------------------------------------------------

# Lei n.º 13/2026, de 1 de Julho, Lei de Segurança Cibernética, as the Boletim da República (I Série,
# número 123) prints it, from the Instituto Nacional das Comunicações de Moçambique: the issue's summary
# listing both its laws, the law's title and preamble, a chapter of subsections, articles with the heading
# in brackets below, the closing words the reading kept with the last article, the law's annexes after its
# signatures, and the next law in the issue.
MZ = [
    [
        head("BOLETIM DA REPÚBLICA"),
        head("SUMÁRIO"),
        para("Assembleia da República: Lei n.º 13/2026: Aprova a Lei de Segurança Cibernética."),
        head("ASSEMBLEIA DA REPÚBLICA"),
        head("Lei n.º 13/2026"),
        para("de 1 de Julho Havendo necessidade de estabelecer o regime jurídico da Segurança Cibernética"),
        head("CAPÍTULO II"),
        head("Organização do Sistema Nacional de Segurança Cibernética"),
        head("ARTIGO 6"),
        head("(Estrutura)"),
        para("O Sistema Nacional de Segurança Cibernética é composto por órgãos e entidades"),
        para("a) Órgãos:", "list_item"),
        para("i. Conselho Nacional de Segurança Cibernética;", "list_item"),
        head("SUBSECÇÃO I"),
        head("Conselho Nacional de Segurança Cibernética"),
        head("ARTIGO 7"),
        head("(Natureza)"),
        para("O Conselho Nacional de Segurança Cibernética, abreviadamente designado por CNSC"),
    ],
    [
        head("ARTIGO 80"),
        head("(Entrada em vigor)"),
        para("A presente Lei entra em vigor 90 dias após a sua publicação."),
        para("Aprovada pela Assembleia da República, aos 29 de Abril de 2026."),
        head("Anexo I"),
        head("Glossário"),
        para("Activo de Informação – todo elemento que agrega valor ao negócio"),
        head("Anexo II"),
        head("Sectores, subsectores e tipos de entidades dos operadores de serviços essenciais"),
        head("ASSEMBLEIA DA REPÚBLICA"),
        para("Lei n.º 14/2026 de 1 de Julho Havendo necessidade de estabelecer o regime jurídico dos crimes"),
        head("ARTIGO 1"),
        head("(Objecto)"),
    ],
]


def test_a_mozambican_law_holds_its_annexes_after_its_signatures_and_the_next_law_is_a_notice():
    text, result = build(MZ, structure.MZ_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei n.º 13/2026"  # never the summary's "Lei n.º 13/2026:"
    assert span(text, nodes["preamble"]).startswith("de 1 de Julho Havendo")
    division = nodes["chap_ii__subdvs_i"]  # a subsection straight inside the chapter, as printed
    assert (division.label, division.parent) == ("SUBSECÇÃO I", nodes["chap_ii"])
    assert (nodes["art_7"].heading, nodes["art_7"].parent) == ("Natureza", division)
    item = nodes["art_6__subpara_a__subpara_i"]  # "a)" then "i."
    assert item.label == "i." and resolve(result, "art. 6(a)(i)") is item
    assert span(text, nodes["notice_1"]).startswith("Aprovada pela Assembleia")
    annex = nodes["annex_i"]
    assert (annex.kind, annex.label, annex.heading, annex.parent) == (
        "annex",
        "Anexo I",
        "Glossário",
        result.root,
    )
    assert nodes["annex_ii"].heading.startswith("Sectores, subsectores")
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
    [para("REPÚBLICA DE CABO VERDE"), head("BOLETIM OFICIAL")],
    [
        head("ASSEMBLEIA NACIONAL"),
        head("Lei nº 120/IX/2021"),
        head("de 17 de março"),
        head("PREÂMBULO"),
        para("A Lei nº 42/VIII/2013, de 17 de setembro, que ora se propõe alterar, regula a composição"),
        head("Artigo 1.º"),
        head("Objeto"),
        para("A presente lei procede à alteração da Lei nº 42/VIII/2013, de 17 de setembro"),
        head("Artigo 2.º"),
        head("Alterações"),
        para(
            "A presente lei altera os artigos 2.º, 10.º, 12.º, 13.º, 18.º, 27.º e 29.º, que passam a ter a "
            "seguinte redação:"
        ),
        para(
            "«Artigo 2.º [...] A CNPD é uma entidade administrativa independente, com personalidade jurídica"
        ),
        para("Artigo 10.º [...] 1. [...] [...] e) Fixar o tempo da conservação dos dados pessoais"),
        para(
            "A CNPD deve ser consultada para emitir parecer sobre quaisquer iniciativas",
            "list_item",
            marker="1.",
        ),
    ],
    [
        head("Artigo 29.º"),
        para("[...]"),
        para("c) Revogada;", "list_item"),
        head("Artigo 32.º"),
        head("Competências e substituição do presidente"),
        para("f) Convocar as reuniões e fixar a ordem de trabalhos;", "list_item"),
        para("2. [...]»"),
        head("Artigo 3.º"),
        head("Revogação"),
        para("São revogados os artigos 29.º, alínea c), 47.º e 50.º."),
        head("Artigo 5.º"),
        head("Entrada em vigor"),
        para("A presente lei entra em vigor trinta dias após a sua publicação."),
        para(
            "Aprovada em 11 de fevereiro de 2021. O Presidente da Assembleia Nacional, Jorge Pedro "
            "Maurício dos Santos Promulgada em 1 de março de 2021. Publique-se."
        ),
        head("REPUBLICAÇÃO"),
        para("Lei nº 42/VIII/2013, de 17 de setembro"),
        head("CAPÍTULO I"),
        head("Disposições gerais"),
        head("Artigo 1.º"),
        head("Objeto"),
        para("A presente lei regula a composição, a competência, a organização e o funcionamento da CNPD"),
    ],
    [
        head("CAPÍTULO II"),
        head("Atribuições e competências"),
        para("Artigo 8.º"),
        head("Atribuições"),
        para("1. A CNPD é a autoridade nacional à qual incumbe controlar e fiscalizar o cumprimento"),
        head("CAPÍTULO II"),
        head("Organização e estatuto dos membros"),
        head("Secção I"),
        head("Composição, mandato e posse"),
        head("Artigo 13.º"),
        head("Composição e eleição"),
        para("A CNPD é composta por três personalidades de reconhecida competência e integridade moral"),
    ],
    [
        para("Artigo 49.º"),
        head("Entrada em vigor"),
        para("O presente diploma entra em vigor no dia seguinte ao da sua publicação"),
        para("Aprovada em 24 de julho de 2013. O Presidente da Assembleia Nacional, Basílio Mosso Ramos"),
        head("Lei nº 121 /IX//2021"),
        head("de 17 de março"),
        head("Artigo 1.º"),
        head("Objeto"),
    ],
]


def test_a_cape_verdean_amending_law_quotes_what_it_replaces_and_republishes_the_law_in_an_annex():
    text, result = build(CV_120, structure.CV_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei nº 120/IX/2021"
    assert words(text, nodes["preamble"]).startswith("de 17 de março PREÂMBULO")
    articles = [n for n in result.nodes if n.kind == "article"]
    assert [n.locator for n in articles if n.parent.kind != "annex" and "annex" not in n.locator] == [
        "art_1",
        "art_2",
        "art_3",
        "art_5",
    ]
    assert (nodes["art_2"].heading, nodes["art_3"].heading) == ("Alterações", "Revogação")
    # The articles article 2 quotes are its words, to the closing », never the law's own articles.
    quotation = nodes["art_2__quot_1"]
    quoted = words(text, quotation)
    assert (quotation.kind, quotation.parent) == ("quotation", nodes["art_2"])
    assert quoted.startswith("«Artigo 2.º [...]") and quoted.endswith("2. [...]»")
    assert not quotation.children and "art_29" not in nodes and "art_32" not in nodes
    assert words(text, nodes["art_2"]).startswith("Artigo 2.º Alterações A presente lei altera")
    assert words(text, nodes["notice_1"]).startswith("Aprovada em 11 de fevereiro de 2021.")
    annex = nodes["annex_1"]
    assert (annex.kind, annex.label, annex.heading, annex.parent) == (
        "annex",
        "REPUBLICAÇÃO",
        "Lei nº 42/VIII/2013, de 17 de setembro",
        result.root,
    )
    # The republished law's articles restart, qualified by the annex; its chapters nest in it.
    first = nodes["annex_1__art_1"]
    assert (first.label, first.heading, first.parent) == ("Artigo 1", "Objeto", nodes["annex_1__chap_i"])
    assert nodes["annex_1__chap_i"].parent is annex
    assert nodes["annex_1__art_8"].heading == "Atribuições"
    assert nodes["annex_1__art_8__para_1"].label == "1."
    assert nodes["annex_1__chap_ii_2"].heading == "Organização e estatuto dos membros"  # printed twice
    assert nodes["annex_1__art_13"].parent is nodes["annex_1__chap_ii_2__dvs_i"]
    # That law's own closing words are the annex's; the next law in the issue is a notice.
    assert "Aprovada em 24 de julho" not in words(text, nodes["annex_1__art_49"])
    assert words(text, annex).endswith("Basílio Mosso Ramos")
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
        head("de 17 de março"),
        head("PREÂMBULO"),
        para("1. O regime jurídico geral de proteção de dados pessoais das pessoas singulares"),
        head("Artigo 1.º"),
        head("Objeto"),
        para("A presente lei procede à segunda alteração do regime jurídico geral de proteção de dados"),
        head("Artigo 2.º"),
        head("Alterações"),
        para(
            "São alterados os artigos 2.º, 4.º, 5.º, 6.º, 8.º, 9.º, 10.º, 11.º, 12.º, 13.º, 14.º, 15.º, "
            "26.º e 41.º, que passam a ter a seguinte redação:"
        ),
        para("Artigo 2.º [...]"),
        para(
            "A presente lei aplica-se ao tratamento de dados pessoais por meios total",
            "list_item",
            marker="1.",
        ),
        para("Artigo 4.º [...] O tratamento de dados pessoais deve processar-se no estrito respeito"),
        para("Artigo 8.º (Tratamento de dados especiais)"),
        para("3. [...]"),
    ],
    [
        head("Artigo 3.º"),
        head("Aditamentos"),
        para(
            "São aditados os artigos 8.º -A e 8.º - B, 12.º -A, 44.º -C e 44.º - D, com a seguinte redação:"
        ),
        para("Artigo 8.º -A"),
        head("(Condições aplicáveis ao consentimento)"),
        para("1. Quando o tratamento for realizado com base no consentimento, o responsável pelo tratamento"),
        para(
            "2. A pena é agravada para o dobro nos seus limites se da inserção referida no número anterior "
            'resultar prejuízo efetivo".'
        ),
        para(
            "Artigo 4.º Revogação É revogado o artigo 48.º da Lei nº 133/V/2001, de 22 de janeiro, "
            "alterada pela Lei nº 41/VIII/2013, de 17 de setembro."
        ),
        para("Artigo 6.º Entrada em vigor A presente lei entra em vigor trinta dias após a sua publicação."),
        para(
            "Aprovada em 11 de fevereiro de 2021. O Presidente da Assembleia Nacional, Jorge Pedro Maurício"
        ),
        para(
            "REPUBLICAÇÃO Lei nº 133/V/2001, de 22 de janeiro, alterada pela Lei nº 41/VIII/2013, de 17 de "
            "setembro, que estabelece o regime jurídico geral de proteção de dados pessoais"
        ),
        head("CAPÍTULO I"),
        head("Disposições gerais"),
        para("Artigo 1.º (Objeto) A presente lei estabelece o regime jurídico geral de proteção de dados"),
        para("Artigo 2.º"),
        head("(Âmbito de aplicação)"),
        para("1. A presente lei aplica-se ao tratamento de dados pessoais por meios total ou parcialmente"),
    ],
    [
        head("CAPÍTULO VI"),
        head("Recursos judiciais, responsabilidade civil, infrações e sanções"),
        head("Secção II"),
        head("Infrações e sanções"),
        head("Subsecção"),
        head("Contraordenações"),
        para("Artigo 48.º (Legislação subsidiária) Às infrações previstas na presente subsecção é"),
        head("Subsecção II"),
        head("Crimes"),
        head("Artigo 59.º"),
        head("(Não cumprimento de obrigações relativas a proteção de dados)"),
        head("Artigo 71.º"),
        head("(Entrada em vigor)"),
        para("A presente lei entra em vigor trinta dias após a sua publicação."),
        para("Aprovada em 20 de dezembro de 2000. O Presidente da Assembleia Nacional, António do Espírito"),
        head("CONSELHO DE MINISTROS"),
        head("Decreto-lei nº 21/2021"),
        head("de 17 de março"),
        head("Artigo 1º"),
        head("Objeto"),
        para("Anexo (A que se refere o artigo 1º) Zona de Desenvolvimento Turístico Integrado (ZDTI)"),
    ],
]


def test_a_quotation_without_marks_ends_at_the_amending_laws_next_article_or_its_closing_mark():
    text, result = build(CV_121, structure.CV_LAW, "pt")
    nodes = by_locator(result)
    assert result.root.heading == "Lei nº 121 /IX//2021"
    own = [n.locator for n in result.nodes if n.kind == "article" and not n.locator.startswith("annex_")]
    assert own == ["art_1", "art_2", "art_3", "art_4", "art_6"]  # the quoted articles 2, 4, 8 and 8-A are not
    altered, added = words(text, nodes["art_2__quot_1"]), words(text, nodes["art_3__quot_1"])
    assert altered.startswith("Artigo 2.º [...] 1. A presente lei") and altered.endswith("3. [...]")
    assert added.startswith("Artigo 8.º -A (Condições") and added.endswith('efetivo".')  # its closing mark
    assert (nodes["art_3__quot_1"].parent, nodes["art_3"].heading) == (nodes["art_3"], "Aditamentos")
    # "Artigo 4.º Revogação É revogado …": the heading is not cut from the words, and the reviewer is told.
    assert nodes["art_4"].heading == "" and words(text, nodes["art_4"]).startswith("Artigo 4.º Revogação É")
    assert words(text, nodes["notice_1"]).startswith("Aprovada em 11 de fevereiro")
    annex = nodes["annex_1"]
    assert (annex.label, annex.heading[:31]) == ("REPUBLICAÇÃO", "Lei nº 133/V/2001, de 22 de jan")
    first = nodes["annex_1__art_1"]
    assert first.heading == "Objeto" and words(text, first).startswith("Artigo 1.º (Objeto) A presente lei")
    assert nodes["annex_1__art_2"].heading == "Âmbito de aplicação" and "annex_1__art_2__para_1" in nodes
    # "Subsecção" without its number is numbered by its place; "Subsecção II" follows it.
    unnumbered, inside = nodes["annex_1__chap_vi__dvs_ii__subdvs_1"], nodes["annex_1__art_48"]
    assert (unnumbered.label, unnumbered.heading) == ("Subsecção", "Contraordenações")
    assert (inside.parent, inside.heading) == (unnumbered, "Legislação subsidiária")
    assert nodes["annex_1__chap_vi__dvs_ii__subdvs_ii"].heading == "Crimes"
    assert "Aprovada em 20 de dezembro" not in words(text, nodes["annex_1__art_71"])
    # The issuing body's heading opens the decree printed next; its "Anexo" is the decree's, not the law's.
    assert words(text, nodes["notice_2"]).startswith("CONSELHO DE MINISTROS Decreto-lei nº 21/2021")
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
            head("Alterações"),
            para("A presente lei altera o artigo 2.º, que passa a ter a seguinte redação:"),
            para("«Artigo 2.º [...] A CNPD é uma entidade administrativa independente"),
            head("Artigo 3.º"),
            head("Revogação"),
            para("Aprovada em 11 de fevereiro de 2021."),
        ]
    ]
    text, result = build(pages, structure.CV_LAW, "pt")
    nodes = by_locator(result)
    assert "art_3" not in nodes and words(text, nodes["art_2__quot_1"]).endswith("Artigo 3.º Revogação")
    assert codes(result) == ["quotation_unclosed"] and "notice_1" in nodes
    spans_nest(text, result)


# --- sn-loi-1 -----------------------------------------------------------------------------------------

# Loi n° 2008-12 du 25 janvier 2008 sur la protection des données à caractère personnel, as the Commission
# de protection des données personnelles publishes it (cdp.sn): the title, the exposé des motifs and the
# enacting words before "Chapitre premier", sections in each chapter, articles headed "Article 2." with
# their words below, and articles the reading kept in one block with their words ("Article 3. La …").
SN = [
    [
        head("LOI n° 2008-12 du 25 janvier 2008 portant sur la Protection des données à caractère personnel"),
        head("EXPOSE DES MOTIFS"),
        para("Avec le développement de l'informatique et de ses applications, le domaine traditionnel"),
    ],
    [
        para("Le Président de la République promulgue la loi sont la teneur suit :"),
        head("Chapitre premier. - Dispositions générales."),
        head("Section première. - Objet de la loi sur les données à caractère personnel."),
        head("Article Premier."),
        para("La présente loi a pour objet de mettre en place un dispositif permettant de lutter contre"),
        head("Section II. - Champ d'application de la loi sur les données à caractère personnel"),
        head("Article 2."),
        para("Sont soumis à la présente loi :"),
        para(
            "Toute collecte, tout traitement, toute transmission, tout stockage et toute utilisation",
            "list_item",
        ),
        para("Article 3. La présente loi ne s'applique pas :"),
        para("aux traitements de données mis en œuvre par une personne physique dans le cadre", "list_item"),
    ],
]

# Loi n° 2008-08 du 25 janvier 2008 sur les transactions électroniques, from the same publisher: TITRE
# PREMIER, articles opening with their words after a dash ("Art. 2. - …"), a chapter heading and the
# enacting formula each split from the article before them by the reading.
SN_TRANSACTIONS = [
    [
        head("LOI n° 2008-08 du 25 janvier 2008 sur les transactions électroniques"),
        head("TITRE PREMIER. - LIBERTE DE COMMUNICATION PAR VOIE ELECTRONIQUE."),
        head("Chapitre premier. - Communication au public par voie électronique."),
        para("Art. 2. - Au sens de la présente loi, on entend par :"),
    ],
    [
        head("TITRE IV. – Transmission par voie électronique des documents ou actes administratifs."),
        para("Art. 53. – Tout document électronique envoyé par un candidat dans lequel un virus"),
        para("Chapitre II. – Recommandé électronique."),
        para("Art. 54. – Le message signé électroniquement sur la base d'un certificat numérique"),
        para("La présente loi sera exécutée comme loi de l'Etat."),
    ],
]


def test_a_senegalese_law_numbers_its_first_chapter_and_section_in_words_and_counts_alineas():
    text, result = build(SN, structure.SN_LAW, "fr")
    nodes = by_locator(result)
    assert result.root.heading.startswith("LOI n° 2008-12")
    assert span(text, nodes["preamble"]).startswith("EXPOSE DES MOTIFS")  # the motives, then enacting words
    chapter, section = nodes["chap_1"], nodes["chap_1__dvs_1"]
    assert (chapter.label, chapter.heading) == ("Chapitre premier", "Dispositions générales.")
    assert section.label == "Section première" and nodes["chap_1__dvs_ii"].parent is chapter
    assert (nodes["art_1"].label, nodes["art_1"].parent) == ("Article Premier", section)
    assert span(text, nodes["art_2__al_2"]).startswith("Toute collecte")
    assert span(text, nodes["art_3__al_1"]).startswith("La présente loi ne s'applique")
    assert resolve(result, "art. 3, al. 2") is nodes["art_3__al_2"]
    assert codes(result) == []
    spans_nest(text, result)


def test_a_senegalese_laws_titles_restart_its_chapters_and_its_enacting_formula_closes_it():
    text, result = build(SN_TRANSACTIONS, structure.SN_LAW, "fr")
    nodes = by_locator(result)
    assert nodes["title_1__chap_1"].parent is nodes["title_1"] and nodes["title_1"].label == "TITRE PREMIER"
    assert span(text, nodes["art_2__al_1"]).startswith("Au sens")  # after "Art. 2. - "
    assert nodes["art_54"].parent is nodes["title_iv__chap_ii"]  # a paragraph printed as a heading is one
    assert span(text, nodes["notice_1"]) == "La présente loi sera exécutée comme loi de l'Etat."
    assert codes(result) == []
    spans_nest(text, result)


# --- bf-loi-1 -----------------------------------------------------------------------------------------

# Loi n° 001-2021/AN du 30 mars 2021 portant protection des personnes à l'égard du traitement des données à
# caractère personnel, as the Assemblée nationale publishes it: its cover, the title, the "Vu" and
# enacting words, TITRE and CHAPITRE numbered in figures, "Article 1 :" with its alinéas below, and the
# closing words the reading kept with the last article.
BF = [
    [
        para("BURKINA FASO ----------- UNITE-PROGRES-JUSTICE ----------- ASSEMBLEE NATIONALE IVE REPUBLIQUE"),
        head("LOI N°001-2021/AN"),
        head("PORTANT PROTECTION DES PERSONNES A L'EGARD DU TRAITEMENT DES DONNEES A CARACTERE PERSONNEL"),
    ],
    [
        head("L'ASSEMBLEE NATIONALE"),
        para(
            "Vu la Constitution ; a délibéré en sa séance du 30 mars 2021 et adopté la loi dont la teneur suit :"
        ),
    ],
    [
        head("TITRE I : DISPOSITIONS GENERALES"),
        head("CHAPITRE 1 : OBJET ET CHAMP D'APPLICATION"),
        head("Article 1 :"),
        para("La présente loi a pour objet de protéger les droits et libertés fondamentaux des personnes"),
        head("Article 4 :"),
        para("La présente loi ne s'applique pas aux :"),
        para("traitements effectués par une personne physique pour l'exercice d'activités", "list_item"),
    ],
    [
        head("TITRE V : DISPOSITIONS DIVERSES, TRANSITOIRES ET FINALES"),
        head("Article 83 :"),
        para("La présente loi sera exécutée comme loi de l'Etat."),
        para("Ainsi fait et délibéré en séance publique à Ouagadougou, le 30 mars 2021 Le Président"),
    ],
]


def test_a_burkinabe_law_has_titles_chapters_and_articles_of_alineas_and_closes_with_ainsi_fait():
    text, result = build(BF, structure.BF_LAW, "fr")
    nodes = by_locator(result)
    assert result.root.heading == "LOI N°001-2021/AN"
    assert span(text, result.root).startswith("BURKINA FASO")  # the cover, held by the document
    assert span(text, nodes["preamble"]).endswith("dont la teneur suit :")
    chapter = nodes["title_i__chap_1"]
    assert (chapter.label, chapter.heading) == ("CHAPITRE 1", "OBJET ET CHAMP D'APPLICATION")
    assert nodes["art_4"].parent is chapter and nodes["art_4"].label == "Article 4"
    assert span(text, nodes["art_4__al_2"]).startswith("traitements effectués")
    # The last article is the enacting formula, numbered; "Ainsi fait …" closes the law.
    assert span(text, nodes["art_83__al_1"]) == "La présente loi sera exécutée comme loi de l'Etat."
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
        head("SIXTY-SIXTH ORDINARY SESSION OF THE COUNCIL OF MINISTERS"),
        head("DIRECTIVE C/DIR. 1/08/11 ON FIGHTING CYBER CRIME WITHIN ECOWAS"),
        head("THE COUNCIL OF MINISTERS,"),
        para("MINDFUL of Articles 10, 11 and 12 of the ECOWAS Treaty as amended; PRESCRIBES:"),
        head("CHAPTER I"),
        head("GENERAL PROVISIONS"),
        head("Article 1 Definitions"),
        para("For the purposes of this Directive:"),
    ],
    [
        head("CHAPTER VII:"),
        head("FINAL PROVISIONS"),
        head("Article 35 Implementation"),
        para(
            "Member States shall adopt the necessary legislative, regulatory and administrative",
            "list_item",
            marker="1.",
        ),
        para(
            "The measures referred to in Paragraph 1 of this Article should make reference",
            "list_item",
            marker="2.",
        ),
        para("DONE AT ABUJA, THIS 19TH DAY OF AUGUST 2011 H.E. OLUGBENGA ASHIRU CHAIRMAN FOR COUNCIL"),
    ],
]


def test_an_ecowas_directive_has_its_preamble_chapters_articles_numbered_paragraphs_and_signature():
    text, result = build(ECOWAS_DIRECTIVE, structure.ECOWAS_DIRECTIVE, "en")
    nodes = by_locator(result)
    assert result.root.heading == "DIRECTIVE C/DIR. 1/08/11 ON FIGHTING CYBER CRIME WITHIN ECOWAS"
    assert span(text, result.root).startswith("SIXTY-SIXTH ORDINARY SESSION")
    assert span(text, nodes["preamble"]).endswith("PRESCRIBES:")
    assert (nodes["chap_i"].heading, nodes["art_1"].heading) == ("GENERAL PROVISIONS", "Definitions")
    chapter = nodes["chap_vii"]
    assert (chapter.label, chapter.heading) == ("CHAPTER VII", "FINAL PROVISIONS")
    assert nodes["art_35"].parent is chapter and nodes["art_35__para_2"].label == "2."
    assert resolve(result, "art. 35(2)") is nodes["art_35__para_2"]
    assert span(text, nodes["notice_1"]).startswith("DONE AT ABUJA")
    assert codes(result) == []
    spans_nest(text, result)


# Directive C/DIR.3/7/26 on Open Data within ECOWAS (Freetown, 17 July 2026), in Portuguese, as the legal
# journal publishes it (in the file named for the roaming regulation's Portuguese text): the logo text
# on its cover, "ADOTA:", chapters titled after a dash, "Artigo 5.º - Direito de acesso", and "FEITO EM".
ECOWAS_DIRECTIVE_PT = [
    [
        para("CEDEAO ECOWAS COMMISSION COMMISSION DE LA CEDEAO COMISSÃO DA CEDEAO 51 ECOWAS"),
        head("DIRETIVA C/DIR.3/7/26 RELATIVA AOS DADOS ABERTOS NO SEIO DA CEDEAO"),
        para("O CONSELHO DE MINISTROS, CIENTE do Tratado da CEDEAO revisto, tal como alterado"),
        head("ADOTA:"),
        head("CAPÍTULO II - ACESSIBILIDADE DOS DOCUMENTOS"),
        head("Artigo 5.º - Direito de acesso"),
        para(
            "O direito de acesso é reconhecido às pessoas singulares ou coletivas", "list_item", marker="1."
        ),
        para("Os seguintes documentos estão excluídos do direito de acesso:", "list_item", marker="3."),
        para("FEITO EM FREETOWN, AOS 17 DIAS DE JULHO DE 2026. PELO CONSELHO, O PRESIDENTE"),
    ]
]


def test_an_ecowas_directive_in_portuguese_reads_artigo_and_closes_with_feito_em():
    text, result = build(ECOWAS_DIRECTIVE_PT, structure.ECOWAS_DIRECTIVE, "pt")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).endswith("ADOTA:")
    assert nodes["chap_ii"].heading == "ACESSIBILIDADE DOS DOCUMENTOS"
    article = nodes["art_5"]
    assert (article.label, article.heading, article.parent) == (
        "Artigo 5",
        "Direito de acesso",
        nodes["chap_ii"],
    )
    assert resolve(result, "art. 5(3)") is nodes["art_5__para_3"]
    assert span(text, nodes["notice_1"]).startswith("FEITO EM FREETOWN")
    assert codes(result) == ["running_head"]  # the logo text on the cover, for the reviewer to remove
    spans_nest(text, result)


# --- ecowas-regulation-1 -------------------------------------------------------------------------------

# Regulation C/REG.3/7/26 relating to Roaming on Public Mobile Communications Networks (Freetown, 17 July
# 2026), in its English and French texts as the ECOWAS legal journal publishes them: "ENACTS:" / "EDICTE :",
# chapters titled after a dash or a colon ("CHAPITRE PREMIER : …"), articles headed in capitals, items
# lettered "a." and the closing words the reading kept with the last article.
ECOWAS_REGULATION_EN = [
    [
        head("NINETY-SIXTH ORDINARY SESSION OF THE COUNCIL OF MINISTERS"),
        head("REGULATION C/REG.3/7/26 RELATING TO ROAMING ON PUBLIC MOBILE COMMUNICATIONS NETWORKS"),
        para("THE COUNCIL OF MINISTERS, MINDFUL of the ECOWAS Revised Treaty, as amended"),
        head("ENACTS:"),
        head("CHAPTER I—DEFINITIONS, OBJECTIVE AND SCOPE OF APPLICATION"),
        head("ARTICLE 2: OBJECTIVE AND SCOPE OF APPLICATION"),
        para("This Regulation shall apply to:", "list_item", marker="2."),
        para("all mobile communications service operators and providers established in a Member State."),
    ],
    [
        head("ARTICLE 16: MEMBER STATES' OBLIGATIONS"),
        para("A Member State shall:"),
        para("a. comply with this Regulation within its territory and shall provide the public", "list_item"),
        para("b. be alert to the particular case of inadvertent roaming in the border regions", "list_item"),
        head("ARTICLE 26"),
        para("This REGULATION C/REG.3/7/26 shall enter into force upon its publication."),
        para("DONE IN FREETOWN, THIS 17TH DAY OF JULY 2026 H.E. ALHAJI MUSA TIMOTHY KABBA"),
    ],
]
ECOWAS_REGULATION_FR = [
    [
        head("RÈGLEMENT C/REG.3/7/26 RELATIF À L'ITINÉRANCE SUR LES RÉSEAUX PUBLICS DE TÉLÉPHONIE MOBILE"),
        para("LE CONSEIL DES MINISTRES, VU le Traité révisé de la CEDEAO tel qu'amendé"),
        head("EDICTE :"),
        head("CHAPITRE PREMIER : DÉFINITIONS, OBJECTIF ET CHAMP D'APPLICATION"),
        head("ARTICLE 1er : DÉFINITIONS"),
        para("Aux termes du présent Règlement, on entend par :"),
        head("ARTICLE 26 : ENTRÉE EN VIGUEUR"),
        para("Le présent Règlement C/REG.3/7/26 entre en vigueur dès sa publication."),
        para("FAIT À FREETOWN, CE 17 JUILLET 2026. POUR LE CONSEIL, LE PRESIDENT"),
    ]
]


def test_an_ecowas_regulation_letters_its_items_and_its_french_text_numbers_chapter_one_in_words():
    text, result = build(ECOWAS_REGULATION_EN, structure.ECOWAS_REGULATION, "en")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).endswith("ENACTS:")
    assert nodes["chap_i"].heading == "DEFINITIONS, OBJECTIVE AND SCOPE OF APPLICATION"
    assert "all mobile communications" in span(text, nodes["art_2__para_2"])
    assert nodes["art_16__subpara_b"].label == "b."  # "a." and "b.", as printed
    assert resolve(result, "art. 16(b)") is nodes["art_16__subpara_b"]
    assert span(text, nodes["notice_1"]).startswith("DONE IN FREETOWN")
    assert codes(result) == []
    spans_nest(text, result)
    text, french = build(ECOWAS_REGULATION_FR, structure.ECOWAS_REGULATION, "fr")
    nodes = by_locator(french)
    assert (nodes["chap_1"].label, nodes["art_1"].heading) == ("CHAPITRE PREMIER", "DÉFINITIONS")
    assert nodes["art_26"].heading == "ENTRÉE EN VIGUEUR" and "notice_1" in nodes
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
    assert rules("aa-ecowas", "decision") is None  # no whole official decision to calibrate on yet
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
