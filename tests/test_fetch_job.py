"""The cloud fetch job: the work list shared by host, each file sniffed, stored once by its
fingerprint with its sidecar, a stopped run run again, unchanged files not taken again, and a host
left alone after a refusal."""

import io
import json
import zipfile

import httpx

from ingestion import fetch_job, polite


class Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, name, data, content_type, metadata=None, replace=False):
        if name in self.objects and not replace:
            return False
        self.objects[name] = data
        return True

    def get(self, name):
        return self.objects[name]

    def list(self, prefix):
        return sorted(n for n in self.objects if n.startswith(prefix))

    def uri(self, name):
        return f"gs://test/{name}"


PDF = b"%PDF-1.7\n" + b"x" * 200
HOSTS = {"nass.gov.ng", "nigeriareposit.nln.gov.ng", "nicnadr.gov.ng"}


def item(n, host, source="nass-acts", expect="document", known=None):
    url = f"https://{host}/file/{n}"
    return {
        "item": f"{source}:{url}",
        "key": f"k{n}",
        "source": source,
        "url": url,
        "host": host,
        "type": "act",
        "expect": expect,
        "size_limit": 1_000_000,
        "listed": {"title": f"Act {n}"},
        "listing_url": f"https://{host}/feed",
        "record_url": None,
        "alternate_urls": None,
        "known": known,
    }


def worklist(items):
    sources = {
        "nass-acts": {"file_hosts": ["nass.gov.ng"]},
        "nln-repository": {"file_hosts": ["nigeriareposit.nln.gov.ng"]},
        "nicn-judgments": {"file_hosts": ["nicnadr.gov.ng"]},
    }
    approval = {"approved_by": "owner", "basis": "chat 2026-10-22"}
    return {
        "id": "W1",
        "country": "ng",
        "delay": 2.0,
        "approval": approval,
        "sources": sources,
        "items": items,
    }


def fetcher(store, work, answers, seen):
    def handler(request):
        url = str(request.url)
        seen.append((url, dict(request.headers)))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        status, headers, body = answers[url] if not callable(answers[url]) else answers[url](request)
        return httpx.Response(status, headers=headers, content=body)

    clock = [0.0]
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = polite.PoliteClient(
        HOSTS, client=http, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)
    )
    return fetch_job.Fetcher(store, work, client=client, log=lambda m: None)


def test_the_work_list_is_shared_by_host_so_no_host_is_fetched_by_two_tasks():
    items = [item(n, "nicnadr.gov.ng") for n in range(6)] + [item(n, "nass.gov.ng") for n in range(6, 9)]
    items += [item(9, "nigeriareposit.nln.gov.ng")]
    shares = [fetch_job.share_by_host(items, task, 2) for task in range(2)]
    assert sorted(i["key"] for share in shares for i in share) == sorted(i["key"] for i in items)
    hosts = [{i["host"] for i in share} for share in shares]
    assert not hosts[0] & hosts[1]
    assert hosts == [{"nicnadr.gov.ng"}, {"nass.gov.ng", "nigeriareposit.nln.gov.ng"}]  # busiest first
    assert fetch_job.share_by_host(items, 0, 1) == items


def test_a_fetched_answer_is_taken_by_what_its_bytes_are():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    assert fetch_job.sniff(PDF, "document") == (".pdf", "application/pdf")
    assert fetch_job.sniff(buffer.getvalue(), "document")[0] == ".docx"
    assert fetch_job.sniff(fetch_job.OLE + b"\x00" * 100, "document")[0] == ".doc"
    assert fetch_job.sniff(b"<!DOCTYPE html><html><body>judgment</body></html>", "html") == (
        ".html",
        "text/html",
    )
    assert fetch_job.sniff(b'{"success": true, "data": {}}', "json")[0] == ".api.json"
    for data, expect in (
        (b"<html>Not found</html>", "document"),
        (b"PK\x03\x04junk", "document"),
        (b"%PDF", "json"),
    ):
        try:
            fetch_job.sniff(data, expect)
        except fetch_job.NotTaken:
            continue
        raise AssertionError(f"{data!r} taken as {expect}")


