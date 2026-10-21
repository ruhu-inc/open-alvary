"""Rwanda's Official Gazette split into languages, instruments and articles, and checked across the
three languages. The pages are built here, word by word, in the layout the Gazette prints; the law
and its numbers are invented."""

from ingestion import rw_gazette as RG

WIDTH, HEIGHT = 842.0, 595.0
COLUMNS = [(70.0, 290.0), (300.0, 520.0), (530.0, 770.0)]


def page(*columns: list[str], header: str | None = None, top: float = 70.0) -> dict:
    """A landscape page: each column a list of printed lines, 14 pt apart; a blank line opens a
    paragraph gap. The running header sits centred above the middle column."""
    words = []

    def put(text, x, y):
        for word in text.split():
            width = 5.0 * len(word)
            words.append({"xmin": x, "ymin": y, "xmax": x + width, "ymax": y + 10, "text": word})
            x += width + 4

    if header:
        put(header, 330.0, 30.0)
    for (x0, _), lines in zip(COLUMNS, columns, strict=False):
        y = top
        for line in lines:
            if line:
                put(line, x0, y)
            y += 14
    return {"width": WIDTH, "height": HEIGHT, "words": words}


def law(rw_articles: list[str], en_articles: list[str], fr_articles: list[str]) -> list[dict]:
    first = page(
        [
            "ITEGEKO N° 009/2026 RYO KU WA",
            "12/03/2026 RIGENGA INGERO",
            "ISHAKIRO",
            "Ingingo ya mbere: Icyo iri tegeko",
            "rigamije",
        ],
        [
            "LAW N° 009/2026 OF 12/03/2026",
            "GOVERNING EXAMPLES",
            "TABLE OF CONTENTS",
            "Article One: Purpose of this Law",
        ],
        [
            "LOI N° 009/2026 DU 12/03/2026",
            "RÉGISSANT LES EXEMPLES",
            "TABLE DES MATIÈRES",
            "Article premier : Objet de la présente loi",
        ],
        header="Official Gazette n° 11 Bis of 16/03/2026",
    )
    body = page(
        ["ITEGEKO N° 009/2026 RYO KU WA", "12/03/2026 RIGENGA INGERO", "Twebwe, EXAMPLE Name,", *rw_articles],
        ["LAW N° 009/2026 OF 12/03/2026", "GOVERNING EXAMPLES", "We, EXAMPLE Name,", *en_articles],
        ["LOI N° 009/2026 DU 12/03/2026", "RÉGISSANT LES EXEMPLES", "Nous, EXAMPLE Name,", *fr_articles],
        header="Official Gazette n° 11 Bis of 16/03/2026",
    )
    return [first, body]


RW = [
    "",
    "Ingingo ya mbere: Icyo iri tegeko",
    "",
    "Iri tegeko rigena ingero.",
    "",
    "Ingingo ya 2: Igihe",
    "",
    "Ritangira.",
]
EN = [
    "",
    "Article One: Purpose",
    "",
    "This Law governs examples.",
    "",
    "Article 2: Commencement",
    "",
    "It begins.",
]
FR = [
    "",
    "Article premier : Objet",
    "",
    "La présente loi régit.",
    "",
    "Article 2 : Entrée en vigueur",
    "",
    "Elle entre.",
]


def test_an_underlined_label_a_little_off_the_line_stays_on_its_line():
    words = [
        {"xmin": 70, "ymin": 100, "xmax": 120, "ymax": 110, "text": "Ubushya"},
        {"xmin": 124, "ymin": 100, "xmax": 190, "ymax": 110, "text": "bw’ubuvumbuzi"},
        {"xmin": 10, "ymin": 97, "xmax": 50, "ymax": 108.5, "text": "Ingingo"},  # underlined: set higher
        {"xmin": 52, "ymin": 97, "xmax": 60, "ymax": 108.5, "text": "ya"},
        {"xmin": 62, "ymin": 97, "xmax": 68, "ymax": 108.5, "text": "6:"},
        {"xmin": 10, "ymin": 114, "xmax": 60, "ymax": 124, "text": "Ingingo"},
    ]
    lines = RG.lines_of(words)
    assert [line["text"] for line in lines] == ["Ingingo ya 6: Ubushya bw’ubuvumbuzi", "Ingingo"]


def test_a_parallel_page_gives_three_languages_and_sets_the_running_header_apart():
    split = RG.split_page(
        page(
            ["Ingingo ya mbere: Icyo", "Iri tegeko rigena ikigo kandi", "mu Rwanda na ku"],
            ["Article One: Purpose", "This Law determines the", "institution and the"],
            ["Article premier : Objet", "La présente loi porte", "création de la et des"],
            header="Official Gazette n° 11 Bis of 16/03/2026",
        ),
        7,
    )
    table = RG.split_page(page(*[["Amazina mu Rwanda na Kigali ya ku kandi"]] * 3), 8)
    assert table["layout"] == "3_columns"  # three columns in one language: a table, not the three versions
    assert split["layout"] == "parallel"
    assert [c["language"] for c in split["columns"]] == ["rw", "en", "fr"]
    assert [f["text"] for f in split["furniture"]] == ["Official Gazette n° 11 Bis of 16/03/2026"]
    assert split["columns"][1]["lines"][0]["text"] == "Article One: Purpose"


def test_an_issue_gives_each_instrument_once_with_its_title_number_date_and_pages_in_each_language():
    issue = RG.streams(RG.split_issue(law(RW, EN, FR)))
    found = RG.instruments(issue)
    assert len(found) == 1  # the title printed again over the body is the same law
    law_found = found[0]
    assert (law_found["kind"], law_found["number"], law_found["date"]) == ("law", "009/2026", "2026-03-12")
    assert law_found["versions"]["en"]["title"] == "LAW N° 009/2026 OF 12/03/2026 GOVERNING EXAMPLES"
    assert law_found["versions"]["fr"]["pages"] == [1, 2]
    constitution = {"rw": [{"text": "ITEGEKO NSHINGA RYA", "page": 2, "x0": 0, "x1": 1, "y0": 0, "y1": 1}]}
    assert RG.openings(constitution["rw"], "rw")[0]["kind"] == "constitution"


