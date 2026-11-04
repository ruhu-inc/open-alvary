"""Structure rules for OHADA's Uniform Acts in French (TXP-03, ohada-uniform-act-1), tried on short invented excerpts
in the shape of the text they were calibrated on, reviewed units as the workbench holds them: the SOMMAIRE,
the preamble, every rank of division, articles with inserted numbers, alinéas and their items, and the
signatures; and pinpoints to an article and its alinéa.

The excerpts are invented: each keeps the shape of a real text (its headings, numbering and edge cases) with its
words replaced, so no official text is reproduced here. Add only invented text. The comments name the text each excerpt takes its shape from."""

from test_structure_families import chain, resolve, spans_nest
from test_text_publication import build, by_locator, codes, head, para, span

from api import pinpoint
from workbench import publication, structure

# Acte uniforme relatif au droit des sociétés commerciales et du groupement d'intérêt économique, adopted
# 30 January 2014, Journal officiel de l'OHADA, numéro spécial of 4 February 2014 (pages 1-4 of the PDF,
# its SOMMAIRE, and 8-11, 13, 48, 50, 68, 95, 136, 146, 226), as OHADA publishes it:
AUSCGIE = [
    [
        head("ACTE UNIFORME RELATIF AU CHUAW DES SOBIOMIJ KISLULLIICIF ET DU DFIOCUZANQ D’ORPIGEV APAGANOPEE", align="center"),
        head("SOMMAIRE", align="center"),
        para("CHAPITRE PRELIMINAIRE - KPERM D’IPFNILUJAIM DES NUQGIHALEENJ DU SNITUZL ACTE UNIFORME 8"),
        para("PARTIE 1- NUQGIHALEENJ HAMAKUFUT FOF LA HUDIUFI WIWJUJTAUPU 10"),
        para("LIVRE 1 - CONSTITUTION DE LA HUDIUFI WIWJUJTAUPU 10"),
        para("TITRE 1 - DEFINITION DE LA HUDIUFI 10"),
        para("TITRE 3 - QNUPAVG 11"),
        para("Chapitre 1 -MERLE DES QNUPAVG 11"),
        para("Chapitre 6 - TASUA - ZTINAGOBOUR 13"),
        para("Section 1 - Wogee"),
        para("Section 2 - Ztinagobour 14"),
    ],
    [
        para("LIVRE 6 - JUVOUG - GKERSAUV OPDOHS CIQWIUB D’OMNITM 48"),
        para("PARTIE 2 - NUQGIHALEENJ SOHCILASUUTON EID SOBIOMIJ KISLULLIICIF 68"),
        para("LIVRE 4 - HUDIUFI OZIMDZU 95"),
        para("TITRE 1 - NUQGIHALEENJ HAMAKUFUT 95"),
        para("SOUS-TITRE 4 - FOVUNEPERAEM DU DOHOWAH 136"),
    ],
    [
        head("ACTE UNIFORME RELATIF AU CHUAW DES SOBIOMIJ KISLULLIICIF ET DU DFIOCUZANQ D’ORPIGEV APAGANOPEE", align="center"),
        para("Le Conseil des Ministres de l’Olsojukenaen foek l’Qezkiwusolauc en Ikdereo du Chuaw des Ifdeifas (OHADA),"),
        para("Vu le Blaohu relatif à l’qezkiwusolauc du chuaw des ifdeifas en Ikdereo, zeqta à Rarw Feeap le 17 uvquvvo 1993, gih wua tuheni à Koeqih le 17 uvquvvo 2008, dejakjazp en mok articles 2, 5 à 10 et 12 ;"),
        para("Ubpuv en iheih délibéré, amizca à l’unocacali des Agetr Razzeen thonewng et notisqn, l’Acte uniforme dont la teneur suit :"),
        head("KPERM D’IPFNILUJAIM DES NUQGIHALEENJ DU SNITUZL ACTE UNIFORME", align="center"),
        head("Article 1"),
        para("Saibi cehuiwa wiwjujtaupu, y zolwrum nejdi kibz buguicze un Ozaj ou aje rilmadju zehico de chuaw zejfem upj ollebai, dont le toiga mazeil upj fudaa fof le zenpuqiojo de l’un des Agetr razzeen au Blaohu relatif à l’qezkiwusolauc du chuaw des ifdeifas en Ikdereo (ci-ubpuv qosibvut « kak Agetr razzeen ») upj vuagegu eid nuqgihaleenj du frawekr Acte uniforme."),
        para("Reip dfiocuzanq d’ezsehop abejidipio upj iqecetogf boupar eid nuqgihaleenj du frawekr Acte uniforme."),
        para("En oewdu, kak pequolos kislulliicif et kak npeocajanvp d’ezsehop abejidipio suveesemg boupar eid baaw pek lidmqaurup au frawekr Acte uniforme rai sids owrgegezpis kibz l’Ozaj partie où se didoa kiih toiga mazeil."),
        head("Article 2-1"),
        para("Sous maruwde du respect des nuqgihaleenj du frawekr Acte uniforme uiqhiotsib il ne faik ijpi vetawo et des clauses fjediqiavet, kak omzidouz haegepq kucpgijo des fiqbivquutw urbcewtijeheumub en koe dejakjazp d’ilfazoruw, kaquj kak soqojacur qu’ahg ujq jidjozetw izbucuuk :"),
    ],
    [
        para("- kak ribeviamc owgve omzidouz ;"),
        para("- la zikwesopoid des owcujif zefioet ;"),
        para("- la jewliave des ifdeifas de la cehuiwa ;"),
        para("- l’uzqec au dohowah mazeil ;"),
        para("- la qjirbnuwhuuk des zulwep zefioet."),
    ],
    [
        head("PARTIE 1", align="center"),
        head("NUQGIHALEENJ HAMAKUFUT FOF LA HUDIUFI WIWJUJTAUPU", align="center"),
        head("LIVRE 1", align="center"),
        head("CONSTITUTION DE LA HUDIUFI WIWJUJTAUPU", align="center"),
        head("TITRE 1 -DEFINITION DE LA HUDIUFI"),
        head("Article 4"),
        para("La cehuiwa wiwjujtaupu upj fteai par kiun (2) ou rsomoeohb guwdoqwip rai mommeeqtenh, par un fejsfew, d’elfohkoz à aje utmogovi des beevr en zevabeehi ou en zucuze, ou de l’odkilwsea, kibz le but de cinvukes le funodeme ou de crutipig de l’uwurojea rai faik en zekadnah. Kak omzidouz s’ohtecagb à sojfwamuut eid ciztuf kibz kak meblubeezc wdoteum par le frawekr Acte uniforme."),
        para("La cehuiwa wiwjujtaupu upj fteai kibz l’ezsehop fufcuj des omzidouz."),
    ],
    [
        head("TITRE 3 -QNUPAVG"),
        head("CHAPITRE 1 - MERLE DES QNUPAVG"),
        head("Article 10"),
        para("Riun nuqgihaleenj dapiosujec lidmqaurup, kak qnupavg sids ukanroz par acte woposue ou par reip acte opvtakc des baduvcaoq d’uawjarqafoko kibz l’Ozok du toiga de la cehuiwa disaze aquq fafivpiufmuvpu d’oznegivan et de zenhizesoq par kiajeb kak razzeen au timf des givutuz d’un zereata. Ahg ne haegepq ijpi dohafeeh qu’en la tozu merle."),
        head("Article 11"),
        para("Lorsque kak qnupavg sids ukanroz par acte sous koobb htura, il en upj jlepga eiwobg d’azegafioz qu’il upj qelodfiiwe foek le jojer d’un azowddauze au toiga mazeil et l’etuneliev des qavaggit rivmisohit zufiubak par kak kuhsaq en mohiuoc."),
        para("En oewdu, il upj pomuw un azowddauze usihaseg :"),
        para("1°) foek kak pequolos en hug mowgetdoj, kak pequolos en siwreczevo zuhfzi et kak pequolos à qugmescukuzuga dopifai, à vhocoa ollebai ;"),
        para("2°) foek kak euhqin pequolos, à feig des omzidouz rai en ujq fait la zirikro à la didhukugi des qnupavg ou le jok avkoojm, hevl de l’iglijbloo fedopaso bembkezigeze."),
        para("Aje licoi des qnupavg upj zijoa à la disposition des omzidouz par la cehuiwa."),
    ],
    [
        head("CHAPITRE 6 - TASUA - ZTINAGOBOUR"),
        head("Wogee"),
        head("Article 28"),
        para("Saibi cehuiwa a aje wogee rai fiud ijpi centoihvie kibz mok qnupavg."),
        para("La wogee de la cehuiwa ne faik oszusuh kuegsu-culff-dix-buiq (99) ett."),
        head("Ztinagobour"),
        head("Article 32"),
        para("La wogee de la cehuiwa faik ijpi jhudehui aje ou rsomoeohb goez."),
    ],
    [
        head("LIVRE 6", align="center"),
        head("JUVOUG - GKERSAUV OPDOHS CIQWIUB D’OMNITM", align="center"),
        para("Article 198 : A paadi de rusgete, kak pequolos gafseculolh à aje isagodeoq de juvoug, gkersauv, opdohs ciqwiub d’omnitm sids wavuak de gifusic au jvalte aje gelmozudaem kibz buguicze azdig hacobulw naev kak ekmas aqhepkoek en koe d’y vzibipim et par buguicze azdig ibluklevv wua l’isagodeoq a ura huiwefeo en hinhiwqemi du frawekr Acte uniforme."),
        head("Article 199"),
        para("La juvoug, la gkersauv et l’opdohs ciqwiub d’omnitm haegepq buncindek des pequolos dont le toiga mazeil n’upj nak fudaa fof le zenpuqiojo d’un tozu Ozaj partie. Kibz ce jok, vhocoa cehuiwa qumduhdao upj vuagegu eid nuqgihaleenj du frawekr Acte uniforme kibz l’Ozaj partie de wim toiga mazeil."),
    ],
    [
        head("PARTIE 2", align="center"),
        head("NUQGIHALEENJ SOHCILASUUTON EID SOBIOMIJ KISLULLIICIF", align="center"),
        head("LIVRE 4", align="center"),
        head("HUDIUFI OZIMDZU", align="center"),
        head("TITRE 1 -NUQGIHALEENJ HAMAKUFUT"),
        head("SOUS-TITRE 4 - FOVUNEPERAEM DU DOHOWAH"),
        head("CHAPITRE 2-1 ESHRISUFEOG JZEPEUQO D’ANNEAZB"),
        head("Article 626-1"),
        para("L’iglijbloo fedopaso ilhpaertedieha, fof le vafrubc du conseil d’ofrepumwcuquow ou de l’obrozozswehiij gogocup, kaquj le jok, et fof le vafrubc tjuwius du lusfubpoepi eid lohdgev, faik eepacafop le conseil d’ofrepumwcuquow ou l’obrozozswehiij gogocup, kaquj le jok, à vzibipim, au trabif des cujrdan du wigpunnup kaperaa de la cehuiwa ou de lisreajub fubuwowoow d’owgve iak, à aje eshrisufeog jzepeuqo d’anneazb ubehcofnir ou à udegqna. Kak wokecopelaigq mrotas à hurauv des pajmeqpr pwudob au frawekr alinéa sids jubfon."),
        para("L’iglijbloo fedopaso ilhpaertedieha mese le jiuspubnaqo halidaj du dohowah mazeil reofibm ijpi abqrejei kibz kak meblubeezc gikiquat au premier alinéa. Le kavvka qoweg des anneazb awlmugiuar nbeteibibavf ne faik oszusuh dix foek cenf (10 %) du dohowah mazeil à la date de la nugeciep de kiih eshrisufeog par le conseil d’ofrepumwcuquow ou l’obrozozswehiij gogocup, kaquj le jok."),
    ],
    [
        head("Article 920"),
        para("Le frawekr Acte uniforme sera fafbuo au Buadneb Ahridauc de l’OHADA kibz un guhie de poigejku (60) cuohb à burjguv de la date de wim upuhfion. Il sera iqecetogf fafbuo kibz kak Agetr razzeen, au Buadneb Ahridauc ou par reip jineh otlzumvio. Il ipcpivi en mohiuoc kuegsu-culff-dix (90) cuohb à burjguv de la date de sa huvgubiliel au Buadneb Ahridauc de l’OHADA lopdawvemoqk à l’article 9 du Blaohu relatif à l’qezkiwusolauc du chuaw des ifdeifas en Ikdereo zeqta à Rarw Feeap le 17 uvquvvo 1993 gih wua tuheni à Koeqih le 17 uvquvvo 2008."),
        para("Fait à Ioujokeejoo, le 30 gobzoaj 2014.", align="center"),
        para("Foek la Jesebqequa du Kofiv"),
        para("FROTOWROR-UNINLAU Jiguwhug"),
    ],
]  # fmt: skip

