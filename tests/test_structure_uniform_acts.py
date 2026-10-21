"""Structure rules for OHADA's Uniform Acts in French (TXP-03, ohada-uniform-act-1), tried on short excerpts
of the official text they were calibrated on, reviewed units as the workbench holds them: the SOMMAIRE,
the preamble, every rank of division, articles with inserted numbers, alinéas and their items, and the
signatures; and pinpoints to an article and its alinéa.

The excerpts are a few articles each, copied from the official file; running heads, page numbers and the
initials signed at the foot of each page are left out, as the readings keep them apart."""

from test_structure_families import chain, resolve, spans_nest
from test_text_publication import build, by_locator, codes, head, para, span

from api import pinpoint
from workbench import publication, structure

# Acte uniforme relatif au droit des sociétés commerciales et du groupement d'intérêt économique, adopted
# 30 January 2014, Journal officiel de l'OHADA, numéro spécial of 4 February 2014 (pages 1-4 of the PDF,
# its SOMMAIRE, and 8-11, 13, 48, 50, 68, 95, 136, 146, 226), as OHADA publishes it:
# https://biblio.ohada.org/doc_num.php?explnum_id=2032
AUSCGIE = [
    [
        head("ACTE UNIFORME RELATIF AU DROIT DES SOCIETES COMMERCIALES ET DU GROUPEMENT D’INTERET ECONOMIQUE", align="center"),
        head("SOMMAIRE", align="center"),
        para("CHAPITRE PRELIMINAIRE - CHAMP D’APPLICATION DES DISPOSITIONS DU PRESENT ACTE UNIFORME 8"),
        para("PARTIE 1- DISPOSITIONS GENERALES SUR LA SOCIETE COMMERCIALE 10"),
        para("LIVRE 1 - CONSTITUTION DE LA SOCIETE COMMERCIALE 10"),
        para("TITRE 1 - DEFINITION DE LA SOCIETE 10"),
        para("TITRE 3 - STATUTS 11"),
        para("Chapitre 1 -FORME DES STATUTS 11"),
        para("Chapitre 6 - DUREE - PROROGATION 13"),
        para("Section 1 - Durée"),
        para("Section 2 - Prorogation 14"),
    ],
    [
        para("LIVRE 6 - FUSION - SCISSION APPORT PARTIEL D’ACTIFS 48"),
        para("PARTIE 2 - DISPOSITIONS PARTICULIERES AUX SOCIETES COMMERCIALES 68"),
        para("LIVRE 4 - SOCIETE ANONYME 95"),
        para("TITRE 1 - DISPOSITIONS GENERALES 95"),
        para("SOUS-TITRE 4 - MODIFICATION DU CAPITAL 136"),
    ],
    [
        head("ACTE UNIFORME RELATIF AU DROIT DES SOCIETES COMMERCIALES ET DU GROUPEMENT D’INTERET ECONOMIQUE", align="center"),
        para("Le Conseil des Ministres de l’Organisation pour l’Harmonisation en Afrique du Droit des Affaires (OHADA),"),
        para("Vu le Traité relatif à l’harmonisation du droit des affaires en Afrique, signé à Port Louis le 17 octobre 1993, tel que révisé à Québec le 17 octobre 2008, notamment en ses articles 2, 5 à 10 et 12 ;"),
        para("Après en avoir délibéré, adopte à l’unanimité des États Parties présents et votants, l’Acte uniforme dont la teneur suit :"),
        head("CHAMP D’APPLICATION DES DISPOSITIONS DU PRESENT ACTE UNIFORME", align="center"),
        head("Article 1"),
        para("Toute société commerciale, y compris celle dans laquelle un État ou une personne morale de droit public est associé, dont le siège social est situé sur le territoire de l’un des États parties au Traité relatif à l’harmonisation du droit des affaires en Afrique (ci-après désignés « les États parties ») est soumise aux dispositions du présent Acte uniforme."),
        para("Tout groupement d’intérêt économique est également soumis aux dispositions du présent Acte uniforme."),
        para("En outre, les sociétés commerciales et les groupements d’intérêt économique demeurent soumis aux lois non contraires au présent Acte uniforme qui sont applicables dans l’État partie où se situe leur siège social."),
        head("Article 2-1"),
        para("Sous réserve du respect des dispositions du présent Acte uniforme auxquelles il ne peut être dérogé et des clauses statutaires, les associés peuvent conclure des conventions extrastatutaires en vue notamment d’organiser, selon les modalités qu’ils ont librement arrêtées :"),
    ],
    [
        para("- les relations entre associés ;"),
        para("- la composition des organes sociaux ;"),
        para("- la conduite des affaires de la société ;"),
        para("- l’accès au capital social ;"),
        para("- la transmission des titres sociaux."),
    ],
    [
        head("PARTIE 1", align="center"),
        head("DISPOSITIONS GENERALES SUR LA SOCIETE COMMERCIALE", align="center"),
        head("LIVRE 1", align="center"),
        head("CONSTITUTION DE LA SOCIETE COMMERCIALE", align="center"),
        head("TITRE 1 -DEFINITION DE LA SOCIETE"),
        head("Article 4"),
        para("La société commerciale est créée par deux (2) ou plusieurs personnes qui conviennent, par un contrat, d’affecter à une activité des biens en numéraire ou en nature, ou de l’industrie, dans le but de partager le bénéfice ou de profiter de l’économie qui peut en résulter. Les associés s’engagent à contribuer aux pertes dans les conditions prévues par le présent Acte uniforme."),
        para("La société commerciale est créée dans l’intérêt commun des associés."),
    ],
    [
        head("TITRE 3 -STATUTS"),
        head("CHAPITRE 1 - FORME DES STATUTS"),
        head("Article 10"),
        para("Sauf dispositions nationales contraires, les statuts sont établis par acte notarié ou par tout acte offrant des garanties d’authenticité dans l’Etat du siège de la société déposé avec reconnaissance d’écritures et de signatures par toutes les parties au rang des minutes d’un notaire. Ils ne peuvent être modifiés qu’en la même forme."),
        head("Article 11"),
        para("Lorsque les statuts sont établis par acte sous seing privé, il en est dressé autant d’originaux qu’il est nécessaire pour le dépôt d’un exemplaire au siège social et l’exécution des diverses formalités requises par les textes en vigueur."),
        para("En outre, il est remis un exemplaire original :"),
        para("1°) pour les sociétés en nom collectif, les sociétés en commandite simple et les sociétés à responsabilité limitée, à chaque associé ;"),
        para("2°) pour les autres sociétés, à ceux des associés qui en ont fait la demande à la signature des statuts ou le cas échéant, lors de l’assemblée générale constitutive."),
        para("Une copie des statuts est tenue à la disposition des associés par la société."),
    ],
    [
        head("CHAPITRE 6 - DUREE - PROROGATION"),
        head("Durée"),
        head("Article 28"),
        para("Toute société a une durée qui doit être mentionnée dans ses statuts."),
        para("La durée de la société ne peut excéder quatre-vingt-dix-neuf (99) ans."),
        head("Prorogation"),
        head("Article 32"),
        para("La durée de la société peut être prorogée une ou plusieurs fois."),
    ],
    [
        head("LIVRE 6", align="center"),
        head("FUSION - SCISSION APPORT PARTIEL D’ACTIFS", align="center"),
        para("Article 198 : A peine de nullité, les sociétés participant à une opération de fusion, scission, apport partiel d’actifs sont tenues de déposer au greffe une déclaration dans laquelle elles relatent tous les actes effectués en vue d’y procéder et par laquelle elles affirment que l’opération a été réalisée en conformité du présent Acte uniforme."),
        head("Article 199"),
        para("La fusion, la scission et l’apport partiel d’actifs peuvent concerner des sociétés dont le siège social n’est pas situé sur le territoire d’un même État partie. Dans ce cas, chaque société concernée est soumise aux dispositions du présent Acte uniforme dans l’État partie de son siège social."),
    ],
    [
        head("PARTIE 2", align="center"),
        head("DISPOSITIONS PARTICULIERES AUX SOCIETES COMMERCIALES", align="center"),
        head("LIVRE 4", align="center"),
        head("SOCIETE ANONYME", align="center"),
        head("TITRE 1 -DISPOSITIONS GENERALES"),
        head("SOUS-TITRE 4 - MODIFICATION DU CAPITAL"),
        head("CHAPITRE 2-1 ATTRIBUTION GRATUITE D’ACTIONS"),
        head("Article 626-1"),
        para("L’assemblée générale extraordinaire, sur le rapport du conseil d’administration ou de l’administrateur général, selon le cas, et sur le rapport spécial du commissaire aux comptes, peut autoriser le conseil d’administration ou l’administrateur général, selon le cas, à procéder, au profit des membres du personnel salarié de la société ou de certaines catégories d’entre eux, à une attribution gratuite d’actions existantes ou à émettre. Les délibérations prises à défaut des rapports prévus au présent alinéa sont nulles."),
        para("L’assemblée générale extraordinaire fixe le pourcentage maximal du capital social pouvant être attribué dans les conditions définies au premier alinéa. Le nombre total des actions attribuées gratuitement ne peut excéder dix pour cent (10 %) du capital social à la date de la décision de leur attribution par le conseil d’administration ou l’administrateur général, selon le cas."),
    ],
    [
        head("Article 920"),
        para("Le présent Acte uniforme sera publié au Journal Officiel de l’OHADA dans un délai de soixante (60) jours à compter de la date de son adoption. Il sera également publié dans les États parties, au Journal Officiel ou par tout moyen approprié. Il entrera en vigueur quatre-vingt-dix (90) jours à compter de la date de sa publication au Journal Officiel de l’OHADA conformément à l’article 9 du Traité relatif à l’harmonisation du droit des affaires en Afrique signé à Port Louis le 17 octobre 1993 tel que révisé à Québec le 17 octobre 2008."),
        para("Fait à Ouagadougou, le 30 janvier 2014.", align="center"),
        para("Pour la République du Bénin"),
        para("DJENONTIN-AGOSSOU Valentin"),
    ],
]  # fmt: skip

