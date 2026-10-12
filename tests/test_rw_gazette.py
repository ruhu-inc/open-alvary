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


def test_a_law_suggests_its_record_details_from_the_text_and_the_issue():
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
    values = RG.suggest(lines, entry, {"date": "2026-03-16", "number": 11, "bis": True})
    assert values["identity.full_title"] == "Law n° 009/2026 of 12/03/2026 governing examples"
    assert values["identity.number.printed"] == "Law n° 009/2026" and values["identity.year"] == "2026"
    assert values["step:adopted:deputies.date"] == "2026-02-20"
    assert values["step:promulgated.date"] == values["step:sealed.date"] == "2026-03-12"
    assert values["step:promulgated.person"] == "EXAMPLE Name, President of the Republic"
    assert values["step:commenced.date"] == values["publication.date"] == "2026-03-16"
    assert values["publication.number"] == "11 Bis"
    assert values["text.summary.text"] == "This Law governs examples."
    assert [(e["language"], e["authority"]) for e in values["expressions"]] == [
        ("rw", "authoritative"),
        ("en", "official_translation"),
        ("fr", "official_translation"),
    ]


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
