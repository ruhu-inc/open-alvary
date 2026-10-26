"""Reading a collection in the cloud (ingestion.cloud_read): the free survey counts pages before any
reading is billed, the work list carries the budget's approval, and a reader keeps every page read
before, never replaces a reading it cannot read, and resumes where it stopped."""

import json
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from ingestion import cloud_read
from ingestion.readers import Reading


def pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


class Store:
    def __init__(self, objects):
        self.objects = objects

    def exists(self, name):
        return name in self.objects

    def get(self, name):
        if self.objects[name] is None:
            raise OSError("unreadable")
        return self.objects[name]

    def put(self, name, data, content_type, replace=False):
        self.objects[name] = data

    def list(self, prefix):
        return sorted(n for n in self.objects if n.startswith(prefix))


class Reader:
    name = "docai"

    def read(self, part):
        count = len(PdfReader(str(part)).pages)
        return Reading("docai", {}, [{"page": k, "nodes": ["read"]} for k in range(1, count + 1)], count, {})

    def version(self):
        return {"processor": "p", "version": "v"}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(
        cloud_read.gazette_layout,
        "text_layer",
        lambda p: [{"words": ["w"] * 30}, {"words": []}, {"words": []}],
    )
    monkeypatch.setattr(cloud_read.ruled_tables, "image_pages", lambda p: {2, 3})
    return Store(
        {
            "sources/ng/nass/aaa.pdf": pdf(3),
            "sources/ng/nass/aaa.json": b"{}",
            "sources/ng/sc/bbb.pdf": b"not a pdf",
            "sources/ng/nicn/ccc.html": b"<html></html>",
        }
    )


def test_the_survey_counts_pages_free_and_the_summary_prices_the_reading(store):
    assert cloud_read.stored_files(store, "ng") == ["sources/ng/nass/aaa.pdf", "sources/ng/sc/bbb.pdf"]
    result = cloud_read.survey(store, "ng", cloud_read.stored_files(store, "ng"), log=lambda m: None)
    assert result == {"measured": 2, "failed": 1}
    measured = json.loads(store.objects["work/ng/survey/aaa.json"])
    assert (measured["pages"], measured["scanned_pages"], measured["text_pages"]) == (3, [2, 3], [1])
    assert cloud_read.survey(store, "ng", ["sources/ng/nass/aaa.pdf"], log=lambda m: None)["measured"] == 0
    total = cloud_read.summary(store, "ng")
    assert (total["files"], total["pages"], total["scanned_pages"]) == (1, 3, 2)
    assert total["unreadable"] == ["sources/ng/sc/bbb.pdf"] and total["cost_usd"] == {
        "layout": 0.03,
        "ocr": 0.0,
    }
    store.objects["sources/ng/sc/bbb.pdf"] = pdf(2)  # failed before: measured again
    assert cloud_read.survey(store, "ng", ["sources/ng/sc/bbb.pdf"], log=lambda m: None)["measured"] == 1


def test_a_reader_keeps_earlier_pages_resumes_and_never_replaces_what_it_cannot_read(store):
    cloud_read.survey(store, "ng", ["sources/ng/nass/aaa.pdf"], log=lambda m: None)
    work = cloud_read.worklist(store, "ng", {"approved_by": "Owner", "max_pages": 10, "basis": "chat"})
    assert [(f["sha256"], f["pages"]) for f in work["files"]] == [("aaa", [1, 2, 3])] and work["pages"] == 3
    copy = {"aaa": {"copy_of": "bbb", "reason": "the Gazette copy is read"}}
    skipped = cloud_read.worklist(
        store, "ng", {"approved_by": "Owner", "max_pages": 10, "basis": "chat"}, copy
    )
    assert skipped["files"] == [] and skipped["not_read"] == [{"sha256": "aaa", "pages": 3, **copy["aaa"]}]
    store.objects["readings/ng/docai/aaa.json"] = json.dumps(
        {"pages": [{"page": 2, "nodes": ["kept"]}]}
    ).encode()
    result = cloud_read.read(store, "ng", "layout", work["files"], 10, log=lambda m: None, reader=Reader())
    assert (result["files"], result["pages"], result["stopped"]) == (1, 2, None)
    kept = json.loads(store.objects["readings/ng/docai/aaa.json"])
    assert [(p["page"], p["nodes"]) for p in kept["pages"]] == [(1, ["read"]), (2, ["kept"]), (3, ["read"])]
    assert kept["runs"][-1]["pages"] == [1, 3]
    again = cloud_read.read(store, "ng", "layout", work["files"], 10, log=lambda m: None, reader=Reader())
    assert again["pages"] == 0
    assert (
        cloud_read.read(store, "ng", "ocr", work["files"], 2, log=lambda m: None, reader=Reader())["stopped"]
        == "budget"
    )
    store.objects["readings/ng/ocr/aaa.json"] = None
    failed = cloud_read.read(store, "ng", "ocr", work["files"], 10, log=lambda m: None, reader=Reader())
    assert failed["failed"][0]["sha256"] == "aaa" and store.objects["readings/ng/ocr/aaa.json"] is None


