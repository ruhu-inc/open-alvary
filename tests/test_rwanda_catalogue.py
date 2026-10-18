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
            words = text.split()
            for word in words[line % 3 :] + words[: line % 3]:  # lines differ, as printed text does
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
    damaged = gazette_layout.words(
        page.replace("</doc>", "")
        + '<page width="0" height="0"><word xMin="1" yMin="1" xMax="9" yMax="9">x</word></page></doc>'
    )
    assert len(damaged) == 2 and damaged[1]["missing"] and not damaged[1]["words"]
    assert damaged[1]["width"] == damaged[0]["width"] and gazette_layout.columns(damaged[1]) == []
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


def test_rlrc_names_of_every_era_and_kind_give_their_kind():
    folder = "/user_upload/RLRC/Laws_of_Rwanda_v2/Domestic_laws/Repealed_laws/10._Business/10.4._Insurance/"
    cases = {
        "10.4.1.1._DECRET-LOI_NO_20-75-1975__RELATIF_AUX_ASSURANCES-J.O._NO_13-1.7.1975.pdf": "decree_law",
        "4.1.2._D.L._N0_22-81-1981MODIFIANT_L_ARTICLE_135.pdf": "decree_law",
        "10.16.2.12._O.L._N0_69_173___Regime_postal-BORU_N0_14_31.7.1960.pdf": "ordinance",
        "9.7.12_ORDONNANCE_LEGISLATIVE_N___22383_DU_11_DECEMBRE_1956.pdf": "ordinance",
        "10.4.2.8._Financement_du_fond_de_garantie__AM_no_11-05_de_1975.pdf": "ministerial_order",
        "7.12.1.20._RDF__special_statute_P.O_no_044.01_of_2020.pdf": "presidential_order",
        "10.17.3._Prime_Minister_s_Order_determining_the_Regulatory_authority__n___028-03_of_2024": "prime_ministers_order",
        "7.11.4.1.9._Governing_Public_Inst__AMEND_ORL_No_092012OL_of_2012.pdf": "organic_law",
    }
    for name, kind in cases.items():
        assert rwanda.parse_rlrc(name, folder)["kind"] == kind, name
    treaties = "/user_upload/RLRC/Laws_of_Rwanda_v2/International_Legal_Instruments__ILI_/Multilateral/x/"
    assert rwanda.parse_rlrc("10.12.2._Berne_Convention_for_protection.pdf", treaties)["kind"] == "convention"
    assert (
        rwanda.parse_rlrc("Accord_commercial_entre_la_Republique_Rwandaise.pdf", treaties)["kind"]
        == "agreement"
    )


def test_a_bulk_fetch_records_a_failed_file_goes_on_and_stops_at_a_refusal_or_a_full_disk(
    tmp_path, monkeypatch
):
    import httpx
    import pytest

    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    monkeypatch.setattr(rwanda, "RESERVE_BYTES", 0)  # this machine's own free space is not the test
    names = ("good", "notpdf", "refused", "later")
    files = [
        {
            "url": f"https://www.rlrc.gov.rw/fileadmin/{n}.pdf",
            "name": f"{n}.pdf",
            "size": "1 KB",
            "modified": "",
            "folder": "/",
        }
        for n in names
    ]
    ledger = Ledger(tmp_path / "sync-ledger.json")
    ledger.observe_listing(
        "rlrc-laws",
        BASE,
        "weekly",
        {"complete": True, "outcome": "ok", "pages_read": 1, "files": files},
        "run-1",
    )
    ledger.save()
    answers = {
        "good": (200, b"%PDF-1.4 a"),
        "notpdf": (200, b"<html>"),
        "refused": (429, b""),
        "later": (200, b"%PDF-1.4 b"),
    }

    def site(request):
        code, body = answers[request.url.path.rsplit("/", 1)[-1][:-4]]
        return httpx.Response(code, content=body)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(site), **kw))
    items = [f"rlrc-laws:{f['url']}" for f in files]
    selection = {"approved_by": "operator", "approved_at": "2026-10-12", "purpose": "bulk", "items": items}
    with pytest.raises(rwanda.FetchStopped, match="429"):
        rwanda.fetch_selection(selection, delay=0, keep_going=True, log=lambda message: None)
    held = Ledger(tmp_path / "sync-ledger.json").data["items"]
    assert held[items[0]].get("sha256")
    assert held[items[1]]["history"][-1]["change"] == "fetch failed"  # not a PDF: recorded, and on
    assert not held[items[3]].get("sha256")  # never asked for after the refusal
    answers["refused"] = (200, b"%PDF-1.4 c")
    monkeypatch.setattr(rwanda, "RESERVE_BYTES", 10**18)
    with pytest.raises(rwanda.FetchStopped, match="free"):
        rwanda.fetch_selection(selection, delay=0, keep_going=True, log=lambda message: None)
    assert set(rwanda.bulk_selection("operator", "bulk", ["rlrc-laws"])["items"]) == set(items[1:])