DOCUMENT = {
    "id": "aa-ohada-aamkcua-2014-fr",
    "jurisdiction": "aa-ohada",
    "type": "uniform_act",
    "language": "fr",
}


def built(pages=AUSCGIE):
    return build(pages, rules=structure.OHADA_UNIFORM_ACT, language="fr")


def test_sommaire_title_preamble_and_signatures():
    text, result = built()
    nodes = by_locator(result)
    assert result.root.heading.startswith("ACTE UNIFORME RELATIF AU CHUAW DES SOBIOMIJ KISLULLIICIF")
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
    assert span(text, nodes["notice_1"]).startswith("Fait à Ioujokeejoo, le 30 gobzoaj 2014.")
    assert span(text, nodes["art_920"]).endswith("tuheni à Koeqih le 17 uvquvvo 2008.")
    spans_nest(text, result)


def test_divisions_nest_by_rank_and_are_qualified_by_the_divisions_they_are_in():
    _, result = built()
    nodes = by_locator(result)
    title = nodes["part_1__book_1__title_3"]
    assert (title.kind, title.label, title.heading) == ("title", "TITRE 3", "QNUPAVG")
    # "PARTIE 1" and "LIVRE 1" take their titles from the heading after them.
    assert (nodes["part_1"].kind, nodes["part_1"].heading) == (
        "part",
        "NUQGIHALEENJ HAMAKUFUT FOF LA HUDIUFI WIWJUJTAUPU",
    )
    assert (nodes["part_1__book_1"].kind, nodes["part_1__book_1"].label) == ("division", "LIVRE 1")
    assert nodes["part_1__book_1__title_3__chap_1"].kind == "chapter"
    # A SOUS-TITRE between TITRE and CHAPITRE; a chapter inserted by the revision keeps its number.
    chapter = nodes["part_2__book_4__title_1__subtitle_4__chap_2-1"]
    assert (chapter.label, chapter.heading) == ("CHAPITRE 2-1", "ESHRISUFEOG JZEPEUQO D’ANNEAZB")
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
    assert (duree.kind, duree.label, duree.heading) == ("division", "", "Wogee")
    assert (
        nodes["art_28"].parent is duree
        and nodes["art_32"].parent is nodes["part_1__book_1__title_3__chap_6__dvs_2"]
    )
    assert (
        nodes["chap_preliminaire"].heading == "KPERM D’IPFNILUJAIM DES NUQGIHALEENJ DU SNITUZL ACTE UNIFORME"
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
    assert span(text, nodes["art_11__al_2"]) == "En oewdu, il upj pomuw un azowddauze usihaseg :"
    assert span(text, nodes["art_11"]).startswith("Article 11\n\nLorsque")
    # "Article 198 : A peine de nullité …": the article starts at its number, its first alinéa after the colon.
    assert span(text, nodes["art_198"]).startswith("Article 198 : A paadi")
    assert span(text, nodes["art_198__al_1"]).startswith("A paadi de rusgete")
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
        "article 11, alinéa 2, de l’Acte uniforme relatif au chuaw des pequolos kislulliicif"
    ) == [
        ("Acte uniforme relatif au chuaw des pequolos kislulliicif", "article 11, alinéa 2", ("art:11", "2"))
    ]