DOCUMENT = {
    "id": "aa-ohada-auscgie-2014-fr",
    "jurisdiction": "aa-ohada",
    "type": "uniform_act",
    "language": "fr",
}


def built(pages=AUSCGIE):
    return build(pages, rules=structure.OHADA_UNIFORM_ACT, language="fr")


def test_sommaire_title_preamble_and_signatures():
    text, result = built()
    nodes = by_locator(result)
    assert result.root.heading.startswith("ACTE UNIFORME RELATIF AU DROIT DES SOCIETES COMMERCIALES")
    entries = [(n.label, n.target) for n in result.nodes if n.kind == "contents_entry"]
    assert entries[:4] == [
        ("CHAPITRE PRELIMINAIRE", "chap_preliminaire"),
        ("PARTIE 1", "part_1"),
        ("LIVRE 1", "part_1__book_1"),
        ("TITRE 1", "part_1__book_1__title_1"),
    ]
    assert entries[-1] == ("SOUS-TITRE 4", "part_2__book_4__title_1__subtitle_4")
    # The title printed again ends the SOMMAIRE; the preamble runs to "… dont la teneur suit :".
    assert span(text, nodes["preamble"]).startswith("Le Conseil des Ministres")
    assert span(text, nodes["preamble"]).endswith("l’Acte uniforme dont la teneur suit :")
    assert span(text, nodes["notice_1"]).startswith("Fait à Ouagadougou, le 30 janvier 2014.")
    assert span(text, nodes["art_920"]).endswith("révisé à Québec le 17 octobre 2008.")
    spans_nest(text, result)