def test_a_file_whose_text_layer_cannot_be_read_still_has_its_pages_counted(store, monkeypatch):
    def broken(path):
        raise RuntimeError("pdftotext failed")

    monkeypatch.setattr(cloud_read.gazette_layout, "text_layer", broken)
    cloud_read.survey(store, "ng", ["sources/ng/nass/aaa.pdf"], log=lambda m: None)
    measured = json.loads(store.objects["work/ng/survey/aaa.json"])
    assert (measured["pages"], measured["text_pages"]) == (3, []) and "pdftotext" in measured[
        "text_layer_error"
    ]


class Counting(Reader):
    """A reader that counts the pages it bills, failing once on the requests listed in `fail_on`
    (after billing them, as the service does when a later request of the file fails)."""

    def __init__(self, fail_on=()):
        self.billed, self.fail_on, self.calls = 0, list(fail_on), 0

    def read(self, part):
        self.calls += 1
        count = len(PdfReader(str(part)).pages)
        self.billed += count
        if self.calls in self.fail_on:
            self.fail_on.remove(self.calls)
            raise RuntimeError("RetryError: the deadline for retries passed")
        return super().read(part)


def big_file(store, pages=120):
    store.objects["sources/ng/nln/big.pdf"] = pdf(pages)
    return {"sha256": "big", "source_object": "sources/ng/nln/big.pdf", "pages": list(range(1, pages + 1))}


def test_a_file_is_saved_every_fifty_pages_and_one_that_fails_is_resumed_before_the_run_ends(store):
    item = big_file(store)
    reader = Counting(fail_on=[2])  # the second group of 50 fails once
    result = cloud_read.read(store, "ng", "layout", [item], 1000, log=lambda m: None, reader=reader)
    assert result["retried"] == ["big"] and result["failed"] == [] and result["pages"] == 120
    kept = json.loads(store.objects["readings/ng/docai/big.json"])
    assert [p["page"] for p in kept["pages"]] == list(range(1, 121))
    assert [len(r["pages"]) for r in kept["runs"]] == [50, 50, 20]  # the first group was kept, not read again
    assert result["billed_pages"] == 170  # the failed group was billed too, and counted


def test_the_budget_counts_billed_pages_also_of_a_file_that_failed(store):
    item = big_file(store)
    reader = Counting(fail_on=[1, 2])  # the first group fails, and again on the second pass
    result = cloud_read.read(store, "ng", "layout", [item], 60, log=lambda m: None, reader=reader)
    assert result["billed_pages"] == 50 and result["stopped"] == "budget" and result["pages"] == 0
    assert "readings/ng/docai/big.json" not in store.objects


def test_requests_are_paced_to_the_allowance_across_threads():
    from ingestion.layout_parser import Pace

    moment, slept = [0.0], []

    def sleep(seconds):
        slept.append(seconds)
        moment[0] += seconds

    pace = Pace(30, clock=lambda: moment[0], sleep=sleep)  # two seconds a page
    for _ in range(3):
        pace.wait(15)
    assert slept == [30.0, 30.0]  # the first goes at once; each next one 15 pages later


def test_a_run_records_its_billed_pages_cost_and_approval(store, monkeypatch):
    item = big_file(store, pages=10)
    store.objects["work/ng/reading.json"] = json.dumps(
        {"pages": 10, "approval": {"max_pages": 10, "basis": "Option 1"}, "files": [item]}
    ).encode()
    monkeypatch.setattr(cloud_read.corpus_store, "open_store", lambda: store)
    monkeypatch.setattr(cloud_read, "PACE", {})
    monkeypatch.setenv("SCAN_MAX_PAGES", "2500")
    monkeypatch.setenv("SCAN_APPROVAL", "Owner in chat, 2026-10-26: re-read the failed files")
    from ingestion import readers

    monkeypatch.setattr(
        readers, "DocAIReader", lambda: type("R", (Counting,), {"preflight": lambda self: None})()
    )
    assert cloud_read.run("layout", "ng", 0, 1, log=lambda m: None) == 0
    (name,) = [n for n in store.objects if n.startswith("work/ng/runs/layout/")]
    report = json.loads(store.objects[name])
    assert (report["billed_pages"], report["budget_pages"], report["cost_usd"]) == (10, 2500, 0.1)
    assert report["approval"].startswith("Owner in chat") and name.endswith("-task1-of-1.json")