def test_each_language_version_has_its_contents_preamble_and_articles_and_they_align():
    issue = RG.streams(RG.split_issue(law(RW, EN, FR)))
    entry = RG.instruments(issue)[0]
    versions = {
        language: RG.structure(issue[language][v["start"] : v["end"]], language)
        for language, v in entry["versions"].items()
    }
    english = versions["en"]
    contents = [n for n in english if n["kind"] == "list_item"]
    assert contents[0]["marker"] == "Article One:" and contents[0]["article"] == "1"
    articles = [n for n in english if n["kind"] == "heading" and n.get("article")]
    assert [(n["marker"], n["text"]) for n in articles] == [
        ("Article One:", "Purpose"),
        ("Article 2:", "Commencement"),
    ]
    assert any(n["kind"] == "paragraph" and n["text"] == "This Law governs examples." for n in english)
    assert RG.alignment(versions) == {
        "articles": {"rw": 2, "en": 2, "fr": 2},
        "aligned": True,
        "differences": {},
    }


def test_an_article_missing_from_one_language_and_a_slip_in_a_heading_are_flagged_for_the_reviewer():
    rw = [
        "",
        "Ingingoya mbere: Icyo iri tegeko",
        "",
        "Iri tegeko rigena ingero.",
        "",
        "Ingingo ya 3: Ivanwaho",
        "",
        "Ni",
    ]
    en = [*EN, "", "Article 3: Repeal", "", "Repealed."]
    fr = [*FR, "", "Article 3 : Abrogation", "", "Abrogé."]
    issue = RG.streams(RG.split_issue(law(rw, en, fr)))
    entry = RG.instruments(issue)[0]
    versions = {
        language: RG.structure(issue[language][v["start"] : v["end"]], language)
        for language, v in entry["versions"].items()
    }
    assert RG.alignment(versions)["differences"] == {
        "en": {"missing": [], "extra": ["2"], "count": 3},
        "fr": {"missing": [], "extra": ["2"], "count": 3},
    }
    RG.flag_differences(versions)
    reading = RG.reading(versions["rw"], {}, "0" * 64, {})
    flags = [f for p in reading["pages"] for f in p["flags"]]
    missing = next(f for f in flags if f["kind"] == "not_aligned")
    assert missing["missing"] == "2" and missing["severity"] == "high" and missing["compare"]
    assert missing["checks"] == {
        "English": "Article 2: Commencement",
        "French": "Article 2 : Entrée en vigueur",
    }
    slip = next(f for f in flags if f["kind"] == "heading_form")
    assert slip["primary"] == "Ingingoya mbere:" and slip["severity"] == "low"
    # Flags point at words as the editor counts them: each unit's number, then its text.
    body = next(p for p in reading["pages"] if p["page"] == 2)
    units = [n for n in body["nodes"] if n["kind"] != "furniture"]
    words = [w for u in units for w in (u.get("marker", "").split() + u["text"].split())]
    assert words[slip["tokens"][0]] == "Ingingoya" and words[missing["tokens"][0]] == "Ingingo"
    assert reading["summary"]["flags"] == {"high": 1, "medium": 0, "low": 1}


def test_spoken_dates_in_every_form_the_gazette_prints():
    assert RG.spoken_date("in its sitting of 04 December 2023;") == "2023-12-04"
    assert RG.spoken_date("in its session of March 4 th , 2005;") == "2005-03-04"
    assert RG.spoken_date("mu nama yawo yo ku wa 08 Gicurasi 2026") == "2026-05-08"


def test_signatories_are_read_whether_the_office_is_printed_before_or_after_the_name():
    newer = [
        "Kigali, 12/03/2026",
        "(Sé)",
        "EXAMPLE Name",
        "Perezida wa Repubulika",
        "President of the Republic",
        "Président de la République",
        "(Sé)",
        "Dr SAMPLE Name",
        "Minisitiri w’Intebe",
        "Prime Minister",
        "Premier Ministre",
        "Seen and sealed with the Seal of the Republic:",
        "(Sé)",
        "OTHER Name",
        "Minister of Justice and Attorney General",
    ]
    assert RG.signatories(newer) == {
        "promulgated": "EXAMPLE Name, President of the Republic",
        "countersigned": "Dr SAMPLE Name, Prime Minister",
        "sealed": "OTHER Name, Minister of Justice and Attorney General",
    }
    older = [
        "Kigali, on 20/10/2005",
        "The President of the Republic",
        "EXAMPLE Name",
        "(sé)",
        "The Prime Minister",
        "SAMPLE Name",
        "(sé)",
        "The Minister of Education",
        "Prof. THIRD Name",
        "(sé)",
        "Seen and sealed with the Seal of the Republic:",
        "The Minister of Justice",
        "OTHER Name",
        "(sé)",
    ]
    assert RG.signatories(older) == {
        "promulgated": "EXAMPLE Name, President of the Republic",
        "countersigned": "SAMPLE Name, Prime Minister",
        "sealed": "OTHER Name, Minister of Justice",
    }


def read_law() -> tuple[dict, dict, dict]:
    """What the reader suggests for the invented law printed in issue n° 11 Bis of 16/03/2026, whose
    English version states the language it was adopted in and when it comes into force: its details,
    and apart from them its legal facts and what is inferred rather than printed for its step."""
    en = [
        "",
        "Article One: Purpose of this Law",
        "",
        "This Law governs examples.",
        "",
        "Article 2: Language provision",
        "",
        "This Law was drafted in English,",
        "considered and adopted in",
        "Ikinyarwanda.",
        "",
        "Article 3: Commencement",
        "",
        "This Law comes into force on",
        "the date of its publication.",
        "Kigali, 12/03/2026",
        "(Sé)",
        "EXAMPLE Name",
        "President of the Republic",
    ]
    pages = law(RW, en, FR)
    pages[1]["words"] += page(
        [], ["The Chamber of Deputies, in", "its sitting of 20 February", "2026;"], [], top=40
    )["words"]  # the sitting, printed above the preamble
    issue_streams = RG.streams(RG.split_issue(pages))
    entry = RG.instruments(issue_streams)[0]
    lines = {
        language: issue_streams[language][v["start"] : v["end"]] for language, v in entry["versions"].items()
    }
    return RG.suggest(lines, entry, {"date": "2026-03-16", "number": 11, "bis": True})


