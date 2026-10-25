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