def test_rlrc_listing_suggests_legal_status_and_subjects_for_a_gazette_law(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    (tmp_path / "catalogue").mkdir()
    root = "/user_upload/RLRC/Laws_of_Rwanda_v2/Domestic_laws/"
    files = [
        {
            "name": "8.3.4._LAW_N0_28-2012__DIRECT_TAX_ON_INCOME.pdf",
            "folder": root + "Repealed_laws/8._Taxation/8.3._Income_tax/",
            "parsed": {
                "kind": "law",
                "number": "28-2012",
                "year": 2012,
                "status": "repealed",
                "subject_code": "8.3.4",
            },
        },
        {
            "name": "6.6.11._Criminal_Procedure__Law_No_027_of_2019.pdf",
            "folder": root + "Laws_in_force/6._Judicial/6.6._Criminal_procedure/",
            "parsed": {
                "kind": "law",
                "number": "027",
                "year": 2019,
                "status": "in_force",
                "subject_code": "6.6.11",
            },
        },
    ]
    (tmp_path / "catalogue" / "rlrc-laws.json").write_text(
        json.dumps({"checked_at": "2026-10-11T04:49:47Z", "files": files})
    )
    index, checked = rwanda.rlrc_index()
    repealed = rwanda.rlrc_status(index, checked, "law", "28/2012", "2012")
    assert repealed["status.legal"] == "repealed" and repealed["status.as_of"] == "2026-10-11"
    assert repealed["classification.subjects"] == ["Taxation › Income tax"]
    assert "among the repealed laws" in repealed["status.note"]
    assert rwanda.rlrc_status(index, checked, "law", "027/2019", "2019")["status.legal"] == "in_force"
    assert rwanda.rlrc_status(index, checked, "law", "055/2024", "2024") == {}  # not listed: left unknown


def test_a_network_outage_is_waited_out_not_skipped_and_a_long_one_stops_the_run(tmp_path, monkeypatch):
    import shutil

    import httpx
    import pytest

    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    monkeypatch.setattr(rwanda, "RESERVE_BYTES", 0)
    url = "https://www.minijust.gov.rw/fileadmin/a.pdf"
    ledger = Ledger(tmp_path / "sync-ledger.json")
    files = [{"url": url, "name": "a.pdf", "size": "1 KB", "modified": "", "folder": "/"}]
    ledger.observe_listing(
        "minijust-gazette",
        BASE,
        "daily",
        {"complete": True, "outcome": "ok", "pages_read": 1, "files": files},
        "run-1",
    )
    ledger.save()
    outage = {"left": 2}

    def site(request):
        if outage["left"]:
            outage["left"] -= 1
            raise httpx.ConnectError("nodename nor servname provided, or not known")
        return httpx.Response(200, content=b"%PDF-1.4 a")

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(site), **kw))
    selection = {
        "approved_by": "operator",
        "approved_at": "2026-10-12",
        "purpose": "bulk",
        "items": [f"minijust-gazette:{url}"],
    }
    waited = []
    results = rwanda.fetch_selection(
        selection, delay=0, keep_going=True, log=lambda m: None, sleep=waited.append
    )
    assert results[0]["fetched"] is True and waited == [30, 60]  # the same file, tried again after waits
    outage["left"] = 100
    shutil.rmtree(tmp_path / "sources")  # the file is gone again: it must be fetched anew
    ledger = Ledger(tmp_path / "sync-ledger.json")
    ledger.data["items"][f"minijust-gazette:{url}"].pop("sha256")
    ledger.save()
    with pytest.raises(rwanda.FetchStopped, match="network"):
        rwanda.fetch_selection(selection, delay=0, keep_going=True, log=lambda m: None, sleep=lambda s: None)