def test_divisions_nest_by_rank_and_are_qualified_by_the_divisions_they_are_in():
    _, result = built()
    nodes = by_locator(result)
    title = nodes["part_1__book_1__title_3"]
    assert (title.kind, title.label, title.heading) == ("title", "TITRE 3", "STATUTS")
    # "PARTIE 1" and "LIVRE 1" take their titles from the heading after them.
    assert (nodes["part_1"].kind, nodes["part_1"].heading) == (
        "part",
        "DISPOSITIONS GENERALES SUR LA SOCIETE COMMERCIALE",
    )
    assert (nodes["part_1__book_1"].kind, nodes["part_1__book_1"].label) == ("division", "LIVRE 1")
    assert nodes["part_1__book_1__title_3__chap_1"].kind == "chapter"
    # A SOUS-TITRE between TITRE and CHAPITRE; a chapter inserted by the revision keeps its number.
    chapter = nodes["part_2__book_4__title_1__subtitle_4__chap_2-1"]
    assert (chapter.label, chapter.heading) == ("CHAPITRE 2-1", "ATTRIBUTION GRATUITE D’ACTIONS")
    assert chapter.parent is nodes["part_2__book_4__title_1__subtitle_4"]
    assert nodes["art_626-1"].parent is chapter
    # LIVRE 6 is a book of PARTIE 1; PARTIE 2 closes it and starts its own books.
    assert nodes["art_198"].parent is nodes["part_1__book_6"]
    assert nodes["part_2__book_4"].parent is nodes["part_2"]