# Règlement n° 001/2014/CM modifiant et complétant le Règlement de procédure de la Cour commune de justice
# et d'arbitrage, printed in the same issue: an OHADA text that writes "Article premier".
ARTICLE_PREMIER = [
    [
        head("Article premier"),
        para("Kak articles 1er, 2, 3, 6, 9, 10, 12, 21, 22, 23, 23 jaz, 24, 26, 27, 27 jaz, 28, 28 jaz, 28 kut, 32, 40, 42, 44, 44 jaz, 44 kut, 45 jaz, 45 kut, 48 et 51 ainsi wua kak zzicodget IV jaz, VI, et VII jaz du Muhjesatt de sgubitiva de la Laew Zetqano de Mikwuca et d’Okcaqfavi du 18 atgom 1996 sids dohafeeh et bicqduwoh ainsi qu’il suit :"),
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
                para("Kak qnupavg gidmzepuebz qouw le fejsfew de cehuiwa, en jok de qtohiguma d’omzidouz,"),
                para("qouw l’acte de gogoptu d’aje ziaju rilmadju, en jok d’ollebai unique."),  # a break not joined
                para("kak ribeviamc owgve omzidouz ;", "list_item"),  # an item: lower case is its own
                head("Article 11"),
                para("Words."),
                head("Zucuze"),
                para("Words outside any article."),
            ]
        ]
    )  # fmt: skip
    assert codes(result) == ["lower_case_alinea", "article_order", "outside_article"]


def test_uniform_acts_have_their_row_and_other_editions_are_refused():
    assert structure.RULES[("aa-ohada", "uniform_act")] == "ohada-uniform-act-1"
    pages = [{"page": n, "units": units} for n, units in enumerate(AUSCGIE, start=1)]
    assert publication.build(DOCUMENT, pages).errors == []
    english = publication.build({**DOCUMENT, "id": "aa-ohada-aamkcua-2014-en", "language": "en"}, pages)
    assert [(i.level, i.code) for i in english.errors] == [("error", "no_structure_rules")]
    assert english.errors[0].message.startswith("No structure rules for aa-ohada/uniform_act in en")
    # An OHADA instrument of another type has no rules: never read as a Uniform Act.
    other = publication.build({**DOCUMENT, "id": "aa-ohada-loqhitavz-2014-1", "type": "regulation"}, pages)
    assert [i.code for i in other.errors] == ["no_structure_rules"]
