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
    "Iri tegeko rigena ingero.",
    "",
    "Ingingo ya 2: Igihe",
    "Ritangira.",
]
EN = ["", "Article One: Purpose", "This Law governs examples.", "", "Article 2: Commencement", "It begins."]
FR = [
    "",
    "Article premier : Objet",
    "La présente loi régit.",
    "",
    "Article 2 : Entrée en vigueur",
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
        "Iri tegeko rigena ingero.",
        "",
        "Ingingo ya 3: Ivanwaho",
        "Ni",
    ]
    en = [*EN, "", "Article 3: Repeal", "Repealed."]
    fr = [*FR, "", "Article 3 : Abrogation", "Abrogé."]
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