def test_a_law_suggests_its_record_details_from_the_text_and_the_issue():
    values, legal, inferred = read_law()
    assert values["identity.full_title"] == "Law n° 009/2026 of 12/03/2026 governing examples"
    assert values["identity.number.printed"] == "Law n° 009/2026" and values["identity.year"] == "2026"
    assert values["step:adopted:deputies.date"] == "2026-02-20"
    assert values["step:promulgated.date"] == "2026-03-12"  # the President signs under "Kigali, 12/03/2026"
    assert values["step:promulgated.person"] == "EXAMPLE Name, President of the Republic"
    assert "issued_by" not in values  # no enacting words printed here: never taken from the kind
    assert values["publication.date"] == "2026-03-16" and values["publication.number"] == "11 Bis"
    assert values["text.summary.text"] == "This Law governs examples."
    # Legal facts are suggested apart, each with the words that state it and their page; the record's
    # own fields stay empty until a reviewer chooses (owner decision 2026-10-21).
    assert [e["language"] for e in values["expressions"]] == ["rw", "en", "fr"]
    assert not any("authority" in e for e in values["expressions"])
    assert "step:commenced.date" not in values and "profile.commencement" not in values
    adopted = "This Law was drafted in English, considered and adopted in Ikinyarwanda."
    assert legal["expressions"]["authority"] == {
        "rw": {"value": "authoritative", "page": 2, "evidence": adopted},
        "en": {"value": "official_translation", "page": 2, "evidence": adopted},
        "fr": {"value": "official_translation", "page": 2, "evidence": adopted},
    }
    # The sentence that says it, not the signatures its paragraph runs on into.
    in_force = {"page": 2, "evidence": "This Law comes into force on the date of its publication."}
    assert legal["step:commenced.date"] == {"value": "2026-03-16", **in_force}
    assert legal["profile.commencement"] == {"value": "on_publication", **in_force}


def test_what_is_inferred_rather_than_printed_for_its_step_is_only_suggested_with_its_words():
    """The Gazette prints one date above the three signatures: the President's date of promulgation is
    filled in, but the Prime Minister's countersignature and the Minister of Justice's seal carry no date
    of their own. A preamble that names only the Chamber of Deputies' sitting does not say the Senate did
    not sit. Each is suggested with the words it rests on and their page, never filled in."""
    values, _, inferred = read_law()
    kigali = {"value": "2026-03-12", "page": 2, "evidence": "Kigali, 12/03/2026"}
    assert inferred["step:countersigned.date"] == inferred["step:sealed.date"] == kigali
    assert "step:countersigned.date" not in values and "step:sealed.date" not in values
    assert inferred["step:adopted:senate.date"] == {
        "absent": "not_applicable",
        "page": 2,
        "evidence": "The Chamber of Deputies, in its sitting of 20 February 2026;",
    }
    assert "step:adopted:senate.date" not in values


def test_who_adopted_a_law_is_read_from_its_enacting_words_never_from_its_kind():
    def lines(*texts):
        return [{"text": text, "page": 3, "x0": 0, "y0": 10 + 14 * i, "x1": 200, "y1": 20 + 14 * i}
                for i, text in enumerate(texts)]  # fmt: skip

    entry = {"kind": "law", "number": "03/99", "date": "1999-03-12", "versions": {"en": {"title": "LAW"}}}

    def issued_by(*texts):
        preamble = lines("We, EXAMPLE Name,", "President of the Republic;", *texts)
        return RG.suggest({"en": preamble}, entry, {})[0]

    assert issued_by("THE PARLIAMENT HAS ADOPTED AND WE SANCTION,")["issued_by"] == "Parliament"
    older = issued_by("THE TRANSITIONAL NATIONAL ASSEMBLY HAD ADOPTED AND", "WE SANCTION, PROMULGATE")
    assert older["issued_by"] == "Transitional National Assembly"
    assert "issued_by" not in issued_by("This Law governs examples.")  # not printed: not filled in


def test_a_legal_fact_is_suggested_only_with_the_words_that_state_it():
    paragraphs = [
        {"page": 4, "text": "Nothing here."},
        {
            "page": 5,
            "text": "Law n° 1/2001 is repealed. This Law comes into force on\nthe date of its publication.",
        },
    ]
    assert RG.stated(paragraphs, RG.IN_FORCE_ON_PUBLICATION) == {
        "page": 5,
        "evidence": "This Law comes into force on the date of its publication.",
    }
    assert RG.stated(paragraphs[:1], RG.IN_FORCE_ON_PUBLICATION) is None  # nothing to quote: no suggestion
    run_on = [{"page": 6, "text": "This Law comes into force on the date of its publication " + "and " * 120}]
    assert RG.stated(run_on, RG.IN_FORCE_ON_PUBLICATION) is None  # no sentence short enough to quote


def test_numbered_items_within_an_article_keep_their_numbers():
    lines = [
        {"text": "We, EXAMPLE Name,", "page": 1, "x0": 0, "y0": 10, "x1": 100, "y1": 20},
        {"text": "Ingingo ya 2: Ibisobanuro", "page": 1, "x0": 0, "y0": 40, "x1": 100, "y1": 50},
        {"text": "1º “Ishuri” ni urwego;", "page": 1, "x0": 0, "y0": 70, "x1": 100, "y1": 80},
        {"text": "rutanga inyigisho;", "page": 1, "x0": 0, "y0": 82, "x1": 100, "y1": 92},
        {"text": "2° “Ikigo” ni urwego.", "page": 1, "x0": 0, "y0": 110, "x1": 100, "y1": 120},
        {"text": "(a) is an orphan;", "page": 1, "x0": 0, "y0": 140, "x1": 100, "y1": 150},
    ]
    items = [n for n in RG.structure(lines, "rw") if n["kind"] == "list_item"]
    assert [(n["marker"], n["text"]) for n in items] == [
        ("1º", "“Ishuri” ni urwego; rutanga inyigisho;"),
        ("2°", "“Ikigo” ni urwego."),
        ("(a)", "is an orphan;"),
    ]