def test_a_heading_printed_without_its_number_is_the_division_the_sommaire_lists():
    text, result = built()
    nodes = by_locator(result)
    # The Journal officiel prints "Durée" and "Prorogation" for Sections 1 and 2 of Chapitre 6, and
    # "CHAMP D'APPLICATION …" for the CHAPITRE PRELIMINAIRE: the SOMMAIRE numbers them.
    duree = nodes["part_1__book_1__title_3__chap_6__dvs_1"]
    assert (duree.kind, duree.label, duree.heading) == ("division", "", "Durée")
    assert (
        nodes["art_28"].parent is duree
        and nodes["art_32"].parent is nodes["part_1__book_1__title_3__chap_6__dvs_2"]
    )
    assert (
        nodes["chap_preliminaire"].heading == "CHAMP D’APPLICATION DES DISPOSITIONS DU PRESENT ACTE UNIFORME"
    )
    assert nodes["art_1"].parent is nodes["chap_preliminaire"]
    assert [i.code for i in result.issues] == ["number_from_contents"] * 3
    assert "the contents list it as Section 2" in result.issues[2].message


def test_articles_inserted_numbers_alineas_and_items():
    text, result = built()
    nodes = by_locator(result)
    assert (nodes["art_2-1"].label, nodes["art_2-1"].heading) == ("Article 2-1", "")
    # Every unit of an article is an alinéa, a list's items too, as French drafting counts them.
    assert [n.locator for n in nodes["art_2-1"].children] == [f"art_2-1__al_{n}" for n in range(1, 7)]
    assert [(n.locator, n.label) for n in nodes["art_11"].children] == [
        ("art_11__al_1", ""),
        ("art_11__al_2", ""),
        ("art_11__al_3", "1°)"),
        ("art_11__al_4", "2°)"),
        ("art_11__al_5", ""),
    ]
    assert span(text, nodes["art_11__al_2"]) == "En outre, il est remis un exemplaire original :"
    assert span(text, nodes["art_11"]).startswith("Article 11\n\nLorsque")
    # "Article 198 : A peine de nullité …": the article starts at its number, its first alinéa after the colon.
    assert span(text, nodes["art_198"]).startswith("Article 198 : A peine")
    assert span(text, nodes["art_198__al_1"]).startswith("A peine de nullité")
    assert [n.locator for n in nodes["art_198"].children] == ["art_198__al_1"]


