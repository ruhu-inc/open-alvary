"""Where every printed line of a source went (ingestion.accounting; owner, 2026-10-29): what reaches a
document, what is set apart as furniture, and what reaches nothing, with the reason, so that losses
are measured before they are fixed."""

from ingestion import accounting, nicn


def line(text, **more):
    return {"text": text, **more}


def column(language, *texts):
    return {"language": language, "x0": 0, "x1": 100, "lines": [line(t) for t in texts]}


def page(number, columns, furniture=()):
    return {"page": number, "columns": columns, "furniture": [line(t) for t in furniture]}


# A treaty from the RLRC: a cover page whose title lines span the page (one column of three languages,
# so no language), then the treaty in three columns.
COVER = page(
    1,
    [
        column(
            None,
            "TREATY OF THE AFRICAN MEDICINES AGENCY (AMA)",
            "Adopted in Addis Ababa, on 12th June 2019",
            "Ratified by the Presidential Order n° 119/01 of 6th November 2019",
        )
    ],
)
BODY = page(
    2,
    [
        column("rw", "IRANGASHINGIRO", "Twebwe, Ibihugu bigize Umuryango"),
        column("en", "PREAMBLE", "We, Member States of the African Union,"),
        column("fr", "PRÉAMBULE", "Nous, États membres de l'Union africaine,"),
    ],
    furniture=["2"],
)


def versions(end=2):
    return {lang: {"title": "Treaty", "start": 0, "end": end, "pages": [2, 2]} for lang in ("rw", "en", "fr")}


def test_a_cover_page_with_no_language_is_counted_as_reaching_no_document():
    found = [{"number": "x", "kind": "agreement", "versions": versions(), "title_from_listing": True}]
    report = accounting.rwanda_issue([COVER, BODY], {}, found, set(), page_count=2)
    assert report["lines"] == {"unplaced": 3, "furniture": 1, "document": 6}
    assert report["pages_unplaced"] == [1] and report["first_page_unplaced"]
    cover = report["unplaced"]["no_language"]
    assert cover["lines"] == 3 and cover["pages"] == [1]
    assert cover["samples"][0] == {"page": 1, "text": "TREATY OF THE AFRICAN MEDICINES AGENCY (AMA)"}
    assert report["first_document_page"] == 2 and report["title_from_listing"]


def test_lines_outside_every_instrument_say_where_they_were():
    # The English version starts at its second line; French has no version; Kinyarwanda is whole.
    found = [
        {
            "number": "x",
            "kind": "law",
            "versions": {
                "rw": {"start": 0, "end": 2, "pages": [2, 2]},
                "en": {"start": 1, "end": 2, "pages": [2, 2]},
            },
        }
    ]
    report = accounting.rwanda_issue([BODY], {}, found, set(), page_count=1)
    assert report["unplaced"]["before_first_instrument"]["lines"] == 1
    assert report["unplaced"]["language_without_version"]["lines"] == 2
    nothing = accounting.rwanda_issue([BODY], {}, [], set(), page_count=1)
    assert nothing["unplaced"]["no_instrument_found"]["lines"] == 6 and nothing["pages_unplaced"] == [2]


def test_an_annex_counts_only_when_its_instrument_was_found():
    annex = page(3, [column("en", "ANNEX TO LAW N° 1", "Table of fees")])
    found = [{"number": "1", "kind": "law", "versions": versions()}]
    kept = accounting.rwanda_issue([BODY, annex], {3: 1}, found, {1}, page_count=2)
    assert kept["lines"]["annex"] == 2 and not kept["unplaced"]
    lost = accounting.rwanda_issue([BODY, annex], {3: 1}, found, set(), page_count=2)
    assert lost["unplaced"]["annex_without_instrument"]["lines"] == 2


def test_a_table_counts_its_cells_words():
    table = {
        "text": "",
        "table": {"header_rows": [[{"text": "Fee"}]], "body_rows": [[{"text": "Ten francs"}]]},
    }
    assert accounting.count(table) == 3


def node(text, kind="paragraph"):
    return {"kind": kind, "text": text}


def pdf_page(number, *nodes):
    return {"page": number, "nodes": list(nodes)}


def test_a_gazette_front_page_in_no_act_is_counted_and_a_page_losing_words_too():
    front = pdf_page(
        1, node("Extraordinary", "furniture"), node("Federal Republic of Nigeria Official Gazette")
    )
    act = pdf_page(2, node("EXAMPLE ACT, 2024"), node("ENACTED by the National Assembly"))
    reading = {"summary": {"pages": 2}, "pages": [front, act]}
    shorter = pdf_page(2, node("EXAMPLE ACT, 2024"))
    report = accounting.ng_pdf(reading, [{"pages": [shorter]}], ocr_check_error="KeyError: 'text'")
    assert report["unplaced"]["front_page"]["words"] == 6 and report["first_page_unplaced"]
    assert report["unplaced"]["dropped_within_page"]["words"] == 5
    assert report["lines"]["furniture"] == 1 and report["ocr_check_error"] == "KeyError: 'text'"
    alone = accounting.ng_pdf(reading, [])
    assert alone["unplaced"]["no_instrument_found"]["words"] == 14


JUDGMENT = (
    "IN THE NATIONAL INDUSTRIAL COURT OF NIGERIA "
    + "The claimant was employed by the defendant and claims his salary for the months unpaid. " * 6
)


def court_page(column_html):
    return (
        "<html><head><title>Portal</title></head><body><nav>Home About</nav>"
        f'<div class="light-background"><div class="col-12">{column_html}</div></div></body></html>'
    ).encode()


def test_a_court_page_accounts_for_its_words_and_finds_words_a_reader_drops():
    whole = accounting.nicn_page(
        court_page(f"<p>{JUDGMENT}</p>"), nicn.read(court_page(f"<p>{JUDGMENT}</p>"))
    )
    assert not whole["unplaced"] and whole["words"]["document"] == whole["words_in_column"]
    assert whole["portal_words_outside_column"] == 2  # the portal's menu, outside the judgment
    # Words inside Word's smart tags ("HOLDEN AT <st1:place>LAGOS</st1:place>").
    tagged = court_page(f"<p>{JUDGMENT}</p><p>HOLDEN AT <st1:place>LAGOS</st1:place> DIVISION</p>")
    report = accounting.nicn_page(tagged, nicn.read(tagged))
    missing = report["unplaced"]["inside_judgment_column"]
    assert missing["words"] == 1 and missing["samples"][0]["text"].endswith("holden at [lagos] division")


def test_many_files_sum_to_one_summary():
    found = [{"number": "x", "kind": "agreement", "versions": versions()}]
    one = {
        "name": "a.pdf",
        "source": "rlrc-laws",
        **accounting.rwanda_issue([COVER, BODY], {}, found, set(), 2),
    }
    two = {
        "name": "b.pdf",
        "source": "minijust-gazette",
        **accounting.rwanda_issue([BODY], {}, found, set(), 1),
    }
    summary = accounting.summarise([one, two])
    assert summary["files"] == 2 and summary["files_with_unplaced"] == 1
    assert summary["files_whose_first_page_is_unplaced"] == 1
    assert summary["reasons"]["no_language"]["files"] == 1
    assert summary["by_source"]["rlrc-laws"]["first_page_unplaced"] == 1
    assert summary["worst"][0]["file"] == "a.pdf"