def test_a_wrapped_number_in_a_sentence_is_not_an_item_and_a_raised_o_is_an_ordinal():
    lines = [
        {"text": "We, EXAMPLE Name,", "page": 1, "x0": 0, "y0": 10, "x1": 100, "y1": 20},
        {"text": "Article 2: Option", "page": 1, "x0": 0, "y0": 40, "x1": 100, "y1": 50},
        {
            "text": "The decision is irrevocable for a period of three",
            "page": 1,
            "x0": 0,
            "y0": 70,
            "x1": 100,
            "y1": 80,
        },
        {"text": "(3) years from the date of notice.", "page": 1, "x0": 0, "y0": 82, "x1": 100, "y1": 92},
    ]
    nodes = RG.structure(lines, "en")
    assert [n["kind"] for n in nodes][-1] == "paragraph"
    assert nodes[-1]["text"].endswith("three (3) years from the date of notice.")
    words = [
        {"xmin": 0, "ymin": 10, "xmax": 20, "ymax": 20, "text": "Loi"},
        {"xmin": 24, "ymin": 10, "xmax": 30, "ymax": 20, "text": "n"},
        {"xmin": 31, "ymin": 9, "xmax": 34, "ymax": 15, "text": "o"},
        {"xmin": 37, "ymin": 10, "xmax": 70, "ymax": 20, "text": "71/2024"},
    ]
    assert RG.lines_of(words)[0]["text"] == "Loi nº 71/2024"


def split_page_of(number: int, *columns: list[str]) -> dict:
    """A split page as the splitter gives it: one column per language list (or one, unlabelled)."""
    labels = list(RG.LANGUAGES) if len(columns) == 3 else ["en"]
    return {
        "page": number,
        "layout": "parallel" if len(columns) == 3 else "single",
        "furniture": [],
        "columns": [
            {
                "language": language,
                "x0": 0,
                "x1": 1,
                "lines": [
                    {"text": text, "x0": 0, "y0": 10 * k, "x1": 1, "y1": 10 * k + 8}
                    for k, text in enumerate(lines)
                ],
            }
            for language, lines in zip(labels, columns, strict=True)
        ],
    }


def test_an_annex_runs_from_its_formal_title_to_its_attestation_and_leaves_the_order_alone():
    split = [
        split_page_of(
            1,
            ["ITEKA RYA PEREZIDA N° 117/01"],
            ["PRESIDENTIAL ORDER N° 117/01"],
            ["ARRÊTÉ PRÉSIDENTIEL N° 117/01"],
        ),
        split_page_of(
            2,
            ["UMUGEREKA W’ITEKA RYA PEREZIDA"],
            ["ANNEX TO PRESIDENTIAL ORDER"],
            ["ANNEXE A L’ARRETE PRESIDENTIEL"],
        ),
        split_page_of(3, ["AIR SERVICES AGREEMENT"]),
        split_page_of(4, ["ANNEX I: ROUTE SCHEDULE"]),  # the agreement's own annex: the same annex goes on
        split_page_of(
            5,
            ["Bibonywe kugira ngo bishyirwe ku mugereka"],
            ["Seen to be annexed to Presidential Order"],
            ["Vu pour être annexé à l’Arrêté Présidentiel"],
        ),
        split_page_of(
            6,
            ["ITEKA RYA PEREZIDA N° 118/01"],
            ["PRESIDENTIAL ORDER N° 118/01"],
            ["ARRÊTÉ PRÉSIDENTIEL N° 118/01"],
        ),
    ]
    roles = RG.annex_pages(split)
    assert roles == {2: 1, 3: 1, 4: 1, 5: 1}
    streams = RG.streams(split, skip=set(roles))
    assert [line["page"] for line in streams["en"]] == [1, 6]
    annex = RG.annexes(split, roles)[0]
    assert annex["pages"] == [2, 3, 4, 5] and "AIR SERVICES AGREEMENT" in [
        x["text"] for x in annex["versions"]["en"]
    ]


