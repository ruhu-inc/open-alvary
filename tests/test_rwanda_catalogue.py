"""The Rwanda catalogue: listing pages read politely, files recorded, changes tracked in the ledger."""

from datetime import UTC, datetime

from ingestion import filelist, rwanda
from ingestion.sync import Ledger

BASE = "https://www.example.gov.rw/laws"
ROOT = "/user_upload/Laws/"


def link(path, page=None):
    query = f"tx_filelist_filelist%5Bpath%5D={path.replace('/', '%2F')}"
    if page:
        query += f"&amp;tx_filelist_filelist%5BcurrentPage%5D={page}"
    return f"/laws?{query}&amp;cHash=abc"


def folder_row(path, name):
    return f'<tr><td><a href="{link(path)}"><img/></a></td><td><a href="{link(path)}">{name}</a></td></tr>'


def file_row(name, size="2 MB", modified="Aug 23, 2023", folder=ROOT):
    href = f"/fileadmin{folder}{name}"
    return (
        f'<tr><td><a href="{href}"><img/></a></td><td><a href="{href}">{name}</a></td>'
        f"<td>{size}</td><td>{modified}</td></tr>"
    )


def page(*rows, up=None, pages=()):
    parent = (
        f'<tr><td><a href="{link(up)}">..</a></td><td colspan="3"><a href="{link(up)}">..</a></td></tr>'
        if up
        else ""
    )
    pager = "".join(f'<a href="{href}">{n}</a>' for n, href in pages)
    return f'<table class="table"><tbody>{parent}{"".join(rows)}</tbody></table><nav>{pager}</nav>'


def site(overrides=None):
    sub = ROOT + "4._Criminal/"
    pages = {
        "": page(folder_row(ROOT, "Laws")),
        ROOT: page(folder_row(sub, "4. Criminal"), up="/user_upload/", pages=[(2, link(ROOT, 2))]),
        (ROOT, 2): page(file_row("8.3.8_Law_no_014_2025_of_27_05_2025.pdf"), up="/user_upload/"),
        sub: page(file_row("4.1.1._Offences_Law_n___68_of__2018.pdf", folder=sub), up=ROOT),
    }
    pages.update(overrides or {})
    seen = []

    def fetch(url):
        query = filelist._query(url)
        path = query.get(filelist.PATH_KEY, "")
        number = int(query.get(filelist.PAGE_KEY, "1") or 1)
        seen.append((path, number))
        return pages[(path, number)] if number > 1 else pages[path]

    return fetch, seen


def test_the_crawl_follows_folders_and_pages_under_its_root_and_never_goes_up():
    fetch, seen = site()
    waits = []
    result = filelist.crawl(BASE, ROOT, fetch, delay=4, sleep=waits.append)
    assert result["complete"] and result["pages_read"] == 4
    assert [f["name"] for f in result["files"]] == [
        "8.3.8_Law_no_014_2025_of_27_05_2025.pdf",
        "4.1.1._Offences_Law_n___68_of__2018.pdf",
    ]
    assert result["files"][1]["folder"] == ROOT + "4._Criminal/"
    assert ("/user_upload/", 1) not in seen  # the ".." link is never followed
    assert waits == [4, 4, 4]  # a pause before every request after the first


def test_a_refusal_stops_the_crawl_and_marks_it_incomplete():
    fetch, _ = site()

    def refusing(url):
        if filelist.PAGE_KEY in filelist._query(url):
            raise filelist.Refused("429 at page 2")
        return fetch(url)

    result = filelist.crawl(BASE, ROOT, refusing, sleep=lambda s: None)
    assert result["complete"] is False and result["outcome"].startswith("refused")