def test_pinpoints_to_an_article_and_its_alinea():
    _, result = built()
    assert resolve(result, "article 11, alinéa 2").locator == "art_11__al_2"
    assert resolve(result, "art. 11 al. 3").locator == "art_11__al_3"  # the item 1°)
    assert resolve(result, "art. 10").locator == "art_10"
    assert resolve(result, "art. 2-1").locator == "art_2-1"
    assert resolve(result, "article 626-1, alinéa 1").locator == "art_626-1__al_1"
    cited = pinpoint.cite(chain(by_locator(result)["art_11__al_2"]))
    assert cited == (("art:11", "2"), "art. 11, al. 2")
    assert pinpoint.split(
        "article 11, alinéa 2, de l’Acte uniforme relatif au droit des sociétés commerciales"
    ) == [
        ("Acte uniforme relatif au droit des sociétés commerciales", "article 11, alinéa 2", ("art:11", "2"))
    ]


# Règlement n° 001/2014/CM modifiant et complétant le Règlement de procédure de la Cour commune de justice
# et d'arbitrage, printed in the same issue (p. 228): an OHADA text that writes "Article premier".
# https://biblio.ohada.org/doc_num.php?explnum_id=2032
ARTICLE_PREMIER = [
    [
        head("Article premier"),
        para("Les articles 1er, 2, 3, 6, 9, 10, 12, 21, 22, 23, 23 bis, 24, 26, 27, 27 bis, 28, 28 bis, 28 ter, 32, 40, 42, 44, 44 bis, 44 ter, 45 bis, 45 ter, 48 et 51 ainsi que les chapitres IV bis, VI, et VII bis du Règlement de procédure de la Cour Commune de Justice et d’Arbitrage du 18 avril 1996 sont modifiés et complétés ainsi qu’il suit :"),
    ]
]  # fmt: skip


def test_article_premier_is_article_1():
    text, result = built(ARTICLE_PREMIER)
    article = by_locator(result)["art_1"]
    assert article.label == "Article premier"
    assert resolve(result, "Article 1er").locator == "art_1"
    assert resolve(result, "article premier, alinéa 1").locator == "art_1__al_1"


def test_warnings_for_the_reviewer():
    _, result = built(
        [
            [
                head("Article 12"),
                para("Les statuts constituent soit le contrat de société, en cas de pluralité d’associés,"),
                para("soit l’acte de volonté d’une seule personne, en cas d’associé unique."),  # a break not joined
                para("les relations entre associés ;", "list_item"),  # an item: lower case is its own
                head("Article 11"),
                para("Words."),
                head("Nature"),
                para("Words outside any article."),
            ]
        ]
    )  # fmt: skip
    assert codes(result) == ["lower_case_alinea", "article_order", "outside_article"]


def test_uniform_acts_have_their_row_and_other_editions_are_refused():
    assert structure.RULES[("aa-ohada", "uniform_act")] == "ohada-uniform-act-1"
    pages = [{"page": n, "units": units} for n, units in enumerate(AUSCGIE, start=1)]
    assert publication.build(DOCUMENT, pages).errors == []
    english = publication.build({**DOCUMENT, "id": "aa-ohada-auscgie-2014-en", "language": "en"}, pages)
    assert [(i.level, i.code) for i in english.errors] == [("error", "no_structure_rules")]
    assert english.errors[0].message.startswith("No structure rules for aa-ohada/uniform_act in en")
    # An OHADA instrument of another type has no rules: never read as a Uniform Act.
    other = publication.build({**DOCUMENT, "id": "aa-ohada-reglement-2014-1", "type": "regulation"}, pages)
    assert [i.code for i in other.errors] == ["no_structure_rules"]