def test_a_number_misprinted_over_the_contents_is_the_same_law_but_two_laws_with_bodies_stay_two():
    def lines(*texts):
        return [
            {"text": t, "page": 1 + k // 5, "x0": 0, "y0": k, "x1": 1, "y1": k + 1}
            for k, t in enumerate(texts)
        ]

    misprinted = lines(
        "LAW N° 066/2024 OF 26/06/2024 DETERMINING THE STATE FINANCES",
        "TABLE OF CONTENTS",
        "Article One: Revenue projections",
        "LAW N° 064/2024 OF 26/06/2024 DETERMINING THE STATE FINANCES",
        "We, EXAMPLE Name,",
        "Article One: Revenue projections",
    )
    found = RG.instruments({"rw": [], "en": misprinted, "fr": []})
    assert [(f["number"], f.get("also_printed_as")) for f in found] == [("064/2024", "066/2024")]
    assert found[0]["versions"]["en"]["start"] == 0
    two = lines(
        "LAW N° 043/2026 OF 03/09/2026 APPROVING THE RATIFICATION",
        "We, EXAMPLE Name,",
        "Article One: Approval",
        "LAW N° 044/2026 OF 03/09/2026 APPROVING THE RATIFICATION",
        "We, EXAMPLE Name,",
        "Article One: Approval",
    )
    assert [f["number"] for f in RG.instruments({"rw": [], "en": two, "fr": []})] == ["043/2026", "044/2026"]


def test_a_scanned_page_keeps_its_layout_and_takes_document_ais_words_with_differences_noted():
    lines = [
        {"text": "Desireux de promouvoir un systerne de", "page": 1, "x0": 0, "y0": 10, "x1": 1, "y1": 18},
        {
            "text": "transport adrien international ; d’une part",
            "page": 1,
            "x0": 0,
            "y0": 20,
            "x1": 1,
            "y1": 28,
        },
    ]
    better = "Désireux de promouvoir un système de transport aérien international ; d'une part ;"
    fixed = RG.correct_lines(lines, better)
    assert [line["text"] for line in fixed] == [
        "Désireux de promouvoir un système de",
        "transport aérien international ; d’une part ;",  # the printed apostrophe kept
    ]
    assert fixed[0]["differences"] == [
        ("Désireux", "Desireux", "OCR layer"),
        ("système", "systerne", "OCR layer"),
    ]
    assert fixed[1]["differences"] == [("aérien", "adrien", "OCR layer")]  # a lone ";" is not a difference
    nodes = RG.structure([{**line, "text": "We, EXAMPLE,"} for line in fixed[:1]] + fixed, "en")
    reading = RG.reading(nodes, {}, "0" * 64, {})
    flags = [f for page in reading["pages"] for f in page["flags"]]
    assert {f["kind"] for f in flags} == {"reading_differs", "accents_restored"}
    words = next(f for f in flags if f["kind"] == "reading_differs")
    assert "aérien" in words["primary"] and "Désireux" not in words["primary"]  # accents listed apart
    # Numbering only the OCR layer has (Document AI leaves it out) is kept; symbol noise is not.
    kept = RG.correct_lines([{"text": "a) les aéronefs ■ .:-", "page": 1}], "les aéronefs")
    assert kept[0]["text"] == "a) les aéronefs"


def test_a_scan_read_by_our_ocr_is_laid_out_like_a_text_layer_and_flagged_until_checked():
    from ingestion import gazette_layout

    page = {
        "width": 600.0,
        "height": 800.0,
        "words": [{"text": "noise", "xmin": 1, "ymin": 1, "xmax": 9, "ymax": 9}],
    }
    read = {"words": [{"text": "a)", "x0": 0.1, "y0": 0.5, "x1": 0.12, "y1": 0.52, "confidence": 0.99}]}
    placed = gazette_layout.from_ocr(read, page)
    assert placed["read_by"] == "ocr" and placed["width"] == 600.0
    assert placed["words"] == [
        {"xmin": 60.0, "ymin": 400.0, "xmax": 72.0, "ymax": 416.0, "text": "a)", "confidence": 0.99}
    ]
    # A page with no lines at all (no text layer, no OCR) cannot take a second reading's words.
    empty = {"page": 3, "layout": "single", "columns": [{"lines": []}]}
    assert RG.correct_page(empty, {"text": "Ingingo ya mbere", "nodes": []}) is empty
    nodes = [{"kind": "paragraph", "text": "Ingingo ya mbere", "page": 3}]
    flags = RG.reading(nodes, {}, "0" * 64, {}, one_reading={3})["pages"][0]["flags"]
    assert [f["kind"] for f in flags] == ["one_reading"]


def test_our_ocr_keeps_what_it_read_surely_and_takes_the_second_reading_where_it_was_unsure():
    """As printed: the Gazette's own slip ("chirurigicales") and its numbering stand; broken type
    the OCR was unsure of takes the Layout Parser's word; every difference names its reading."""
    words = [
        ("1°)", 0.99, 40),
        ("interventions", 0.99, 80),
        ("chirurigicales", 0.97, 200),
        ("amara.а...у.", 0.45, 330),
        ("yo", 0.99, 420),
    ]
    page_words = [
        {"text": t, "confidence": c, "xmin": x, "xmax": x + 60, "ymin": 100, "ymax": 110} for t, c, x in words
    ]
    lines = [{**line, "page": 1} for line in RG.lines_of(page_words)]
    assert lines[0]["doubtful"] == ["amara.а...у."]
    fixed = RG.correct_lines(lines, "interventions chirurgicales amafaranga yo")
    assert fixed[0]["text"] == "1°) interventions chirurigicales amafaranga yo"
    assert fixed[0]["differences"] == [
        ("chirurigicales", "chirurgicales", "Layout Parser"),
        ("amafaranga", "amara.а...у.", "OCR"),
    ]
    nodes = RG.structure(fixed, "fr")
    flags = [f for page in RG.reading(nodes, {}, "0" * 64, {})["pages"] for f in page["flags"]]
    differs = next(f for f in flags if f["kind"] == "reading_differs")
    assert differs["checks"] == {"Layout Parser": "chirurgicales", "OCR": "amara.а...у."}
    # The second reading taking a passage in another order adds nothing twice.
    two = RG.lines_of(
        [
            {"text": t, "confidence": 0.99, "xmin": x, "xmax": x + 50, "ymin": y, "ymax": y + 10}
            for t, x, y in [
                ("Iteka", 40, 100),
                ("rya", 100, 100),
                ("Arrêté", 40, 120),
                ("ministériel", 100, 120),
            ]
        ]
    )
    again = RG.correct_lines(two, "Arrêté ministériel Iteka rya")
    assert " ".join(line["text"] for line in again).split().count("Iteka") == 1


def test_running_headers_of_every_era_are_page_furniture_and_text_is_not():
    for header in [
        "J.O. n°9 du 1/5/2001 -24-",
        "J.O.n* special du 04/06/2003 -60-",  # the OCR's "n*" for n°
        "- 301 - J.O. n° 7 du 1er avril 1975.",
        "Official Gazette nº 15 of 13/04/2015",
        "Igazeti ya Leta n° 27 yo ku wa 05/07/2021",
        "- 52 -",
        "200",
    ]:
        assert RG.FURNITURE.match(header), header
    for text in [
        "Law n° 11/2001 of 8/2/2001 modifying the law of july 15, 1964",
        "Official Gazette of the Republic of Rwanda",
        "J.O. is the old name",
        "Ingingo ya 10",
    ]:
        assert not RG.FURNITURE.match(text), text


def test_older_headings_open_their_instrument_numbered_or_known_by_their_date():
    def opened(text, language):
        lines = [{"text": text, "page": 1}, {"text": "Le Ministre de l'Agriculture,", "page": 1}]
        return RG.openings(lines, language)

    decree = opened("DÉCRET-LOI N° 10/75 DU 11 MARS 1975 PORTANT ORGANISATION DE LA PRÉFECTURE", "fr")[0]
    assert (decree["kind"], decree["number"], decree["date"]) == ("decree_law", "10/75", "1975-03-11")
    dated = opened("LOI DU 19 FÉVRIER 1964 RELATIVE AUX SOINS MÉDICAUX", "fr")[0]
    assert (dated["kind"], dated["number"], dated["date"]) == ("law", "1964-02-19", "1964-02-19")
    assert opened("ORDONNANCE N° 53/351 DU 9 JUILLET 1959", "fr")[0]["kind"] == "ordinance"
    assert opened("ITEGEKO-TEKA N° 10/75 RYO KUWA 11 WERURWE 1975", "rw")[0]["kind"] == "decree_law"
    assert (
        opened("ITEKA RYA MINISTRI N° 1/71/Agri RYO KUWA 30 MARISI 1971", "rw")[0]["kind"]
        == "ministerial_order"
    )
    assert not opened("LOI DU PAYS", "fr")  # no number and no date: not a heading
    # A date in figures on older pages, and a number that only looks like one.
    assert RG._plain_date("J.O. N0 5-1.3.1964") == "1964-03-01"
    assert RG._plain_date("D.L N0 06-81-1981") is None


def test_a_file_whose_heading_is_not_known_is_one_instrument_named_as_its_source_lists_it():
    from ingestion import rwanda

    words = "Il est fixé des taxes rémunératoires de propagande et de diversification sur les cafés exportés"
    streams = {
        "fr": [{"text": words, "page": 1}, {"text": words, "page": 2}],
        "rw": [{"text": "Ingingo", "page": 1}],  # a stray line is not a language printed here
        "en": [],
    }
    title = rwanda.listing_title("8.7.22._TAXES_REMUNERATOIRES__DE_PROPAGANDE-J.O._N0_5-1.3.1971.pdf")
    assert title == "TAXES REMUNERATOIRES DE PROPAGANDE-J.O. N0 5-1.3.1971"
    [entry] = RG.whole_file(streams, "ministerial_order", "1-71", None, title)
    assert entry["title_from_listing"] and list(entry["versions"]) == ["fr"]
    assert entry["versions"]["fr"]["pages"] == [1, 2] and entry["versions"]["fr"]["end"] == 2


def test_unsure_ocr_words_left_out_are_a_low_flag_not_a_word_to_check():
    node = {
        "kind": "paragraph",
        "text": "The Prime Minister",
        "page": 1,
        "differences": [("", "possent Isage શ્રોE", "OCR"), ("Minister", "Mimster", "OCR")],
    }
    flags = RG.reading([node], {}, "0" * 64, {})["pages"][0]["flags"]
    kinds = {f["kind"]: f["severity"] for f in flags}
    assert kinds == {"unsure_words_dropped": "low", "reading_differs": "medium"}


def _lines(*pages: list[str]) -> list[dict]:
    """Lines laid out as printed: each page's lines 14 pt apart (a new paragraph each)."""
    return [
        {"text": text, "page": number, "x0": 0, "x1": 300, "y0": 20 * i, "y1": 20 * i + 10}
        for number, texts in enumerate(pages, 2)
        for i, text in enumerate(texts)
    ]


def test_a_treaty_reads_as_articles_on_their_own_pages_not_one_title():
    # RLRC's international instruments open on "PREAMBULE", not "Nous, …", and number articles
    # in Roman numerals: once read as one 14,000-character title on the first page (2,908 versions).
    lines = _lines(
        ["PREAMBULE", "Les États parties au présent Protocole,", "CONVAINCUS de l'importance des immunités,"],
        [
            "Article I: Définition",
            "Aux fins du présent protocole, « Fonctionnaire » signifie toute personne.",
        ],
        ["Article XIV : Entrée en vigueur", "Le présent Protocole entre en vigueur à sa signature."],
    )
    nodes = RG.structure(lines, "fr")
    assert {n["page"] for n in nodes} == {2, 3, 4}
    articles = [(n["article"], n["text"], n["page"]) for n in nodes if n.get("article")]
    assert articles == [("1", "Définition", 3), ("14", "Entrée en vigueur", 4)]
    assert not any(n.get("unusual") for n in nodes)


def test_a_title_never_runs_on_to_another_page():
    # Whatever opened the body went unrecognised: the title still ends with its page.
    lines = _lines(
        ["ITEKA RYA MINISITIRI N° 001 RYO KUWA 01/01/2020", "RIGENA IBIJYANYE N'IBINDI"],
        ["Ingingo ya mbere: Icyo iri teka rigamije", "Iri teka rigena ibijyanye n'ibindi."],
    )
    nodes = RG.structure(lines, "rw")
    assert nodes[0]["kind"] == "heading" and nodes[0]["page"] == 2 and "Ingingo" not in nodes[0]["text"]
    assert [n["article"] for n in nodes if n.get("article")] == ["1"]
    assert all(n["page"] == 3 for n in nodes[1:])


def _two_columns(left: list[str], right: list[str], centred: list[str] = ()) -> dict:
    """A portrait page with French on the left and Kinyarwanda on the right, as the older Gazette
    printed them, and lines centred across both (a signature, a date)."""
    width, height, words = 595.0, 842.0, []

    def put(text, x, y):
        for word in text.split():
            w = 5.0 * len(word)
            words.append({"xmin": x, "ymin": y, "xmax": x + w, "ymax": y + 10, "text": word})
            x += w + 4

    y = 60.0
    for a, b in zip(left, right, strict=False):
        put(a, 40.0, y)
        put(b, 310.0, y)
        y += 14
    for line in centred:
        put(line, 297.0 - 2.5 * len(line), y)
        y += 14
    return {"width": width, "height": height, "words": words}


def test_a_short_bilingual_page_under_a_centred_signature_keeps_its_two_languages():
    # A page of a few side-by-side lines under a centred signature (rw-decree-law-20-76) is split on
    # its own columns. (On the printed page its gutter is measured off-centre; borrowing the earlier
    # page's columns for it lost thousands of orders elsewhere, so that page is left to the reviewer.)
    # Lines of up to 40 characters: each column's half of the page, as printed.
    fr = ["Le présent décret-loi sort ses effets", "à la date de sa signature et de la loi"]
    rw = ["Iri tegeko-teka ritangira gukurikizwa", "ku munsi ryashyiriweho umukono na ya"]
    first = _two_columns(
        ["Décret-loi n° 20/76 du 20 avril 1976", *fr * 8],
        ["Itegeko-teka nº 20/76 ryo kuwa 20 mata", *rw * 8],
    )
    second = _two_columns(fr, rw, centred=["Kigali, le 20 avril 1976.", "HABYARIMANA J."])
    pages = RG.split_issue([first, second])
    assert [c["language"] for c in pages[1]["columns"]] == ["fr", "rw"]
    left, right = (" ".join(line["text"] for line in c["lines"]) for c in pages[1]["columns"])
    assert "ritangira gukurikizwa" in right and "ritangira" not in left
    assert "décret-loi sort ses effets" in left and "décret-loi" not in right


def test_older_french_articles_abbreviated_art_are_articles_and_a_reference_is_not():
    # The 1970s Gazette heads French articles "Art. 2." (rw-decree-law-20-76 counted 1 article in
    # French against 4 in Kinyarwanda); "Art. 3 de la loi …" opening a wrapped line is a reference.
    lines = _lines(
        ["Décret-loi n° 20/76 du 20 avril 1976 octroyant une prime.", "Nous, HABYARIMANA Juvénal,"],
        [
            "Art. 1er.",
            "Toute personne qui aura découvert un gisement bénéficiera d'une prime.",
            "Art. 2.",
            "L'importance d'un gisement est appréciée.",
            "Art. 3 de la loi du 27 avril 1971 reste applicable.",
            "Art. 3.",
            "Le présent décret-loi sort ses effets à la date de sa signature.",
        ],
    )
    nodes = RG.structure(lines, "fr")
    assert [n["article"] for n in nodes if n.get("article")] == ["1", "2", "3"]


def test_a_repealed_article_amends_its_law_and_a_repealed_law_is_repealed():
    lines = [
        {"text": text}
        for text in [
            "Having reviewed Law n° 5/2001 of 01/02/2001 governing examples;",
            "Article One: Working hours",
            "Article 43 of the Law n° 066/2018 of 30/08/2018 regulating labour in Rwanda is amended as follows:",
            "“The working hours are determined by an Order.”",
            "Article 2: Weekly rest",
            "Article 44 of Law n° 66/2018 of 30/08/2018 regulating labour in Rwanda is repealed.",
            "Article 3: Repealing provision",
            "Law nº 028/2023 of 19/05/2023 on the prevention of money laundering, as amended by Law",
            "n° 9/2024 of 02/03/2024, and Presidential Order n° 046/01 of 29/05/2019 determining examples",
            "are hereby repealed. All prior legal provisions contrary to this Law are repealed.",
            "Article 4: Saving and repealing provisions",
            "Save Article 163 of Law n° 13/2009 of 27/05/2009 regulating labour in Rwanda, its other",
            "provisions as well as other prior legal provisions contrary to this Law are repealed.",
        ]
    ]
    entry = {
        "kind": "law",
        "number": "027/2026",
        "versions": {
            "en": {"title": "LAW N° 027/2026 OF 18/05/2026 AMENDING THE LAW N° 66/2018 OF 30/08/2018"}
        },
    }
    found = {(c["kind"], c["instrument"], c["number"], c["year"]): c for c in RG.cited({"en": lines}, entry)}
    assert set(found) == {
        ("amends", "law", "066/2018", "2018"),
        ("repeals", "law", "028/2023", "2023"),
        ("repeals", "presidential_order", "046/01", "2019"),
        ("repeals", "law", "13/2009", "2009"),
    }  # not the law it was made under, nor the one that only amended the law it repeals
    labour = found["amends", "law", "066/2018", "2018"]
    assert labour["target"] == "Law n° 066/2018 of 30/08/2018 regulating labour in Rwanda"
    assert labour["note"] == "Articles 43 and 44"
    assert found["repeals", "law", "028/2023", "2023"]["target"] == (
        "Law nº 028/2023 of 19/05/2023 on the prevention of money laundering"
    )
    assert "note" not in found["repeals", "presidential_order", "046/01", "2019"]
    saved = found["repeals", "law", "13/2009", "2009"]  # all but one article
    assert (saved["target"], saved["note"]) == (
        "Law n° 13/2009 of 27/05/2009 regulating labour in Rwanda",
        "Except Article 163",
    )


def test_a_title_names_the_law_it_amends_and_a_french_text_is_read_where_there_is_no_english():
    entry = {
        "kind": "law",
        "number": "43/2018",
        "versions": {
            "fr": {
                "title": "LOI N° 43/2018 DU 13/08/2018 MODIFIANT LA LOI N° 19/2008 DU 14/07/2008 RELATIVE À L’HYMNE"
            }
        },
    }
    lines = [
        {
            "text": "Article 2 : L’article 7 ter de la Loi n° 19/2008 du 14/07/2008 relative à l’hymne est abrogé."
        }
    ]
    [link] = RG.cited({"fr": lines, "rw": [{"text": "Ingingo ya 2"}]}, entry)
    assert (link["kind"], link["number"], link["note"]) == ("amends", "19/2008", "Article 7 ter")
    assert link["target"] == "Loi n° 19/2008 du 14/07/2008 relative à l’hymne"
    [title] = RG.cited({"fr": [{"text": ""}]}, entry)  # only the title: the law it amends, itself left out
    assert (title["kind"], title["number"]) == ("amends", "19/2008")
    assert title["target"].startswith("Loi n° 19/2008 du 14/07/2008")


def test_a_bis_number_is_its_own_law_not_the_law_with_the_plain_number():
    def number(text, language="en"):
        return RG.openings([{"text": text, "page": 1}], language)[0]["number"]

    assert number("LAW N° 001 BIS /2021 OF 04/02/2021 APPROVING RATIFICATION") == "001bis/2021"
    assert number("LOI N° 001 bis/2021 DU 04/02/2021 APPROUVANT", "fr") == "001bis/2021"
    assert number("LAW N°53BIS/2013 OF 01/01/2013 GOVERNING EXAMPLES") == "53bis/2013"
    assert number("LAW N° 001/2021 OF 04/02/2021 GOVERNING EXAMPLES") == "001/2021"
    assert number("LAW N° 12 OF 2001 GOVERNING EXAMPLES") == "12"
    assert number("MINISTERIAL ORDER N° 001/19/10/TC OF 15/03/2019 X") == "001/19/10/TC"
    [link] = RG.cited(
        {"en": [{"text": "Law n° 001 bis /2021 of 04/02/2021 approving examples is repealed."}]},
        {"kind": "law", "number": "1/2022", "versions": {}},
    )
    assert (link["number"], RG.number_key(link["number"])) == ("001 bis /2021", "1-bis-2021")


def test_kinyarwanda_texts_and_preambles_give_what_a_text_repeals_amends_and_is_made_under():
    rw = [
        {"text": text}
        for text in [
            "Ashingiye ku Itegeko nº 20/2000 ryo kuwa 26 Nyakanga 2000 ryerekeye imiryango idaharanira",
            "inyungu, cyane cyane mu ngingo yaryo ya 8, iya 9 n’iya 10;",
            "Ishingiye ku masezerano yemejwe n’Iteka rya Perezida nº 47/01 ryo ku wa 14/04/2002;",
            "Ingingo ya 3: Ingingo ya 44 y’Itegeko n° 66/2018 ryo ku wa 30/08/2018 rigenga umurimo mu",
            "Rwanda ivanyweho.",
            "Ingingo ya 75: Ingingo ivanaho Itegeko nº 028/2023 ryo ku wa 19/05/2023 ryerekeye gukumira",
            "iyezandonke rivanyweho. Ingingo ya 76: Ingingo izigama",
        ]
    ]
    entry = {
        "kind": "ministerial_order",
        "number": "114/11",
        "versions": {"rw": {"title": "ITEKA RYA MINISITIRI"}},
    }
    found = {(c["kind"], c["instrument"], c["number"], c["year"]): c for c in RG.cited({"rw": rw}, entry)}
    assert set(found) == {
        ("made_under", "law", "20/2000", "2000"),  # not the order a treaty was ratified by
        ("amends", "law", "66/2018", "2018"),
        ("repeals", "law", "028/2023", "2023"),
    }
    assert found["made_under", "law", "20/2000", "2000"]["note"] == "Articles 8, 9 and 10"
    assert found["made_under", "law", "20/2000", "2000"]["target"] == (
        "Itegeko nº 20/2000 ryo kuwa 26 Nyakanga 2000 ryerekeye imiryango idaharanira inyungu"
    )
    assert found["amends", "law", "66/2018", "2018"]["note"] == "Article 44"


def test_an_order_is_made_under_the_laws_its_preamble_names_first_in_each_language():
    en = [{"text": "Pursuant to Law n° 70/2019 of 10/01/2020 governing metrology, especially in Articles 4 and 9;"},
          {"text": "Pursuant to the Presidential Order N°27/01 of 18/07/2004, determining certain orders,"},
          {"text": "especially in Article One; Reviewing Prime Minister’s Order nº 96/03 of 25/08/2011;"}]  # fmt: skip
    fr = [
        {
            "text": "Vu la Loi n° 10/2011 du 13/05/2011 déterminant les missions, spécialement en son article 12 ;"
        }
    ]
    entry = {"kind": "ministerial_order", "number": "002/MINICOM/23", "versions": {}}
    made = [(c["instrument"], c["number"], c.get("note")) for c in RG.cited({"en": en}, entry)]
    assert made == [("law", "70/2019", "Articles 4 and 9"), ("presidential_order", "27/01", "Article 1")]
    [french] = RG.cited({"fr": fr}, entry)
    assert (french["kind"], french["number"], french["note"]) == ("made_under", "10/2011", "Article 12")
    assert french["target"] == "Loi n° 10/2011 du 13/05/2011 déterminant les missions"


def test_a_law_named_in_the_title_of_a_repealed_order_is_not_itself_repealed():
    lines = [{"text": "Article 60: Repealing provision Ministerial Order nº 005/12/10/TC of 22/06/2012 determining the"},
             {"text": "modalities for the implementation of Law nº 59/2011 of 31/12/2011 establishing revenue and all"},
             {"text": "prior provisions contrary to this Order are repealed."}]  # fmt: skip
    entry = {"kind": "ministerial_order", "number": "008/19/10/TC", "versions": {}}
    [link] = RG.cited({"en": lines}, entry)
    assert (link["kind"], link["instrument"], link["number"]) == (
        "repeals",
        "ministerial_order",
        "005/12/10/TC",
    )


def test_each_page_is_split_on_its_own_columns_not_an_earlier_pages():
    # Pipelines 2026-10-18.1 to .3 laid an earlier page's columns over a page whose own sat elsewhere
    # when the earlier gutters seemed to hold: lines were cut across languages, 3,588 orders lost.
    def three(spans):
        """A landscape page of the three languages, in columns at these places."""
        words = []
        for (x0, _), lines in zip(spans, (RW * 3, EN * 3, FR * 3), strict=True):
            y = 70.0
            for line in lines:
                x = x0
                for word in line.split():
                    words.append(
                        {"xmin": x, "ymin": y, "xmax": x + 5.0 * len(word), "ymax": y + 10, "text": word}
                    )
                    x += 5.0 * len(word) + 4
                y += 14
        return {"width": 842.0, "height": 595.0, "words": words}

    first = three([(70, 290), (300, 520), (530, 770)])
    second = three([(40, 250), (320, 520), (590, 800)])  # its own three columns, elsewhere
    pages = RG.split_issue([first, second])
    assert [(c["x0"], c["x1"]) for c in pages[1]["columns"]] != [
        (c["x0"], c["x1"]) for c in pages[0]["columns"]
    ]
    assert len(pages[1]["columns"]) == 3


def test_a_page_whose_measure_leaves_a_column_empty_takes_the_page_befores_languages_side_by_side(
    monkeypatch,
):
    # rw-decree-law-20-76, page 2: the signature centred across the gutter moved the measured gutter to
    # the margin, leaving the Kinyarwanda column empty and the Kinyarwanda in the French.
    fr = ["Le présent décret-loi sort ses effets", "à la date de sa signature et de la loi"]
    rw = ["Iri tegeko-teka ritangira gukurikizwa", "ku munsi ryashyiriweho umukono na ya"]
    first = _two_columns(
        ["Décret-loi n° 20/76 du 20 avril 1976", *fr * 8], ["Itegeko-teka nº 20/76", *rw * 8]
    )
    second = _two_columns(fr * 2, rw * 2, centred=["Kigali, le 20 avril 1976.", "HABYARIMANA J."])
    measured = RG.gazette_layout.columns
    margin = [(38.0, 560.0), (565.0, 595.0)]  # the measure's gutter at the margin: one column nearly empty
    monkeypatch.setattr(
        RG.gazette_layout,
        "columns",
        lambda page: margin if page["words"] == second["words"] else measured(page),
    )
    pages = RG.split_issue([first, second])
    assert [c["language"] for c in pages[1]["columns"]] == ["fr", "rw"]
    left, right = (" ".join(line["text"] for line in c["lines"]) for c in pages[1]["columns"])
    assert "ritangira gukurikizwa" in right and "ritangira" not in left
    # After a page of one column, or of a table's columns, nothing is taken: the page keeps its own.
    table = _two_columns(["Administrative Unit"] * 12, ["Job Title Director of Unit"] * 12)
    lone = RG.split_issue([table, second])[1]
    assert [(c["x0"], c["x1"]) for c in lone["columns"]] == margin