def test_each_file_is_stored_once_with_its_sidecar_and_a_stopped_run_skips_what_it_did():
    store = Store()
    work = worklist([item(1, "nass.gov.ng"), item(2, "nigeriareposit.nln.gov.ng", source="nln-repository")])
    answers = {
        "https://nass.gov.ng/file/1": (
            200,
            {
                "etag": '"e1"',
                "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT",
                "content-length": str(len(PDF)),
            },
            PDF,
        ),
        "https://nigeriareposit.nln.gov.ng/file/2": (200, {}, PDF),  # the same bytes from another source
    }
    seen = []
    totals = fetcher(store, work, answers, seen).run(work["items"])
    assert totals["stored"] == 1 and totals["already_stored"] == 1
    files = [n for n in store.list("sources/ng/") if n.endswith(".pdf")]
    assert len(files) == 1 and files[0].startswith("sources/ng/nass-acts/")  # stored once
    sidecar = json.loads(store.get(files[0][: -len(".pdf")] + ".json"))
    assert (
        sidecar["etag"] == '"e1"' and sidecar["last_modified"].startswith("Mon") and sidecar["content_length"]
    )
    assert (
        sidecar["item"] == work["items"][0]["item"] and sidecar["final_url"] == "https://nass.gov.ng/file/1"
    )
    assert sidecar["listed"] == {"title": "Act 1"} and sidecar["approval"]["approved_by"] == "owner"
    assert [o["item"] for o in sidecar["other_sources"]] == [work["items"][1]["item"]]
    occurrence = store.list(files[0][: -len(".pdf")] + ".occurrences/")
    assert len(occurrence) == 1 and json.loads(store.get(occurrence[0]))["source"] == "nln-repository"
    second = json.loads(store.get("work/ng/fetched/k2.json"))
    assert second["outcome"] == "already_stored" and second["same_file_as"] == work["items"][0]["item"]
    asked = len(seen)
    again = fetcher(store, work, answers, seen).run(work["items"])  # the run run again
    assert again["skipped"] == 2 and len(seen) == asked


def test_an_unchanged_file_is_not_taken_again_by_its_validators_or_its_fingerprint():
    store = Store()
    import hashlib

    sha = hashlib.sha256(PDF).hexdigest()
    known = {"sha256": sha, "stored": f"gs://test/sources/ng/nass-acts/{sha}.pdf", "etag": '"e1"'}
    work = worklist(
        [item(1, "nass.gov.ng", known=known), item(2, "nass.gov.ng", known={**known, "etag": None})]
    )
    answers = {
        "https://nass.gov.ng/file/1": lambda request: (
            (304, {"etag": '"e1"'}, b"") if request.headers.get("if-none-match") == '"e1"' else (200, {}, PDF)
        ),
        "https://nass.gov.ng/file/2": (200, {}, PDF),
    }
    seen = []
    totals = fetcher(store, work, answers, seen).run(work["items"])
    assert totals["unchanged"] == 2 and not store.list("sources/")
    first, second = (json.loads(store.get(f"work/ng/fetched/k{n}.json")) for n in (1, 2))
    assert (first["how"], second["how"]) == ("304 Not Modified", "same fingerprint")


def test_a_bot_check_stops_its_host_and_the_task_goes_on_to_the_next_host():
    store = Store()
    items = [item(n, "nass.gov.ng") for n in (1, 2, 3)] + [
        item(4, "nicnadr.gov.ng", source="nicn-judgments", expect="html")
    ]
    work = worklist(items)
    page = b"<!doctype html><html><body>IN THE NATIONAL INDUSTRIAL COURT</body></html>"
    answers = {
        "https://nass.gov.ng/file/1": (404, {}, b"gone"),
        "https://nass.gov.ng/file/2": (403, {}, b"Forbidden"),
        "https://nass.gov.ng/file/3": (200, {}, PDF),
        "https://nicnadr.gov.ng/file/4": (200, {"content-type": "text/html"}, page),
    }
    seen = []
    totals = fetcher(store, work, answers, seen).run(items)
    assert "https://nass.gov.ng/file/3" not in [url for url, _ in seen]  # never asked after the refusal
    (stop,) = totals["hosts_stopped"]
    assert stop["host"] == "nass.gov.ng" and stop["sources"] == ["nass-acts"]
    assert json.loads(store.get("work/ng/fetch-stopped/W1/nass.gov.ng.json"))["reason"].startswith("refused")
    assert json.loads(store.get("work/ng/fetch-failed/k1.json"))["reason"] == "HTTP 404"
    html = [n for n in store.list("sources/ng/nicn-judgments/") if n.endswith(".html")]
    assert len(html) == 1 and store.get(html[0]) == page  # the page as fetched


