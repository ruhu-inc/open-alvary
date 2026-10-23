"""The Nigerian catalogue: each source's listing read into ledger items with their provenance, one
item per document, kept answers read again, incremental runs, likely duplicates flagged, and the
fetch work list."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from ingestion import nigeria, polite
from ingestion.sync import Ledger

FIXTURES = Path(__file__).parent / "fixtures" / "ng"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Site:
    """Fixture answers by address in place of a polite client; an address may also be answered by
    a function, or raise (a host that does not answer)."""

    def __init__(self, pages: dict, disallowed=(), default=None):
        self.pages, self.disallowed, self.default = pages, disallowed, default
        self.asked: list[str] = []
        self.requests: dict[str, int] = {}
        self.refused_paths: list[str] = []

    def get(self, url, limit=None, head=None, hosts=None, headers=None):
        self.asked.append(url)
        answer = self.pages.get(url, self.default)
        if callable(answer):
            answer = answer(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return polite.Answer(url, url, 404, {}, b"", [])
        status, headers_, body = answer if isinstance(answer, tuple) else (200, {}, answer)
        if isinstance(body, str):
            body = body.encode()
        host = url.split("/")[2]
        self.requests[host] = self.requests.get(host, 0) + 1
        return polite.Answer(url, url, status, headers_, body, [])

    def permits(self, url):
        return not any(url.startswith(prefix) for prefix in self.disallowed)

    def robots_state(self):
        return {}

    def close(self):
        pass


def read(source_id: str, site: Site, state=None, budget=None) -> nigeria.Found:
    found = nigeria.Found(source_id)
    nigeria.READERS[source_id](site, found, state or {}, log=lambda m: None, budget=budget)
    return found


# --- readers ---------------------------------------------------------------------------------------


def test_the_national_assembly_acts_feed_gives_acts_with_their_row_verbatim():
    url = "https://nass.gov.ng/documents/magazine_track/?draw=1&start=0&length=10000"
    found = read("nass-acts", Site({url: fixture("nass-magazine-track.json")}))
    act = found.items["https://nass.gov.ng/documents/download/11251"]
    assert act["type"] == "act" and act["listing_url"] == url
    assert act["listed"] == {
        "title": "NIGERIA REVENUE SERVICE (ESTABLISHMENT) ACT, 2025",
        "year": 2025,
        "chamber": "NASS",
        "parliament": "10th Parliament",
        "session": "1st Session",
        "uploaded": "2026-05-13",
        "uploaded_as_listed": "2026-05-13",
        "category": "Magazine",
        "document_id": "11251",
    }
    assert act["listed_raw"][0] == "NIGERIA REVENUE SERVICE (ESTABLISHMENT) ACT, 2025"
    constitution = found.items["https://nass.gov.ng/documents/download/5820"]
    assert (
        constitution["type"] == "constitution"
        and constitution["listed"]["title"] == "1999 CONSTITUTION OF NIGERIA"
    )
    assert (
        "uploaded" not in constitution["listed"]
        and constitution["listed"]["uploaded_as_listed"] == "0000-00-00"
    )
    assert [r["title"] for r in found.rejected] == ["Motion"]  # not a law: logged, not kept


def test_the_bills_tracker_gives_bill_numbers_and_record_pages():
    url = "https://nass.gov.ng/documents/bill_track/?draw=1&start=0&length=10000"
    found = read("nass-bills", Site({url: fixture("nass-bill-track.json")}))
    bill = found.items["https://nass.gov.ng/documents/bill/10238"]
    assert bill["listed"]["bill_number"] == "HB. 1586" and bill["listed"]["year"] == 2018
    assert (
        bill["listed"]["title"]
        == "Constitution of the Federal Republic of Nigeria 1999 (Alteration) Bill, 2018"
    )
    assert bill["record_url"] == bill["url"]
    assert [r["reason"] for r in found.rejected] == ["no title"]


def test_the_national_library_is_read_from_its_data_interface_and_a_failing_set_record_by_record():
    oai = nigeria.OAI
    api = nigeria.NLN_API
    item_uuid = "11111111-2222-4333-8444-555555555555"
    stream = "66666666-7777-4888-8999-000000000000"
    identifiers = (
        '<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListIdentifiers>'
        "<header><identifier>oai:nigeriareposit.nln.gov.ng:20.500.14186/1387</identifier></header>"
        "</ListIdentifiers></OAI-PMH>"
    )
    item = {
        "uuid": item_uuid,
        "handle": "20.500.14186/1387",
        "lastModified": "2024-01-01T00:00:00Z",
        "metadata": {
            "dc.title": [{"value": "Federal Republic of Nigeria Official Gazette Vol.106 No.120"}],
            "dc.date.issued": [{"value": "2019-08-06"}],
            "dc.relation.ispartofseries": [{"value": "No. 120 Vol. 106"}],
            "dc.identifier.uri": [{"value": "https://nigeriareposit.nln.gov.ng/handle/20.500.14186/1387"}],
        },
    }
    bundles = {
        "_embedded": {
            "bundles": [
                {"name": "ORIGINAL", "_links": {"bitstreams": {"href": f"{api}/core/bundles/b1/bitstreams"}}}
            ]
        }
    }
    streams = {
        "_embedded": {
            "bitstreams": [
                {
                    "uuid": stream,
                    "name": "NCoS_act_2019.pdf",
                    "sizeBytes": 95131,
                    "sequenceId": 1,
                    "checkSum": {"checkSumAlgorithm": "MD5", "value": "a" * 32},
                }
            ]
        }
    }
    pages = {
        f"{oai}?verb=ListRecords&metadataPrefix=xoai&set=col_20.500.14186_350": polite.Unreachable(
            "HTTP 500"
        ),
        f"{oai}?verb=ListIdentifiers&metadataPrefix=oai_dc&set=col_20.500.14186_350": identifiers,
        f"{oai}?verb=GetRecord&metadataPrefix=xoai&identifier=oai:nigeriareposit.nln.gov.ng:20.500.14186/1387": (
            polite.Unreachable("HTTP 500")
        ),
        f"{api}/pid/find?id=20.500.14186/1387": json.dumps(item),
        f"{api}/core/items/{item_uuid}/bundles": json.dumps(bundles),
        f"{api}/core/bundles/b1/bitstreams": json.dumps(streams),
        f"{oai}?verb=ListRecords&metadataPrefix=xoai&set=col_20.500.14186_403": fixture("nln-oai-xoai.xml"),
    }
    found = read("nln-repository", Site(pages))
    act = found.items[f"{api}/core/bitstreams/49832e2c-d269-4da4-b108-3bd7b54f9345/content"]
    assert (
        act["type"] == "act"
        and act["record_url"] == "https://nigeriareposit.nln.gov.ng/handle/20.500.14186/1682"
    )
    assert act["listed"]["file"]["checksum"] == "beff81fa4a19464f178701c21df63303"
    assert act["listed"]["size_bytes"] == 5505101 and act["listed"]["date_issued"] == "2023-05"
    assert "Assented by President" in act["listed"]["description"]
    assert act["listed_raw"].startswith("<") and "ORIGINAL" in act["listed_raw"]
    gazette = found.items[f"{api}/core/bitstreams/{stream}/content"]
    assert gazette["type"] == "gazette"
    assert (gazette["listed"]["gazette_number"], gazette["listed"]["gazette_volume"]) == (120, 106)
    assert gazette["listed"]["file"]["checksum_algorithm"] == "MD5"
    assert any("read from the REST API" in note for note in found.notes)
    assert not any("/search" in url or "/browse" in url for url in found.items)


def test_the_ministry_of_justice_constitution_with_the_size_its_media_api_states():
    pages = {
        "https://justice.gov.ng/nigerian-constitution/": fixture("moj-constitution.html"),
        "https://justice.gov.ng/wp-json/wp/v2/media?search=Nigerian-Constitution&per_page=20": fixture(
            "moj-media.json"
        ),
    }
    found = read("moj-constitution", Site(pages))
    (item,) = found.items.values()
    assert (
        item["url"] == "https://justice.gov.ng/wp-content/uploads/2020/09/Nigerian-Constitution.pdf"
    )  # https
    assert item["type"] == "constitution" and item["listed"]["size_bytes"] == 672495
    assert item["listed"]["posted"] == "2020-09-03" and item["listed"]["last_modified"] == "2020-09-03"
    assert item["listed"]["link_as_listed"].startswith("http://")


def sc_site(cap_hit: bool = True) -> Site:
    records = json.loads(fixture("sc-search.json"))["data"]

    def search(url: str):
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        low, high = query["judgmentDate"][0], query["judgmentDate2"][0]
        rows = [r for r in records if low <= r["dateJudgement"] <= high]
        return json.dumps({"success": True, "data": rows})

    api = nigeria.SC_API
    pages = {
        f"{api}/judgment/passed/521": fixture("sc-detail.json"),
        f"{api}/judgment/passed/215": '{"data": {}}',
    }
    pages[f"{api}/supreme-court-rules?active=1"] = fixture("sc-rules.json")
    site = Site(pages)
    original = site.get

    def get(url, **kw):
        if "/judgment-new/search?" in url:
            site.asked.append(url)
            return polite.Answer(url, url, 200, {}, search(url).encode(), [])
        if url.startswith(api) and url not in pages:
            return polite.Answer(url, url, 200, {}, b'{"data": {}}', [])
        return original(url, **kw)

    site.get = get
    return site


def test_the_supreme_court_one_item_per_judgment_its_opinions_linked_and_the_old_text_record(monkeypatch):
    monkeypatch.setattr(nigeria, "SC_CAP", 2)  # three records: the first window is over the cap
    site = sc_site()
    found = read("supreme-court", site, state={"run_date": "2026-10-22"})
    searches = [u for u in site.asked if "judgment-new/search" in u]
    assert len(searches) > 1 and "judgmentDate2=2026-10-22" in searches[0]  # halved until under the cap
    lead = found.items[
        "https://scnwebsites3.s3.af-south-1.amazonaws.com/judgements/original/f595739b-a64a-4dbf-9c36-4190e7a8cef8.pdf"
    ]
    assert lead["alternate_urls"] == [
        "https://scnwebsites3.s3.af-south-1.amazonaws.com/judgements/watermarked/lead_ae096fcd-af49-4120-bdb9-4ffce0b9cf45.pdf"
    ]  # the watermarked copy is kept as another rendition, not a second item
    assert lead["listed"]["suit_no"] == "SC/CV/499/2026" and lead["listed"]["opinion"] == "lead"
    assert lead["listed"]["justices"] == [
        "HON. JUSTICE ADAMU JAURO",
        "HON. JUSTICE HARUNA SIMON TSAMMANI",
        "HON. JUSTICE JOSEPH OLUBUNMI KAYODE OYEWOLE",
    ]
    assert lead["record_url"] == "https://supremecourt.gov.ng/judgment-delivered/521"
    opinions = [i for i in found.items.values() if i["listed"].get("opinion") == "contribution"]
    assert len(opinions) == 2 and all(o["listed"]["part_of"]["url"] == lead["url"] for o in opinions)
    assert {o["listed"]["justice"] for o in opinions} == {
        "Hon. Justice Adamu Jauro",
        "Hon. Justice Haruna Simon Tsammani",
    }
    old = found.items[f"{nigeria.SC_API}/judgment/passed/17"]  # no file: the court's own data record
    assert old["listed"]["text_characters"] > 0 and "judgement" not in old["listed_raw"]["record"]
    ruling = found.items[
        "https://scnwebsites3.s3.af-south-1.amazonaws.com/judgements/original/0aa1bb2c-0000-4000-8000-000000000215.pdf"
    ]
    assert ruling["type"] == "ruling" and "alternate_urls" not in ruling  # watermark settings, not a file
    rules = found.items["https://scnwebsites3.s3.af-south-1.amazonaws.com/courtrule/1746367810.pdf"]
    assert rules["type"] == "rules" and rules["listed"]["contents"] == "The Supreme Court Rule 2024"


def test_the_industrial_court_is_read_by_record_id_a_share_each_run_from_the_cursor():
    pages = {
        nigeria.NICN: fixture("nicn-front.html"),
        f"{nigeria.NICN}judgement.php?id=1": "<html><head><title>- National Industrial Court of Nigeria</title></head></html>",
        f"{nigeria.NICN}judgement.php?id=2": fixture("nicn-record-old.html"),
        f"{nigeria.NICN}judgement.php?id=3": fixture("nicn-record.html"),
    }
    first = read("nicn-judgments", Site(pages), budget=2)
    assert first.cursor["next_id"] == 3 and not first.cursor["sweep_finished"]
    assert [r["reason"] for r in first.rejected] == ["an empty record"]
    old = first.items[f"{nigeria.NICN}judgement.php?id=2"]
    assert old["type"] == "ruling" and old["listed"]["suit_no"] == "NICN/LA/488/2014"
    assert old["listed"]["judgment_date"] == "2015-05-27" and old["listed"]["division"] == "LAGOS"
    assert old["listed"]["from_page"]["structured_header"]["date_delivered"] == "2015-05-27"
    second = read("nicn-judgments", Site(pages), state=first.cursor, budget=2)
    new = second.items[f"{nigeria.NICN}judgement.php?id=3"]
    assert second.cursor["sweep_finished"] and new["type"] == "judgment"
    assert (
        new["listed"]["judgment_date"] == "2026-10-12" and new["listed"]["judge"] == "HON. JUSTICE A. JUDGE"
    )
    assert new["listed"]["front_listing"]["suit_no"] == "NICN/LA/1/2021"
    assert (
        "Representation" not in new["listed"] and "F. Counsel" in new["listed"]["from_page"]["representation"]
    )
    assert new["listed_raw"]["header_text"].startswith("IN THE NATIONAL INDUSTRIAL COURT")
    assert new["listed_raw"]["header_text"].endswith("JUDGMENT")  # the header only, not the judgment's text


def test_the_aviation_authority_record_pages_give_number_dates_and_whether_current():
    listing = "https://ncaa.gov.ng/documents/regulations/nigcars-2023-official-gazette/"
    pages = {url: "<html></html>" for _, url in nigeria.NCAA_LISTINGS}  # the other listings: empty
    pages[listing] = fixture("ncaa-listing.html")
    pages[f"{listing}personnel-licensing/"] = fixture("ncaa-record.html")
    found = read("ncaa", Site(pages))
    item = found.items["https://ncaa.gov.ng/media/mvmk433o/nigcars-part-2-personnel-licensing.pdf"]
    listed = item["listed"]
    assert (listed["number"], listed["issue_date"], listed["effective_date"], listed["is_current"]) == (
        "PART 2",
        "2023-08-11",
        "2023-08-11",
        "Yes",
    )
    assert (
        listed["set"] == "Nig.CARs 2023 (Official Gazette)"
        and item["record_url"] == f"{listing}personnel-licensing/"
    )
    assert "Is Current" in item["listed_raw"]["record"] and item["listing_url"] == listing
    assert any(r["reason"].startswith("record page HTTP 404") for r in found.rejected)  # the others' pages
    assert (listed["part"], listed["year"]) == (2, 2023)  # the same Part in another set: a likely duplicate
    assert nigeria.ncaa_part("Nig.CARs/2023/002", "Part 2 - Personnel Licensing") == 2


def test_the_communications_commission_published_and_draft_sections_and_determinations():
    regulations, determinations = (url for _, url, _ in nigeria.NCC_PAGES[::2])
    pages = {regulations: fixture("ncc-regulations.html"), determinations: fixture("ncc-determinations.html")}
    found = read("ncc", Site(pages, default="<html></html>"))
    published = found.items["https://ncc.gov.ng/media/1118/view"]
    assert published["type"] == "regulation" and published["listed"]["status"] == "published"
    assert published["listed"]["power"].startswith("Consistent with Section 70")
    assert any(i["listed"].get("status") == "draft" for i in found.items.values())
    ussd = found.items["https://ncc.gov.ng/media/198/view"]
    assert ussd["type"] == "order" and "July 24, 2020" in ussd["listed"]["description"]
    assert "USSD" in ussd["listed"]["tags"]
    assert any(r["reason"].startswith("not an instrument") for r in found.rejected)  # the consultation paper


def test_the_securities_commission_download_blocks_paragraph_links_and_circulars():
    rules = nigeria.SEC_PAGES[0][1]
    pages = {rules: fixture("sec-rules.html"), nigeria.SEC_CIRCULARS: fixture("sec-circulars.html")}
    found = read("sec", Site(pages, default="<html></html>"))
    first = found.items[
        "https://sec.gov.ng/documents/1514/Rules_for_Public_Offering_of_Securities_by_A_Free_Trade_Zone_Entity_FTZE.pdf"
    ]
    assert first["type"] == "rules" and first["listed"]["size_bytes"] == int(208.2 * 1024)
    consolidated = found.items[
        "https://sec.gov.ng/documents/1347/SEC_Consolidated_JUNE2013_SIGNEDWEBSITE_1.pdf"
    ]
    assert consolidated["listed"]["title"] == "Consolidated Rules 2013"  # a link inside a paragraph
    listed = found.items["https://sec.gov.ng/documents/23/AMENDMENTS_17415.pdf"]
    assert listed["listed"]["title"] == "Sundry Amendments to SEC Rules and Regulations - April 13, 2015"
    circular = found.items[
        "https://sec.gov.ng/for-investors/keep-track-of-circulars/circular_commencement-of-direct-cash-settlement-dcs/"
    ]
    assert circular["type"] == "circular" and circular["listed"]["date"] == "2026-02-03"
    assert not any("/de/" in url for url in found.items)  # the page in other languages is not a circular


def test_the_data_protection_commission_keeps_laws_and_translations_and_leaves_out_reports():
    found = read("ndpc", Site({"https://ndpc.gov.ng/resources/": fixture("ndpc-resources.html")}))
    act = found.items["https://ndpc.gov.ng/wp-content/uploads/2024/03/Nigeria_Data_Protection_Act_2023.pdf"]
    assert act["type"] == "act" and act["listed"]["year"] == 2023
    gaid = found.items["https://ndpc.gov.ng/wp-content/uploads/2025/07/NDP-ACT-GAID-2025-MARCH-20TH.pdf"]
    assert (
        gaid["type"] == "guideline"
        and "title" not in gaid["listed"]
        and gaid["listed"]["label"] == "Open Book"
    )
    yoruba = found.items["https://ndpc.gov.ng/wp-content/uploads/2025/12/Book_NDPAct_Yoruba.pdf"]
    assert yoruba["listed"]["language"] == "Yoruba"
    assert [r["title"] for r in found.rejected] == ["Nigeria Data Protection Commission Annual Report 2025"]


def test_state_house_keeps_executive_orders_and_assent_announcements_only():
    site = Site({})
    posts = fixture("statehouse-posts.json")
    site.get = lambda url, **kw: polite.Answer(url, url, 200, {"x-wp-totalpages": "1"}, posts, [])
    found = read("statehouse", site)
    kinds = {i["listed"]["post_id"]: i["type"] for i in found.items.values()}
    assert kinds == {65029: "executive_order", 63000: "press_release"}  # an opinion piece is not an order
    assert (
        found.items["https://statehouse.gov.ng/president-tinubu-assents-nimc-act-2026/"]["listed"]["about"]
        == "assent"
    )
    assert any(r["url"].startswith("https://example.com/") for r in found.rejected)  # off the host


def test_the_electricity_commission_reads_every_page_and_leaves_out_zip_schedules():
    first = "https://nerc.gov.ng/resource-category/regulations/"
    site = Site({first: fixture("nerc-regulations.html")}, default="<html></html>")
    found = read("nerc", site)
    asked = [u for u in site.asked if "regulations" in u]
    assert asked[0] == first and asked[-1] == f"{first}page/5/"  # the pages the first one names
    net = found.items["https://nerc.gov.ng/wp-content/uploads/2026/06/Net-Billing-Regulation-2026.pdf"]
    assert net["type"] == "regulation" and net["listed"]["posted"] == "2026-06-03"
    assert any(r["reason"].startswith("a ZIP archive") for r in found.rejected)


def test_nafdac_reads_its_table_data_with_the_nonce_its_page_carries():
    page = fixture("nafdac-page.html").decode()
    table = fixture("nafdac-table.json")
    site = Site({nigeria.SOURCES["nafdac"]["url"]: page})
    original = site.get

    def get(url, **kw):
        if "admin-ajax.php" in url:
            assert "ninja_table_public_nonce=" in url and "table_id=9904" in url
            return polite.Answer(url, url, 200, {}, table, [])
        return original(url, **kw)

    site.get = get
    found = read("nafdac", site)
    item = next(iter(found.items.values()))
    assert item["listed"]["title"] == "NAFDAC (Animal Feed Additive Advertisement) Regulations, 2024"
    assert (item["listed"]["product_type"], item["listed"]["status"]) == ("Veterinary", "Published")
    assert item["listed_raw"]["value"]["sn"] == "47"


def test_nesrea_gives_si_numbers_refuses_drive_files_and_ignores_spam_links():
    url = nigeria.SOURCES["nesrea"]["url"]
    found = read(
        "nesrea", Site({url: fixture("nesrea-laws.html")}, disallowed=("https://drive.google.com/uc",))
    )
    wetlands = found.items[
        "https://nesrea.gov.ng/wp-content/uploads/2025/05/Wetlands_River_Banks_and_Lake_Shores_Protection_-Regulations-2009.pdf"
    ]
    assert (wetlands["listed"]["si_number"], wetlands["listed"]["year"]) == (26, 2009)
    drive = [i for i in found.items.values() if "drive.google.com" in i["url"]]
    assert drive and all(i["refused"] == nigeria.DRIVE_REFUSED for i in drive)
    assert found.items["https://nesrea.gov.ng/wp-content/uploads/2025/05/NESREA-ACT.pdf"]["type"] == "act"
    assert not any("linklist" in i["url"] or "wangi" in i["url"] for i in found.items.values())


def test_the_corporate_affairs_library_keeps_one_item_for_a_file_uploaded_twice():
    site = Site({})
    media = fixture("cac-media.json")
    site.get = lambda url, **kw: polite.Answer(url, url, 200, {"x-wp-totalpages": "1"}, media, [])
    found = read("cac", site)
    first = found.items[
        "https://news.cac.gov.ng/wp-content/uploads/2021/01/COMPANIES-REGULATIONS-2021-published.pdf"
    ]
    assert first["alternate_urls"] == [
        "https://news.cac.gov.ng/wp-content/uploads/2026/01/COMPANIES-REGULATIONS-2021-published.pdf"
    ]  # the same title and size uploaded again: one document
    gazette = found.items[
        "https://news.cac.gov.ng/wp-content/uploads/2022/10/Companies-Regulations-2021-PDF-Gazette.pdf"
    ]
    assert gazette["listed"]["copy"] == "Gazette" and "alternate_urls" not in gazette  # another size: apart
    notice = found.items[
        "https://news.cac.gov.ng/wp-content/uploads/2022/04/Notice-on-Insolvency-Regulations-Approval-Commencement.pdf"
    ]
    assert notice["type"] == "circular"
    assert [r["title"] for r in found.rejected] == ["Business Name Form"]


def test_naicom_posts_are_their_files_and_images_and_the_broken_host_are_left_out():
    site = Site({})
    posts = fixture("naicom-posts.json")
    site.get = lambda url, **kw: polite.Answer(url, url, 200, {"x-wp-totalpages": "1"}, posts, [])
    found = read("naicom", site)
    kinds = {i["listed"]["post_id"]: i["type"] for i in found.items.values()}
    assert kinds == {41084: "act", 40899: "regulation", 41226: "circular"}
    assert {r["reason"].split(" (")[0] for r in found.rejected} == {
        "an image, not a document",
        "off the official host",
    }


def test_court_of_appeal_judgments_are_listed_and_their_drive_files_refused():
    url = (
        "https://coa-admin.courtofappeal.gov.ng/api/judgement/search?division=&caseNo=&party=&date=&p=1&ps=30"
    )
    found = read(
        "court-of-appeal",
        Site({url: fixture("coa-search.json")}, disallowed=("https://drive.google.com/uc",)),
    )
    (item,) = found.items.values()
    assert item["url"] == "https://drive.google.com/uc?export=download&id=1KGwNmMM2fYRMENiuT-dOnjKyeEWYHFBF"
    assert item["refused"] == nigeria.DRIVE_REFUSED and item["listed"]["appeal_no"] == "CA/ABJ/CR/233/2020"
    assert item["listed"]["panel"] == ["HARUNA SIMON TSAMMANI", "DANLAMI ZAMA SENCHI", "BATURE GAFAI"]
    assert found.without_file[0]["reason"] == "no Google Drive file link"


def test_the_federal_high_court_rules_are_titles_only_and_its_spam_links_are_rejected():
    found = read("fhc-rules", Site({nigeria.SOURCES["fhc-rules"]["url"]: fixture("fhc-rules.html")}))
    titles = {w["title"]: w["type"] for w in found.without_file}
    assert titles == {
        "Federal High Court (Civil Procedure) Rules, 2019": "rules",
        "Federal High Court (Tax Appeals) Rules, 2022": "rules",
        "Federal High Court (Criminal Trials) Practice Directions, 2013": "practice_direction",
    }
    assert {r["reason"] for r in found.rejected} == {"spam link"}
    assert "https://www.fhc.gov.ng/fhc/Pre-ElectionMPD.pdf" in found.items  # the survey's one file


def test_titles_are_sanity_checked_and_spam_words_are_caught_without_catching_laws():
    assert nigeria.sane_title("Slot Gacor Maxwin hari ini") == "spam words in the title"
    assert nigeria.sane_title("Casino Taxation Act (Amendment) Bill, 2019") is None
    assert nigeria.sane_title("Irrua Specialist Teaching Hospital Act") is None
    assert nigeria.sane_title("()") == "no title"
    assert nigeria.on_path("https://spam.example.com/a.pdf", {"nesrea.gov.ng"}, r"^/wp-content/")
    assert (
        nigeria.law_type("Code of Conduct") == "guideline"
        and nigeria.law_type("Acts of Nigeria 1964") == "act"
    )


def test_dates_as_the_sources_print_them():
    assert nigeria.to_date("11/08/2023") == "2023-08-11"  # day first
    assert nigeria.to_date("Wednesday, August 9, 2023") == "2023-08-09"
    assert nigeria.to_date("Oct. 10, 2026") == "2026-10-10"
    assert nigeria.to_date("MONDAY 12TH OCTOBER 2026") == "2026-10-12"
    assert nigeria.to_date("0000-00-00") is None and nigeria.to_date("2023-05") == "2023-05"


# --- the catalogue, the ledger and kept answers ----------------------------------------------------


def nass_client(feed: bytes, seen: list):
    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>home</html>")
        return httpx.Response(200, headers={"content-type": "application/json"}, content=feed)

    clock = [0.0]

    def tick():
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return polite.PoliteClient({"nass.gov.ng"}, client=http, clock=tick, sleep=sleep)


def test_a_run_keeps_every_answer_and_reparse_reads_them_again_without_the_site(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    seen = []
    summary = nigeria.catalogue(
        "nass-acts", client=nass_client(fixture("nass-magazine-track.json"), seen), log=lambda m: None
    )
    assert summary["items"] == 3 and summary["outcome"] == "ok"
    (run,) = nigeria.runs_of("nass-acts")
    index = json.loads((run / "index.json").read_text())
    assert [r["url"] for r in index["responses"]] == [seen[-1]] and "nass.gov.ng" in index["robots"]
    record = Ledger(tmp_path / "sync-ledger.json").data["items"][
        "nass-acts:https://nass.gov.ng/documents/download/11248"
    ]
    assert record["listing_url"].endswith("length=10000") and record["listed_raw"][0] == "ELECTORAL ACT, 2026"
    asked_before = len(seen)
    (again,) = nigeria.reparse("nass-acts", log=lambda m: None)
    assert len(seen) == asked_before  # no request to the site
    assert again["items"] == 3 and again["changes"] == {"added": 0, "changed": 0, "moved": 0, "gone": 0}
    state = Ledger(tmp_path / "sync-ledger.json").source("nass-acts")
    assert state["last_reparsed_at"] and state["last_check_outcome"] == "ok"  # a re-reading is not a check


def test_a_later_run_records_only_what_changed_and_what_is_no_longer_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    feed = json.loads(fixture("nass-magazine-track.json"))
    nigeria.catalogue("nass-acts", client=nass_client(json.dumps(feed).encode(), []), log=lambda m: None)
    feed["data"][0][0] = "NIGERIA REVENUE SERVICE (ESTABLISHMENT) ACT, 2025 (AS CORRECTED)"
    feed["data"] = [row for row in feed["data"] if row[6] != "11248"]
    feed["recordsTotal"] = len(feed["data"])
    summary = nigeria.catalogue(
        "nass-acts", client=nass_client(json.dumps(feed).encode(), []), log=lambda m: None
    )
    assert summary["changes"] == {"added": 0, "changed": 1, "moved": 0, "gone": 1}
    items = Ledger(tmp_path / "sync-ledger.json").data["items"]
    changed = items["nass-acts:https://nass.gov.ng/documents/download/11251"]
    assert [h["change"] for h in changed["history"]] == ["new", "listing changed"]
    assert changed["history"][-1]["before"]["title"] == "NIGERIA REVENUE SERVICE (ESTABLISHMENT) ACT, 2025"
    gone = items["nass-acts:https://nass.gov.ng/documents/download/11248"]
    assert gone["gone"] and gone["history"][-1]["change"] == "no longer listed"  # recorded, never deleted


def test_a_bot_check_stops_the_source_and_is_never_read_as_nothing_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    nigeria.catalogue(
        "nass-acts", client=nass_client(fixture("nass-magazine-track.json"), []), log=lambda m: None
    )
    challenge = b"<html><head><title>Just a moment...</title></head><body>cf_chl_opt</body></html>"
    summary = nigeria.catalogue("nass-acts", client=nass_client(challenge, []), log=lambda m: None)
    assert summary["outcome"].startswith("stopped: refused or bot check") and not summary["complete"]
    items = Ledger(tmp_path / "sync-ledger.json").data["items"]
    assert not any(record.get("gone") for record in items.values())


def test_a_resumable_listing_resumes_from_the_cursor_and_its_runs_are_read_again_in_order(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    pages = {
        nigeria.NICN: fixture("nicn-front.html"),
        f"{nigeria.NICN}judgement.php?id=1": b"<html><head><title></title></head></html>",
        f"{nigeria.NICN}judgement.php?id=2": fixture("nicn-record-old.html"),
        f"{nigeria.NICN}judgement.php?id=3": fixture("nicn-record.html"),
    }

    def handler(request):
        url = str(request.url)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=pages[url])

    def make():
        clock = [0.0]
        http = httpx.Client(transport=httpx.MockTransport(handler))
        return polite.PoliteClient(
            {"nicnadr.gov.ng"},
            client=http,
            clock=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        )

    first = nigeria.catalogue("nicn-judgments", budget=2, client=make(), log=lambda m: None)
    second = nigeria.catalogue("nicn-judgments", budget=2, client=make(), log=lambda m: None)
    assert first["cursor"]["next_id"] == 3 and second["cursor"]["sweep_finished"]
    assert second["items_in_ledger"] == 2 and second["changes"]["gone"] == 0
    again = nigeria.reparse("nicn-judgments", log=lambda m: None)
    assert [r["items"] for r in again] == [1, 1]
    assert Ledger(tmp_path / "sync-ledger.json").source("nicn-judgments")["cursor"]["sweep_finished"]


# --- likely duplicates, the fetch work list and what the cloud fetched ---------------------------


def ledger_with(tmp_path, records: dict) -> Ledger:
    ledger = Ledger(tmp_path / "sync-ledger.json")
    for item_id, record in records.items():
        ledger.data["items"][item_id] = {
            "source": item_id.split(":", 1)[0],
            "url": item_id.split(":", 1)[1],
            "history": [{"at": "2026-10-22T00:00:00Z", "change": "new"}],
            **record,
        }
    ledger.save()
    return ledger


def test_likely_duplicates_are_flagged_across_sources_by_title_and_by_case(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    nass = "nass-acts:https://nass.gov.ng/documents/download/11247"
    nln = "nln-repository:https://nigeriareposit.nln.gov.ng/server/api/core/bitstreams/x/content"
    other = "nass-acts:https://nass.gov.ng/documents/download/1"
    case_a = "supreme-court:https://scnwebsites3.s3.af-south-1.amazonaws.com/a.pdf"
    case_b = "supreme-court:https://scnwebsites3.s3.af-south-1.amazonaws.com/b.pdf"
    ledger_with(
        tmp_path,
        {
            nass: {
                "type": "act",
                "listed": {"title": "JOINT REVENUE BOARD OF NIGERIA (ESTABLISHMENT) ACT, 2025"},
            },
            nln: {
                "type": "act",
                "listed": {"title": "Joint Revenue Board of Nigeria (Establishment) Act, 2025"},
            },
            other: {"type": "act", "listed": {"title": "ELECTORAL ACT, 2026"}},
            case_a: {
                "type": "judgment",
                "listed": {"suit_no": "SC/CV/499/2026", "judgment_date": "2026-09-24"},
            },
            case_b: {
                "type": "judgment",
                "listed": {"suit_no": "SC.CV.499.2026", "judgment_date": "2026-09-24"},
            },
        },
    )
    result = nigeria.flag_duplicates()
    items = Ledger(tmp_path / "sync-ledger.json").data["items"]
    assert items[nass]["likely_duplicates"] == [{"item": nln, "by": "title"}]
    assert items[case_a]["likely_duplicates"] == [{"item": case_b, "by": "case"}]
    assert "likely_duplicates" not in items[other]
    assert result == {"items_flagged": 4, "flagged_across_sources": 2}


def test_the_fetch_work_list_takes_new_and_changed_items_with_their_validators(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    base = "nass-acts:https://nass.gov.ng/documents/download/"
    fetched = {
        "sha256": "ab" * 32,
        "stored": "gs://b/sources/ng/nass-acts/" + "ab" * 32 + ".pdf",
        "last_fetched": "2026-10-22T01:00:00Z",
    }
    ledger_with(
        tmp_path,
        {
            f"{base}1": {"type": "act", "listed": {"title": "New Act, 2025"}},
            f"{base}2": {"type": "act", "listed": {}, "refused": "robots.txt"},
            f"{base}3": {"type": "act", "listed": {}, "gone": "2026-10-22T02:00:00Z"},
            f"{base}4": {"type": "act", "listed": {}, **fetched, "validators": {"etag": '"v1"'}},
            f"{base}5": {
                "type": "act",
                "listed": {"title": "Changed"},
                **fetched,
                "history": [
                    {"at": "2026-10-22T00:00:00Z", "change": "new"},
                    {"at": "2026-10-22T03:00:00Z", "change": "listing changed"},
                ],
            },
            "nass-bills:https://nass.gov.ng/documents/bill/9": {"type": "bill", "listed": {}},
        },
    )
    with pytest.raises(ValueError):
        nigeria.fetch_worklist({"approved_by": "owner", "basis": ""})
    work = nigeria.fetch_worklist({"approved_by": "owner", "basis": "chat, 2026-10-22"})
    assert [i["item"] for i in work["items"]] == [f"{base}1", f"{base}5"]  # bills are metadata only
    assert work["items"][1]["known"]["sha256"] == "ab" * 32
    assert work["left_out"] == {"refused": 1, "no longer listed": 1, "stored, unchanged": 1}
    assert work["by_host"] == {"nass.gov.ng": 2} and work["delay"] >= 2.0
    recheck = nigeria.fetch_worklist({"approved_by": "owner", "basis": "b"}, recheck=True)
    assert recheck["items"][1]["known"]["etag"] == '"v1"'
    assert work["items"][0]["key"] == nigeria.item_key(f"{base}1")


class Store:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def put(self, name, data, content_type, metadata=None, replace=False):
        if name in self.objects and not replace:
            return False
        self.objects[name] = data
        return True

    def get(self, name):
        return self.objects[name]

    def list(self, prefix):
        return sorted(n for n in self.objects if n.startswith(prefix))

    def exists(self, name):
        return name in self.objects

    def uri(self, name):
        return f"gs://test/{name}"


def test_what_the_cloud_fetched_is_recorded_in_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    one = "nass-acts:https://nass.gov.ng/documents/download/1"
    two = "nln-repository:https://nigeriareposit.nln.gov.ng/server/api/core/bitstreams/b/content"
    three = "nass-acts:https://nass.gov.ng/documents/download/3"
    ledger_with(tmp_path, {i: {"type": "act", "listed": {}} for i in (one, two, three)})
    sha = "cd" * 32
    stored = f"gs://test/sources/ng/nass-acts/{sha}.pdf"

    def done(item, outcome, **more):
        key = nigeria.item_key(item)
        record = {
            "item": item,
            "outcome": outcome,
            "sha256": sha,
            "bytes": 10,
            "suffix": ".pdf",
            "stored": stored,
        }
        record |= {"retrieved_at": "2026-10-23T00:00:00Z", "validators": {"etag": '"e"'}, **more}
        return {
            f"work/ng/fetched/{key}.json": json.dumps(record).encode(),
            f"work/ng/fetch-done/W1/{key}": b"",
        }

    objects = {"work/ng/fetch.json": json.dumps({"id": "W1"}).encode()}
    objects |= done(one, "stored") | done(two, "already_stored", same_file_as=one)
    objects[f"work/ng/fetch-failed/{nigeria.item_key(three)}.json"] = json.dumps(
        {"item": three, "at": "2026-10-23T00:00:00Z", "reason": "HTTP 404", "worklist": "W1"}
    ).encode()
    summary = nigeria.pull_fetched(Store(objects), log=lambda m: None)
    assert summary == {
        "worklist": "W1",
        "stored": 1,
        "already_stored": 1,
        "unchanged": 0,
        "empty": 0,
        "failed": 1,
        "hosts_stopped": [],
    }
    items = Ledger(tmp_path / "sync-ledger.json").data["items"]
    assert items[one]["stored"] == stored and items[one]["validators"] == {"etag": '"e"'}
    assert items[one]["other_sources"] == [two] and items[two]["same_file_as"] == one
    assert items[three]["fetch_failed"] == "HTTP 404"


def test_the_state_and_kept_runs_are_mirrored_to_the_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    nigeria.catalogue(
        "nass-acts", client=nass_client(fixture("nass-magazine-track.json"), []), log=lambda m: None
    )
    store = Store()
    nigeria.push_held(store)
    names = store.list("state/ng/")
    assert "state/ng/sync-ledger.json" in names and "state/ng/catalogue/nass-acts.json" in names
    assert any(n.endswith("/responses.jsonl.gz") for n in names) and any(
        n.endswith("/index.json") for n in names
    )
    assert not store.list("state/rw/")


def test_every_type_and_source_is_one_the_brief_names():
    assert {kind for meta in nigeria.SOURCES.values() for kind in meta["kinds"]} <= set(nigeria.TYPES)
    assert not {"cbn", "nrs", "nuprc", "fct-high-court"} & set(nigeria.SOURCES)
    assert set(nigeria.READERS) == set(nigeria.SOURCES)
    assert all(meta["wave"] in (1, 2) for meta in nigeria.SOURCES.values())
    assert date.fromisoformat("2026-10-22")  # the owner's decision the sources follow


# --- the National Industrial Court catalogued from the cloud fetch -------------------------------


def nicn_ledger(tmp_path):
    """A ledger where ids 1 and 3 are catalogued, 4 was found empty, and the cursor is at 2."""
    ledger = ledger_with(
        tmp_path,
        {
            f"nicn-judgments:{nigeria.nicn_url(n)}": {
                "type": "judgment",
                "listed": {"record_id": n, "title": f"A -VS- B {n}"},
            }
            for n in (1, 3)
        },
    )
    ledger.data["sources"]["nicn-judgments"] = {
        "source": "nicn-judgments",
        "cursor": {
            "next_id": 2,
            "newest_id": 4,
            "sweep_finished": False,
            "sweep_started": "2026-10-22T23:08:29Z",
        },
        "empty_ids": [4],
    }
    ledger.save()
    card = {
        "suit_no": "NICN/LA/1/2026",
        "judge": "Hon. Justice A. Judge",
        "parties": "A -VS- B",
        "html": "<div>card</div>",
    }
    front = {"url": nigeria.NICN, "read_at": "2026-10-23T08:00:00Z", "newest_id": 6, "cards": {6: card}}
    return front


def test_the_fetch_work_list_adds_the_industrial_court_ids_not_yet_catalogued(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    front = nicn_ledger(tmp_path)
    work = nigeria.fetch_worklist(
        {"approved_by": "owner", "basis": "b"}, ["nicn-judgments"], nicn_front=front
    )
    pending = [i for i in work["items"] if i["listed"].get("catalogue_from_page")]
    assert [i["listed"]["record_id"] for i in pending] == [2, 5, 6]  # 3 is catalogued, 4 was empty
    six = pending[-1]
    assert six["url"] == "https://nicnadr.gov.ng/judgement/judgement.php?id=6" and six["expect"] == "html"
    assert six["listed"] == {"record_id": 6, "catalogue_from_page": "nicn"} and six["type"] is None
    assert six["key"] == nigeria.item_key(f"nicn-judgments:{six['url']}") and six["front_listing"]["suit_no"]
    assert six["record_url"] == six["listing_url"] == six["url"]
    assert work["nicn_catalogue"] == {
        "front_page": nigeria.NICN,
        "front_read_at": "2026-10-23T08:00:00Z",
        "newest_id": 6,
        "ids": 3,
        "from_id": 2,
        "to_id": 6,
    }
    trial = nigeria.fetch_worklist(
        {"approved_by": "o", "basis": "b"}, ["nicn-judgments"], nicn_front=front, nicn_limit=1
    )
    assert [i["listed"]["record_id"] for i in trial["items"] if i["listed"].get("catalogue_from_page")] == [2]
    plain = nigeria.fetch_worklist({"approved_by": "o", "basis": "b"}, ["nicn-judgments"])
    assert not any(i["listed"].get("catalogue_from_page") for i in plain["items"])  # no front page read: none


def nicn_fetched(url, record_id, catalogue, key=None, stored=True):
    item = f"nicn-judgments:{url}"
    record = {
        "item": item,
        "key": key or nigeria.item_key(item),
        "source": "nicn-judgments",
        "url": url,
        "final_url": url,
        "record_id": record_id,
        "retrieved_at": "2026-10-23T09:00:00Z",
        "status": 200,
        "validators": {"last_modified": "Thu, 22 Oct 2026 10:00:00 GMT"},
        "catalogue": catalogue,
        "worklist": "W2",
    }
    if catalogue.get("empty"):
        return record | {"outcome": "empty", "stored": None}
    sha = f"{record_id:064d}"
    return record | {
        "outcome": "stored",
        "sha256": sha,
        "bytes": 70000,
        "suffix": ".html",
        "stored": f"gs://test/sources/ng/nicn-judgments/{sha}.html",
    }


def test_pull_fetched_builds_the_industrial_court_entries_from_the_job_records(tmp_path, monkeypatch):
    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    front = nicn_ledger(tmp_path)
    entry = {
        "type": "ruling",
        "listed": {
            "record_id": 2,
            "title": "C -VS- D",
            "suit_no": "NICN/ABJ/2/2020",
            "judgment_date": "2020-01-02",
        },
        "listed_raw": {
            "page_title": "C -VS- D- National Industrial Court of Nigeria",
            "header_text": "IN THE …",
        },
    }
    six = {
        "type": "judgment",
        "listed": {"record_id": 6, "title": "A -VS- B", "front_listing": {"suit_no": "x"}},
    }
    records = [
        nicn_fetched(nigeria.nicn_url(2), 2, entry),
        nicn_fetched(nigeria.nicn_url(5), 5, {"empty": True, "reason": "an empty record"}),
        nicn_fetched(nigeria.nicn_url(6), 6, six | {"listed_raw": {}}),
    ]
    worklist = {"id": "W2", "nicn_catalogue": {"newest_id": 6}}
    objects = {"work/ng/fetch.json": json.dumps(worklist).encode()}
    for record in records:
        objects[f"work/ng/fetched/{record['key']}.json"] = json.dumps(record).encode()
        objects[f"work/ng/fetch-done/W2/{record['key']}"] = b""
    ledger = Ledger(tmp_path / "sync-ledger.json")
    ledger.data["sources"]["nicn-judgments"]["nicn_worklist_pending"] = "W2"
    ledger.save()
    summary = nigeria.pull_fetched(Store(objects), log=lambda m: None)
    assert (summary["stored"], summary["empty"]) == (2, 1)
    assert summary["nicn_catalogued"]["entries"] == 2 and summary["nicn_catalogued"]["changes"]["added"] == 2
    ledger = Ledger(tmp_path / "sync-ledger.json")
    two = ledger.data["items"][f"nicn-judgments:{nigeria.nicn_url(2)}"]
    assert two["type"] == "ruling" and two["listed"]["suit_no"] == "NICN/ABJ/2/2020"
    assert two["listed_raw"]["header_text"] == "IN THE …"
    assert two["record_url"] == two["listing_url"] == nigeria.nicn_url(2)
    assert two["stored"].endswith(".html") and two["validators"]["last_modified"].startswith("Thu")
    assert two["history"][0]["change"] == "new" and "cloud-catalogue" in two["history"][0]["run"]
    state = ledger.source("nicn-judgments")
    assert state["empty_ids"] == [4, 5] and "nicn_worklist_pending" not in state
    assert state["cursor"]["next_id"] == 7 and state["cursor"]["sweep_finished"]  # past 2, 3, 4, 5, 6
    assert not nigeria.nicn_pending(ledger, front)  # nothing is listed again
    assert [i["listed"]["record_id"] for i in nigeria.nicn_pending(ledger, {**front, "newest_id": 8})] == [
        7,
        8,
    ]