def test_the_survey_sums_files_by_source_and_year_and_prices_the_scanned_pages(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    (tmp_path / "survey").mkdir()
    rows = [
        {
            "source": "minijust-gazette",
            "year": "2017",
            "pages": 253,
            "scanned_pages": 182,
            "text_pages": 253,
            "tables": 1,
            "annex_pages": 170,
            "in_three_languages": 7,
            "instruments": {"presidential_order": 7},
        },
        {
            "source": "minijust-gazette",
            "year": "2024",
            "pages": 394,
            "scanned_pages": 0,
            "text_pages": 394,
            "tables": 0,
            "annex_pages": 0,
            "in_three_languages": 1,
            "instruments": {"law": 1},
        },
        {"source": "rlrc-laws", "year": None, "error": "PdfReadError: broken"},
    ]
    for k, row in enumerate(rows):
        (tmp_path / "survey" / f"{k}.json").write_text(json.dumps(row))
    summary = rwanda.survey_summary(price_per_1000_pages=10.0)
    every = summary["by"]["all"]
    assert (summary["files_surveyed"], summary["errors"]) == (3, 1)
    assert (every["files"], every["pages"], every["scanned_pages"]) == (2, 647, 182)
    assert every["document_ai_cost_usd"] == 1.82 and every["instruments"] == {
        "presidential_order": 7,
        "law": 1,
    }
    assert summary["by"]["minijust-gazette 2017"]["scanned_pages"] == 182


def test_two_fetches_sharing_the_ledger_keep_each_others_records(tmp_path):
    files = [
        {
            "url": f"https://www.rlrc.gov.rw/fileadmin/{n}.pdf",
            "name": f"{n}.pdf",
            "size": "1 KB",
            "modified": "",
            "folder": "/",
        }
        for n in ("a", "b")
    ]
    first = Ledger(tmp_path / "sync-ledger.json")
    first.observe_listing(
        "rlrc-laws",
        BASE,
        "weekly",
        {"complete": True, "outcome": "ok", "pages_read": 1, "files": files},
        "run-1",
    )
    first.save()
    a, b = (f"rlrc-laws:{f['url']}" for f in files)
    one, two = Ledger(tmp_path / "sync-ledger.json"), Ledger(tmp_path / "sync-ledger.json")
    one.fetched(a, "1" * 64, 10)
    two.fetched(b, "2" * 64, 20)
    one.save_items([a])
    two.save_items([b])  # written after the other: it must not undo the first's record
    on_disk = Ledger(tmp_path / "sync-ledger.json").data["items"]
    assert on_disk[a]["sha256"] == "1" * 64 and on_disk[b]["sha256"] == "2" * 64
    assert two.data["items"][a]["sha256"] == "1" * 64  # and each sees what the other wrote


def test_a_word_document_is_taken_as_it_is_and_a_damaged_file_is_not():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    assert rwanda.file_kind(buffer.getvalue()) == (".docx", rwanda.DOCX)
    assert rwanda.file_kind(b"%PDF-1.7 ...") == (".pdf", "application/pdf")
    assert rwanda.file_kind(b"\x00" * 10 + b"%PDF-1.4") == (".pdf", "application/pdf")  # a little junk before
    assert rwanda.file_kind(b"\x00" * 4096) is None  # damaged at the source
    assert rwanda.file_kind(b'o"\n      ]\n ') is None


def test_a_word_document_is_passed_over_by_prepare_and_the_survey_not_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    ledger = Ledger(tmp_path / "sync-ledger.json")
    item = "rlrc-laws:https://www.rlrc.gov.rw/x/DECRET-LOI.docx"
    ledger.data["items"][item] = {
        "source": "rlrc-laws",
        "url": "https://www.rlrc.gov.rw/x/DECRET-LOI.docx",
        "listed": {"name": "DECRET-LOI.docx"},
        "sha256": "ab" * 32,
        "stored": "gs://bucket/sources/rw/rlrc-laws/" + "ab" * 32 + ".docx",
        "format": "docx",
    }
    ledger.save()

    class NoStore:
        def get(self, name):
            raise AssertionError(f"a Word document is not read as a PDF: {name}")

    said = []
    collection = rwanda.LocalCollection(tmp_path / "collection")
    rwanda.prepare_selection({"items": [item]}, log=said.append, collection=collection, store=NoStore())
    assert said == ["not prepared (a Word document; not yet read by this pipeline): DECRET-LOI.docx"]
    assert rwanda.survey(store=NoStore(), log=said.append)["files_surveyed"] == 0


def test_scanned_pages_are_read_once_per_file_from_the_bucket_within_the_approved_budget(
    tmp_path, monkeypatch
):
    import io
    import json
    from types import SimpleNamespace

    import google.api_core.exceptions
    from pypdf import PdfReader, PdfWriter

    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    monkeypatch.setattr(rwanda.ruled_tables, "image_pages", lambda pdf: {2})  # page 2 is a scan
    writer = PdfWriter()
    writer.add_blank_page(600, 840)
    writer.add_blank_page(600, 840)
    two_pages = io.BytesIO()
    writer.write(two_pages)
    ledger = Ledger(tmp_path / "sync-ledger.json")
    (tmp_path / "survey").mkdir()
    files = {"a": ("a1", 1), "b": ("b1", 1), "c": ("c1", 5)}  # sha: (item, scanned pages found)
    for sha, (item, pages) in files.items():
        ledger.data["items"][item] = {
            "source": "rlrc-laws",
            "listed": {"name": item},
            "sha256": sha,
            "stored": "gs://x",
        }
        (tmp_path / "survey" / f"{sha}.json").write_text(json.dumps({"item": item, "scanned_pages": pages}))
    ledger.data["items"]["a2"] = {
        **ledger.data["items"]["a1"],
        "listed": {"name": "a2"},
    }  # the same file twice
    ledger.data["items"]["d1"] = {
        "source": "rlrc-laws",
        "listed": {"name": "d1"},
        "sha256": "d",
        "stored": "gs://x",
    }
    (tmp_path / "survey" / "d.json").write_text(json.dumps({"item": "d1", "scanned_pages": 1}))
    ledger.save()

    class Store:
        put_names = []

        def list(self, prefix):
            return [f"{prefix}d.json"]  # d was read before

        def get(self, name):
            assert name.startswith("sources/rw/rlrc-laws/") and name.endswith(".pdf")
            return two_pages.getvalue()

        def put(self, name, data, content_type, replace=False):
            self.put_names.append(name)

    class Reader:
        def read(self, part):
            return SimpleNamespace(pages=[{"text": "read"} for _ in PdfReader(str(part)).pages])

        def version(self):
            return {"reader": "fake"}

    said = []
    result = rwanda.read_scans(log=said.append, store=Store(), max_pages=3, files_at_once=2, reader=Reader())
    assert result["pages_read"] == 2 and result["left"] == 1 and "budget of 3" in result["stopped"]
    assert sorted(Store.put_names) == ["readings/rw/docai/a.json", "readings/rw/docai/b.json"]
    assert json.loads((tmp_path / "docai" / "a.json").read_text())["pages"] == {"2": {"text": "read"}}

    class Refused(Reader):
        def read(self, part):
            raise google.api_core.exceptions.PermissionDenied("billing is disabled")

    result = rwanda.read_scans(log=said.append, store=Store(), max_pages=100, reader=Refused())
    assert result["pages_read"] == 0 and result["stopped"].startswith("stopped: Document AI refused")


def test_the_cloud_job_reads_its_share_of_the_work_list_without_a_ledger(tmp_path, monkeypatch):
    import io
    import json
    from types import SimpleNamespace

    from pypdf import PdfReader, PdfWriter

    from ingestion import scan_job

    monkeypatch.setattr(rwanda, "STAGING", tmp_path)
    ledger = Ledger(tmp_path / "sync-ledger.json")
    (tmp_path / "survey").mkdir()
    for item, sha, pages in [("x1", "x", 3), ("y1", "y", 9), ("z1", "z", 5), ("x2", "x", 3)]:
        ledger.data["items"][item] = {
            "source": "rlrc-laws",
            "listed": {"name": item},
            "sha256": sha,
            "stored": "gs://b",
        }
        (tmp_path / "survey" / f"{item}.json").write_text(json.dumps({"item": item, "scanned_pages": pages}))
    ledger.save()
    worklist = rwanda.scan_worklist({"approved_by": "operator", "max_pages": 20, "basis": "test"})
    assert [f["sha256"] for f in worklist["files"]] == ["y", "z", "x"]  # largest first, each file once
    assert worklist["pages"] == 17
    assert [f["sha256"] for f in scan_job.share(worklist["files"], 0, 2)] == ["y", "x"]
    assert [f["sha256"] for f in scan_job.share(worklist["files"], 1, 2)] == ["z"]

    # In the cloud: no ledger, no survey; the work list says what each file is.
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    monkeypatch.setattr(rwanda, "STAGING", cloud)
    monkeypatch.setattr(rwanda.ruled_tables, "image_pages", lambda pdf: {1})
    writer = PdfWriter()
    writer.add_blank_page(600, 840)
    one_page = io.BytesIO()
    writer.write(one_page)
    stored = []

    class Store:
        def list(self, prefix):
            return []

        def get(self, name):
            return one_page.getvalue()

        def put(self, name, data, content_type, replace=False):
            stored.append(name)

    class Reader:
        def read(self, part):
            return SimpleNamespace(pages=[{"words": []} for _ in PdfReader(str(part)).pages], raw={})

        def version(self):
            return {"reader": "fake"}

    result = rwanda.read_scans(
        store=Store(),
        max_pages=20,
        kind="ocr",
        reader=Reader(),
        worklist=scan_job.share(worklist["files"], 1, 2),
    )
    assert result["pages_read"] == 1 and stored == ["readings/rw/ocr/z.json"]


def test_scan_progress_counts_the_work_list_files_each_reader_has_read():
    import json

    worklist = {
        "pages": 12,
        "files": [
            {"sha256": "a", "scanned_pages": 9},
            {"sha256": "b", "scanned_pages": 3},
        ],
    }

    class Store:
        def get(self, name):
            assert name == rwanda.WORKLIST
            return json.dumps(worklist).encode()

        def list(self, prefix):
            return {
                "readings/rw/ocr/": [f"{prefix}a.json", f"{prefix}b.json"],
                "readings/rw/docai/": [f"{prefix}b.json"],
            }[prefix]

    assert rwanda.scan_progress(Store()) == {
        "files": 2,
        "pages": 12,
        "ocr": {"files": 2, "pages": 12},
        "layout": {"files": 1, "pages": 3},
    }


def test_in_a_cloud_job_the_text_layer_is_read_by_pdftotext_in_its_own_container(tmp_path, monkeypatch):
    import subprocess

    from ingestion import gazette_layout

    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(
            command, 0, '<doc><page width="600" height="840"></page></doc>', ""
        )

    monkeypatch.setattr(gazette_layout.subprocess, "run", run)
    monkeypatch.setenv("ALVARY_TEXT_LAYER", "direct")
    pages = gazette_layout.text_layer(tmp_path / "issue.pdf")
    assert seen[0][:2] == ["pdftotext", "-bbox-layout"] and "docker" not in seen[0]
    assert pages[0]["width"] == 600.0


def test_an_issue_prepared_by_an_older_pipeline_is_prepared_again_and_the_manifest_reads_both():
    import json

    from ingestion import rw_gazette

    class Store:
        def __init__(self):
            self.objects = {
                "collections/rw/issues/old.json": json.dumps([{"id": "rw-law-1-2001-en"}]).encode(),
            }

        def exists(self, name):
            return name in self.objects

        def get(self, name):
            return self.objects[name]

        def put(self, name, data, content_type, replace=False):
            self.objects[name] = data

        def list(self, prefix):
            return sorted(n for n in self.objects if n.startswith(prefix))

    store = Store()
    collection = rwanda.BucketCollection(store)
    assert not collection.done("old")  # a bare list: an older pipeline's index
    collection.finish_issue("new", [{"id": "rw-law-2-2002-en"}], {"ocr": False, "docai": False})
    assert collection.done("new")
    store.objects["readings/rw/ocr/new.json"] = b"{}"  # its scans read since: prepared again
    assert not collection.done("new")
    collection.finish_issue("new", [{"id": "rw-law-2-2002-en"}], {"ocr": True, "docai": False})
    assert collection.done("new")
    assert json.loads(store.objects["collections/rw/issues/new.json"])["pipeline"] == rw_gazette.PIPELINE
    collection.save_manifest(rwanda.new_manifest())
    manifest = json.loads(store.objects["collections/rw/manifest.json"])
    assert [d["id"] for d in manifest["documents"]] == ["rw-law-1-2001-en", "rw-law-2-2002-en"]


def test_the_prepare_job_takes_its_share_and_the_state_from_the_bucket(tmp_path):
    from ingestion import prepare_job

    assert prepare_job.share(["a", "b", "c", "d", "e"], 1, 2) == ["b", "d"]

    class Store:
        objects = {
            "state/rw/sync-ledger.json": b'{"items": {}}',
            "state/rw/catalogue/rlrc-laws.json": b'{"files": []}',
            "state/rw/catalogue/notes.txt": b"not json",
        }

        def get(self, name):
            return self.objects[name]

        def list(self, prefix):
            return sorted(n for n in self.objects if n.startswith(prefix))

    prepare_job.fetch_state(Store(), tmp_path)
    assert (tmp_path / "sync-ledger.json").exists() and (tmp_path / "catalogue" / "rlrc-laws.json").exists()
    assert not (tmp_path / "catalogue" / "notes.txt").exists()


def test_a_manifest_entry_is_light_and_its_details_go_beside_it():
    document = {
        "id": "rw-law-1-2001-en",
        "title": "Law n° 1/2001",
        "summary": {"pages": 6, "flags": {"high": 1, "medium": 2, "low": 0}},
        "suggested": {"identity.number": "1/2001"},
        "issue": {"name": "OG n° 1"},
        "alignment": {"aligned": True},
        "also_printed_as": None,
    }
    entry, details = rwanda.split_entry(document)
    assert entry == {
        "id": "rw-law-1-2001-en",
        "title": "Law n° 1/2001",
        "page_count": 6,
        "flag_counts": {"high": 1, "medium": 2, "low": 0},
    }
    assert set(details) == {"suggested", "issue", "alignment"}


def test_a_prepare_task_notes_a_file_it_cannot_prepare_and_goes_on(monkeypatch, capsys):
    import json

    from ingestion import prepare_job

    ledger = {"items": {name: {"stored": "gs://b"} for name in ("a", "b", "c")}}

    class Store:
        def get(self, name):
            return json.dumps(ledger).encode()

        def list(self, prefix):
            return []

    prepared, saved = [], []
    monkeypatch.setattr(rwanda, "STAGING", rwanda.STAGING)  # the job points it at its own folder
    monkeypatch.setattr(prepare_job.corpus_store, "CorpusStore", Store)
    monkeypatch.setattr(rwanda.BucketCollection, "save_manifest", lambda self, manifest: saved.append(1))

    def prepare(selection, **kwargs):
        [item] = selection["items"]
        assert kwargs["save"] is False
        if item == "b":
            raise ZeroDivisionError("float division by zero")
        prepared.append(item)
        return {"documents": 0, "instruments": [{"work": item}]}

    monkeypatch.setattr(rwanda, "prepare_selection", prepare)
    assert prepare_job.main() == 0
    assert prepared == ["a", "c"] and saved == [1]
    out = capsys.readouterr().out
    assert "failed b: ZeroDivisionError" in out and '"failed": 1' in out


def test_an_order_numbered_again_each_year_takes_its_year_and_one_document_is_kept_per_id():
    assert rwanda.work_id("presidential_order", "002/01", "2022") == "rw-presidential-order-002-01-2022"
    assert rwanda.work_id("law", "055/2024", "2024") == "rw-law-055-2024"  # the year is in the number
    assert rwanda.work_id("law", "1964-02-19", "1964") == "rw-law-1964-02-19"
    rlrc = {
        "id": "rw-law-055-2024-en",
        "url": "https://www.rlrc.gov.rw/x.pdf",
        "source_object": "sources/rw/rlrc-laws/b.pdf",
        "objects": "files/b/",
        "scanned_pages": [],
        "flag_counts": {"high": 0},
    }
    gazette = {
        **rlrc,
        "url": "https://www.minijust.gov.rw/y.pdf",
        "source_object": "sources/rw/minijust-gazette/g.pdf",
        "objects": "files/g/",
        "flag_counts": {"high": 3},
    }
    other = {**rlrc, "id": "rw-law-056-2024-en"}
    [first, second] = rwanda.one_per_id([rlrc, other, gazette])
    assert first["objects"] == "files/g/"  # the Official Gazette's printing is the one reviewed
    assert first["other_sources"] == [
        {"url": rlrc["url"], "source_object": rlrc["source_object"], "objects": "files/b/"}
    ]
    assert second["id"] == "rw-law-056-2024-en" and "other_sources" not in second


def test_each_source_files_documents_are_kept_in_its_own_folder():
    import json

    class Store:
        def __init__(self):
            self.objects = {}

        def put(self, name, data, content_type, replace=False):
            self.objects[name] = data

    store = Store()
    collection = rwanda.BucketCollection(store)
    collection.write("rw-law-1-2001-en", {"pages": []}, {"sha256": "abc"}, None)
    collection.finish_issue("abc", [{"id": "rw-law-1-2001-en", "suggested": {"x": 1}}])
    assert {
        "collections/rw/files/abc/rw-law-1-2001-en.reading.json",
        "collections/rw/files/abc/rw-law-1-2001-en.provenance.json",
        "collections/rw/files/abc/rw-law-1-2001-en.document.json",
    } <= set(store.objects)
    index = json.loads(store.objects["collections/rw/issues/abc.json"])
    assert index["documents"][0]["objects"] == "files/abc/"


def test_a_law_links_to_the_laws_it_repeals_and_amends_and_they_link_back():
    def doc(work, language="en", **more):
        return {
            "id": f"{work}-{language}",
            "work": work,
            "language": language,
            "title": f"Title of {work}",
            **more,
        }

    cites = [
        {
            "kind": "repeals",
            "target": "Law nº 028/2023 of 19/05/2023 on examples",
            "instrument": "law",
            "number": "028/2023",
            "year": "2023",
        },
        {
            "kind": "amends",
            "target": "Law n° 66/2018 of 30/08/2018 regulating labour",
            "instrument": "law",
            "number": "066/2018",
            "year": "2018",
            "note": "Article 44",
        },
        {
            "kind": "repeals",
            "target": "Law n° 08/99 of 18/06/1999 on banks",
            "instrument": "law",
            "number": "08/99",
            "year": "1999",
        },
        {
            "kind": "repeals",
            "target": "Presidential Order n° 046/01 of 29/05/2019",
            "instrument": "presidential_order",
            "number": "046/01",
            "year": "2019",
        },
    ]
    documents = [
        doc("rw-law-001-2025", cites=cites),
        doc("rw-law-001-2025", "fr", cites=cites),
        doc("rw-law-28-2023"),
        doc("rw-law-66-2018", "rw"),
        doc("rw-presidential-order-46-01-2019"),
        doc("rw-law-28-2023", part="annex-1", id="rw-law-28-2023-annex-1-en"),
    ]
    linked = {d["id"]: d.get("relations") for d in rwanda.link_works(documents)}
    assert (
        linked["rw-law-001-2025-en"]
        == linked["rw-law-001-2025-fr"]
        == [
            {
                "kind": "repeals",
                "target": "Law nº 028/2023 of 19/05/2023 on examples",
                "target_id": "rw-law-28-2023",
            },
            {
                "kind": "amends",
                "target": "Law n° 66/2018 of 30/08/2018 regulating labour",
                "target_id": "rw-law-66-2018",
                "note": "Article 44",
            },
            {"kind": "repeals", "target": "Law n° 08/99 of 18/06/1999 on banks"},  # not in the collection
            {
                "kind": "repeals",
                "target": "Presidential Order n° 046/01 of 29/05/2019",
                "target_id": "rw-presidential-order-46-01-2019",
            },
        ]
    )
    assert linked["rw-law-28-2023-en"] == [
        {"kind": "repealed_by", "target": "Title of rw-law-001-2025", "target_id": "rw-law-001-2025"}
    ]
    assert linked["rw-law-66-2018-rw"] == [
        {
            "kind": "amended_by",
            "target": "Title of rw-law-001-2025",
            "target_id": "rw-law-001-2025",
            "note": "Article 44",
        }
    ]
    assert linked["rw-law-28-2023-annex-1-en"] is None  # an annex is not the law
    assert (
        rwanda.work_key("rw-law-53bis-2013") == rwanda.work_key("rw-law-053-bis-2013") == "rw-law-53-bis-2013"
    )