def test_the_ledger_records_new_changed_moved_and_no_longer_listed_files(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    first = {
        "complete": True,
        "outcome": "ok",
        "pages_read": 3,
        "files": [
            {
                "url": "u1",
                "name": "a.pdf",
                "size": "2 MB",
                "modified": "Aug 23, 2023",
                "folder": "/in_force/",
            },
            {
                "url": "u2",
                "name": "b.pdf",
                "size": "1 MB",
                "modified": "Aug 23, 2023",
                "folder": "/in_force/",
            },
        ],
    }
    at = datetime(2026, 10, 10, 7, tzinfo=UTC)
    changes = ledger.observe_listing("rlrc-laws", BASE, "weekly", first, "run-1", at=at)
    assert len(changes["added"]) == 2 and ledger.source("rlrc-laws")["next_due"] == "2026-10-17T07:00:00Z"
    second = {
        "complete": True,
        "outcome": "ok",
        "pages_read": 3,
        "files": [
            {
                "url": "u1",
                "name": "a.pdf",
                "size": "3 MB",
                "modified": "Oct 01, 2026",
                "folder": "/in_force/",
            },
            {
                "url": "u3",
                "name": "c.pdf",
                "size": "1 MB",
                "modified": "Oct 01, 2026",
                "folder": "/repealed/",
            },
        ],
    }
    changes = ledger.observe_listing("rlrc-laws", BASE, "weekly", second, "run-2", at=at.replace(day=17))
    assert changes == {
        "added": ["rlrc-laws:u3"],
        "changed": ["rlrc-laws:u1"],
        "moved": [],
        "gone": ["rlrc-laws:u2"],
    }
    assert ledger.data["items"]["rlrc-laws:u2"]["history"][-1]["change"] == "no longer listed"
    assert ledger.source("rlrc-laws")["last_changed_at"] == "2026-10-17T07:00:00Z"
    incomplete = {"complete": False, "outcome": "refused: 429", "pages_read": 1, "files": []}
    changes = ledger.observe_listing("rlrc-laws", BASE, "weekly", incomplete, "run-3", at=at.replace(day=24))
    assert changes["gone"] == []  # an incomplete check never says files have gone
    assert ledger.source("rlrc-laws")["last_check_outcome"] == "refused: 429"
    ledger.save()
    assert Ledger(tmp_path / "ledger.json").source("rlrc-laws")["counts"]["listed"] == 0


def test_file_names_give_the_subject_status_kind_number_and_year():
    folder = "/user_upload/RLRC/Laws_of_Rwanda_v2/Domestic_laws/Laws_in_force/8._Taxation/8.3/"
    parsed = rwanda.parse_rlrc(
        "8.3.8_Law_no_014_2025_of_27_05_2025_amending_law_no_027_2022_of_20_10_2022_Establishing_Taxes_on_Income.pdf",
        folder,
    )
    assert parsed == {
        "subject_code": "8.3.8",
        "status": "in_force",
        "domain": "8 Taxation",
        "kind": "law",
        "number": "014",
        "year": 2025,
        "date": "2025-05-27",
        "parsed": True,
    }
    organic = rwanda.parse_rlrc("1.1.9.3._Senate_functionning__OL_No_001-2026.OL_of_2026.pdf", folder)
    assert organic["kind"] == "organic_law" and organic["number"] == "001-2026.OL"
    old = rwanda.parse_rlrc("4.1.4.__LAW_N0_01-2012-OL__ORGANIC_LAW__INSTITUTING_THE_PENAL_CODE.pdf", folder)
    assert old["kind"] == "organic_law" and old["number"] == "01-2012-OL"
    assert (
        rwanda.parse_rlrc("1.1.1.1._Constitution_of_the_Rep._of_Rwanda_2023.pdf", folder)["kind"]
        == "constitution"
    )


def test_gazette_file_names_give_the_issue_number_date_and_kind():
    assert rwanda.parse_gazette("52_bis_of_25.12.2017.pdf") == {
        "date": "2017-12-25",
        "bis": True,
        "number": 52,
        "parsed": True,
    }
    assert rwanda.parse_gazette("Special_of_14.12.2017.pdf")["special"] is True
    later = rwanda.parse_gazette("Igazeti ya Leta n° 52 yo ku wa 29.12.2025_ Amazina + Cooperative.pdf")
    assert later["number"] == 52 and later["date"] == "2025-12-29"
    # The names the archive has used over the years, typing slips included.
    cases = {
        "Official_Gazette_no_42_of_19.10.09.pdf": (42, "2009-10-19"),
        "Official_Gazette_no_13_ter_of_01.07.2006.pdf": (13, "2006-07-01"),
        "24bis_of_11.06.2018.pdf": (24, "2018-06-11"),
        "Official Gazette nº 29 of 21/09/2020": (29, "2020-09-21"),
        "OG_N0_31_of_30-07-2018_co__1_.pdf": (31, "2018-07-30"),
        "29_OG_No_29_bis_of_29-_07-_2019_MINES.pdf": (29, "2019-07-29"),
        "Official_Gazette_N___42_of_28_12_2020_Amazina.pdf": (42, "2020-12-28"),
        "Official_Gazette_no_02_11.01.2021_MINECOFIN___RDB.pdf": (2, "2021-01-11"),
    }
    for name, (number, date) in cases.items():
        parsed = rwanda.parse_gazette(name)
        assert (parsed.get("number"), parsed.get("date"), parsed["parsed"]) == (number, date, True), name
    assert rwanda.parse_gazette("Official_Gazette_no_13_ter_of_01.07.2006.pdf")["ter"] is True
    special = rwanda.parse_gazette("Igazeti ya Leta n° Idasanzwe yo ku wa 18/10/2024_Imbabazi")
    assert special == {"date": "2024-10-18", "special": True, "parsed": True}
    assert rwanda.parse_gazette("Official_Gazette_No_Spcial_of_04.11.2020.pdf")["special"] is True
    # A slip in the date leaves the issue for the reviewer, rather than guessing.
    assert rwanda.parse_gazette("Official_Gazette_N___Special_of_18.022021_Uburezi.pdf")["parsed"] is False


def test_a_listed_file_may_be_a_little_larger_than_shown_but_not_much_more():
    assert rwanda.size_limit({"size": "71 MB"}) == int(71 * 1024**2 * 1.25)
    assert rwanda.size_limit({"size": "436 KB"}) == rwanda.MAX_BYTES
    assert rwanda.size_limit({"size": "900 MB"}) == rwanda.LARGEST_GAZETTE


def test_the_crawler_says_who_it_is_and_adds_a_contact_only_when_set(monkeypatch):
    monkeypatch.delenv("ALVARY_CRAWLER_CONTACT", raising=False)
    assert filelist.user_agent() == "OpenAlvaryBot/0.1 (catalogue of official legal texts)"
    monkeypatch.setenv("ALVARY_CRAWLER_CONTACT", "https://example.org/contact")
    assert filelist.user_agent().endswith("; https://example.org/contact)")
    assert filelist.size_bytes("555 KB") == 568320 and filelist.listed_date("Sep 29, 2026") == "2026-09-29"
    assert filelist.size_bytes("1,004 KB") == 1004 * 1024 and filelist.size_bytes("1,5 MB") == 1572864


def bbox(*columns, width=600.0, lines=12):
    """A page as pdftotext -bbox-layout prints it: one block of words per column, line by line."""
    out = [f'<page width="{width}" height="840">']
    for (x0, x1), text in columns:
        for line in range(lines):
            y = 60 + line * 14
            x = x0
            for word in text.split():
                w = 6 * len(word)
                if x + w > x1:
                    break
                out.append(f'<word xMin="{x}" yMin="{y}" xMax="{x + w}" yMax="{y + 10}">{word}</word>')
                x += w + 4
    out.append("</page>")
    return "<doc>" + "".join(out) + "</doc>"


KIN = "Ingingo ya mbere Itegeko rigena ishyirwaho ry ikigo na mu Rwanda cyangwa kandi"
ENG = "Article One This Law determines the establishment of the institution and shall be"
FRA = "Article premier La présente loi porte création de la institution et des règles du"


def test_a_three_language_page_is_split_into_its_columns_and_languages():
    from ingestion import gazette_layout

    page = bbox(((40, 210), KIN), ((225, 395), ENG), ((410, 580), FRA))
    analysed = gazette_layout.analyse(gazette_layout.words(page))
    assert analysed[0]["layout"] == "three_columns"
    assert analysed[0]["languages"] == ["rw", "en", "fr"]
    single = gazette_layout.analyse(gazette_layout.words(bbox(((40, 580), ENG))))
    assert single[0]["layout"] == "single" and single[0]["languages"] == ["en"]
    empty = gazette_layout.analyse(gazette_layout.words('<doc><page width="600" height="840"></page></doc>'))
    assert empty[0]["layout"] == "no_text" and not empty[0]["text_layer"]
    summary = gazette_layout.summary(analysed + single)
    assert summary["layouts"] == {"three_columns": 1, "single": 1}
    assert summary["three_column_language_orders"] == {"rw | en | fr": 1}


def test_fetching_needs_an_approved_selection_of_catalogued_files_on_allowed_hosts(tmp_path, monkeypatch):
    import json

    import pytest

    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    unapproved = tmp_path / "selection.json"
    unapproved.write_text(json.dumps({"items": ["x"]}))
    with pytest.raises(ValueError, match="approved_by"):
        rwanda.load_selection(unapproved)
    ledger = Ledger(tmp_path / "sync-ledger.json")
    crawl = {
        "complete": True,
        "outcome": "ok",
        "pages_read": 1,
        "files": [
            {
                "url": "https://elsewhere.example/a.pdf",
                "name": "a.pdf",
                "size": "1 MB",
                "modified": "",
                "folder": "/",
            },
        ],
    }
    ledger.observe_listing("rlrc-laws", BASE, "weekly", crawl, "run-1")
    ledger.save()
    selection = {
        "approved_by": "operator",
        "approved_at": "2026-10-11",
        "purpose": "sample",
        "items": ["rlrc-laws:https://elsewhere.example/a.pdf"],
    }
    with pytest.raises(ValueError, match="not on an allowed host"):
        rwanda.fetch_selection(selection)
    with pytest.raises(ValueError, match="not in the catalogue"):
        rwanda.fetch_selection({**selection, "items": ["rlrc-laws:https://www.rlrc.gov.rw/unknown.pdf"]})


def test_every_kind_read_from_an_rlrc_name_is_a_kind_of_the_common_record():
    from schema.record import FAMILIES

    kinds = {kind for _, listed in FAMILIES.values() for kind in listed}
    assert set(rwanda.KINDS.values()) <= kinds, set(rwanda.KINDS.values()) - kinds