def test_an_answer_that_is_not_the_document_is_recorded_as_a_failure():
    store = Store()
    work = worklist([item(1, "nass.gov.ng")])
    answers = {
        "https://nass.gov.ng/file/1": (
            200,
            {"content-type": "text/html"},
            b"<html>Document not available</html>",
        )
    }
    totals = fetcher(store, work, answers, []).run(work["items"])
    assert totals["failed"] == 1 and not store.list("sources/")
    assert (
        "web page instead of the document" in json.loads(store.get("work/ng/fetch-failed/k1.json"))["reason"]
    )


def test_an_industrial_court_page_is_read_once_stored_and_catalogued_and_pulled_into_the_ledger(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from ingestion import nigeria
    from ingestion.sync import Ledger

    monkeypatch.setattr(nigeria, "STAGING", tmp_path)
    page = (Path(__file__).parent / "fixtures" / "ng" / "nicn-record.html").read_bytes()
    empty = b"<!doctype html><html><head><title>- National Industrial Court of Nigeria</title></head><body></body></html>"
    front = {
        "url": nigeria.NICN,
        "read_at": "2026-10-23T08:00:00Z",
        "newest_id": 3,
        "cards": {3: {"suit_no": "NICN/LA/1/2021", "html": "<div/>"}},
    }
    work = nigeria.fetch_worklist(
        {"approved_by": "owner", "basis": "b"}, ["nicn-judgments"], nicn_front=front
    )
    work["items"] = [i for i in work["items"] if i["listed"]["record_id"] in (1, 3)]
    answers = {
        nigeria.nicn_url(1): (200, {"content-type": "text/html"}, empty),
        nigeria.nicn_url(3): (
            200,
            {"content-type": "text/html; charset=UTF-8", "last-modified": "Thu, 22 Oct 2026 10:00:00 GMT"},
            page,
        ),
    }
    store, seen = Store(), []
    totals = fetcher(store, work, answers, seen).run(work["items"])
    assert (totals["stored"], totals["empty"]) == (1, 1)
    assert sorted(url for url, _ in seen if "judgement.php" in url) == [
        nigeria.nicn_url(1),
        nigeria.nicn_url(3),
    ]  # once each
    (html,) = [n for n in store.list("sources/ng/nicn-judgments/") if n.endswith(".html")]
    assert store.get(html) == page  # the whole page, as fetched
    sidecar = json.loads(store.get(html[: -len(".html")] + ".json"))
    expected = nigeria.nicn_entry(3, page.decode(), work["items"][1]["front_listing"], len(page), False)
    assert (sidecar["type"], sidecar["listed"], sidecar["listed_raw"]) == expected  # read_nicn's own rule
    assert sidecar["listed"]["suit_no"] == "NICN/LA/1/2021" and sidecar["listed"]["front_listing"]["suit_no"]
    assert sidecar["listed_in_work_list"] == {"record_id": 3, "catalogue_from_page": "nicn"}
    blank = json.loads(store.get(f"work/ng/fetched/{work['items'][0]['key']}.json"))
    assert blank["outcome"] == "empty" and blank["catalogue"] == {"empty": True, "reason": "an empty record"}

    store.put("work/ng/fetch.json", json.dumps(work).encode(), "application/json", replace=True)
    summary = nigeria.pull_fetched(store, log=lambda m: None)  # the job's records only: no page is read here
    assert summary["nicn_catalogued"]["entries"] == 1 and summary["empty"] == 1
    ledger = Ledger(tmp_path / "sync-ledger.json")
    item = ledger.data["items"][f"nicn-judgments:{nigeria.nicn_url(3)}"]
    assert (
        item["type"] == "judgment"
        and item["listed"] == sidecar["listed"]
        and item["stored"].endswith(".html")
    )
    assert item["listed_raw"]["header_text"].endswith("JUDGMENT")
    assert ledger.source("nicn-judgments")["empty_ids"] == [1]
    assert ledger.source("nicn-judgments")["cursor"]["next_id"] == 2  # id 2 was not fetched: it stays to do
