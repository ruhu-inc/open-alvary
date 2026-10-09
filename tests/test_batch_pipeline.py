import json
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from ingestion import batch, compare, readers, reconcile


def blank_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    path.write_bytes(buffer.getvalue())
    return path


# --- comparison --------------------------------------------------------------------------------


def test_words_both_checks_reject_are_flagged_and_numbers_are_high():
    flags = compare.compare(
        "Section 21 (1) is amended within 72 hours",
        {
            "mineru": "Section 21 (1) is amended within 7 days",
            "azure": "Section 21 (1) is amended within 7 days",
        },
    )
    assert [(f.severity, f.kind, f.primary) for f in flags] == [("high", "differs", "72 hours")]
    assert flags[0].checks == {"mineru": "7 days", "azure": "7 days"}


def test_split_checks_lower_the_severity_and_name_the_dissenter():
    flags = compare.compare(
        "the word genuineness", {"mineru": "the word genuineness", "azure": "the word genuiness"}
    )
    assert [(f.severity, f.readers_disagreeing) for f in flags] == [("low", ["azure"])]


def test_typography_only_is_low_and_reconstruction_is_high():
    low = compare.compare("Nigeria — the", {"mineru": "Nigeria - the", "azure": "Nigeria – the"})
    assert {f.severity for f in low} == {"low"}
    reconstructed = compare.compare(
        "SCHEDULE TO THE BILL I certify that",
        {"mineru": "SCHEDULE TO THE BILL", "azure": "SCHEDULE TO THE BILL"},
    )
    assert [(f.severity, f.kind, f.primary) for f in reconstructed] == [
        ("high", "only_in_primary", "I certify that")
    ]


def test_words_every_check_has_but_the_primary_lacks_are_reported():
    flags = compare.compare(
        "Section of the Act", {"mineru": "Section of 44 of the Act", "azure": "Section of 44 of the Act"}
    )
    assert [(f.kind, f.severity) for f in flags] == [("missing_from_primary", "high")]
    assert all("44" in text for text in flags[0].checks.values())


# --- converters --------------------------------------------------------------------------------


def test_mineru_page_keeps_tables_lists_and_handwriting_and_sets_furniture_aside():
    blocks = [
        {"type": "header", "content": [{"type": "text", "content": "A 123"}]},
        {"type": "doc_title", "content": [{"type": "text", "content": "SCHEDULE"}]},
        {
            "type": "list",
            "content": [{"type": "text", "content": [{"type": "text", "content": "(a) first"}]}],
        },
        {
            "type": "table",
            "content": [
                {"type": "table_caption", "content": [{"type": "text", "content": "Table 1"}]},
                {"type": "table_body", "content": "<table><tr><td>A</td><td>B</td></tr></table>"},
            ],
        },
        {
            "type": "image",
            "sub_type": "text_image",
            "content": [{"type": "image_body", "content": "27th Day of Feb."}],
        },
        {"type": "chart", "content": [{"type": "chart_body", "content": "ignored"}]},
    ]
    page = readers.mineru_page(blocks)
    assert page["text"].splitlines() == ["SCHEDULE", "(a) first", "Table 1", "A B", "27th Day of Feb."]
    assert page["tables"] == [[["A", "B"]]] and page["furniture"] == ["A 123"]


def test_azure_pages_group_paragraphs_and_tables_by_page():
    result = {
        "paragraphs": [
            {"content": "A 302", "role": "pageHeader", "boundingRegions": [{"pageNumber": 1}]},
            {"content": "1. Short title", "boundingRegions": [{"pageNumber": 1}]},
            {"content": "Schedule", "boundingRegions": [{"pageNumber": 2}]},
        ],
        "tables": [
            {
                "rowCount": 1,
                "columnCount": 2,
                "boundingRegions": [{"pageNumber": 2}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "content": "X"},
                    {"rowIndex": 0, "columnIndex": 1, "content": "Y"},
                ],
            }
        ],
    }
    pages = readers.azure_pages(result, 2)
    assert pages[0] == {"page": 1, "text": "1. Short title", "tables": [], "furniture": ["A 302"]}
    assert pages[1]["tables"] == [[["X", "Y"]]]


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeAzure:
    def __init__(self):
        self.calls = []
        self.responses = [
            FakeResponse(429, headers={"Retry-After": "1"}),
            FakeResponse(202, headers={"Operation-Location": "https://x/op"}),
            FakeResponse(200, {"status": "running"}, {"Retry-After": "1"}),
            FakeResponse(503),
            FakeResponse(
                200,
                {"status": "succeeded", "analyzeResult": {"modelId": "prebuilt-layout", "paragraphs": []}},
            ),
        ]

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append((method, url))
        return self.responses.pop(0)


def test_azure_reader_retries_throttling_and_polls_until_done(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_DI_ENDPOINT", "https://example.cognitiveservices.azure.com/")
    monkeypatch.setenv("AZURE_DI_KEY", "test")
    slept = []
    reader = readers.AzureReader(client=FakeAzure(), sleep=slept.append)
    reading = reader.read(blank_pdf(tmp_path / "a.pdf", 2))
    assert [p["page"] for p in reading.pages] == [1, 2] and reading.billed_pages == 2
    assert reading.raw["model_id"] == "prebuilt-layout"
    assert reading.version == reader.version()  # the stored version is the cache key
    assert slept == [1.0, 1.0, 2.0]  # Retry-After, Retry-After, then backoff for the 503


# --- reconciliation -----------------------------------------------------------------------------


def node(text, kind="list_item", page=1):
    return {"kind": kind, "level": kind, "text": text, "page": page, "ordered": True}


def test_markers_are_restored_only_when_the_checks_agree():
    primary = {
        "page": 1,
        "nodes": [node("Section 17 is amended"), node("Section 21 is amended")],
    }
    checks = {
        "mineru": {"text": "2. Section 17 is amended\n3. Section 21 is amended"},
        "azure": {"text": "2. Section 17 is amended\n8. Section 21 is amended"},
    }
    page = reconcile.reconcile_page(primary, checks)
    assert page["nodes"][0]["marker"] == "2." and page["nodes"][0]["marker_source"] == ["azure", "mineru"]
    assert "marker" not in page["nodes"][1]
    assert [f["kind"] for f in page["flags"] if f["severity"] == "high"][0] == "marker_conflict"


# --- batch runner -------------------------------------------------------------------------------


class FakeReader:
    def __init__(self, name, billed, fail=False, log=None):
        self.name, self.billed, self.fail, self.log = name, billed, fail, log if log is not None else []

    def version(self):
        return {"fake": self.name}

    def read(self, pdf):
        self.log.append(self.name)
        if self.fail:
            raise RuntimeError("service down")
        pages = [{"page": 1, "text": "1. Short title", "tables": [], "furniture": []}]
        if self.name == "docai":
            pages[0]["nodes"] = [node("Short title")]
        return readers.Reading(self.name, self.version(), pages, 1 if self.billed else 0)


def make_batch(tmp_path, fail=()):
    log = []
    factories = {
        name: (lambda name=name, billed=billed: FakeReader(name, billed, name in fail, log))
        for name, billed in (("docai", True), ("mineru", False), ("azure", True))
    }
    blank_pdf(tmp_path / "ng-act.pdf", 1)
    return batch.Batch(tmp_path, ["docai", "mineru", "azure"], factories), log


def test_plan_counts_only_uncached_billed_pages_and_runs_resume_from_cache(tmp_path):
    runner, log = make_batch(tmp_path)
    act = {"id": "ng-act"}
    assert sum(row["billed"] for row in runner.plan([act])) == 2
    outcome = runner.run_act(act)
    assert outcome["complete"] and outcome["summary"]["restored_markers"] == 1
    reading = json.loads((tmp_path / "ng-act.reading.json").read_text())
    assert reading["pages"][0]["nodes"][0]["marker"] == "1."
    assert sum(row["billed"] for row in runner.plan([act])) == 0
    runner.run_act(act)
    assert sorted(log) == ["azure", "docai", "mineru"]  # second run read nothing again


def test_one_failing_reader_is_isolated_and_recorded(tmp_path):
    runner, _ = make_batch(tmp_path, fail={"azure"})
    outcome = runner.run_act({"id": "ng-act"})
    assert (
        outcome["readers"]["azure"]["status"] == "failed"
        and "service down" in outcome["readers"]["azure"]["error"]
    )
    assert outcome["readers"]["docai"]["status"] == "read" and not outcome["complete"]
    assert not (tmp_path / "readings" / "ng-act" / "azure.json").exists()
    assert json.loads((tmp_path / "ng-act.reading.json").read_text())["summary"]["missing_checks"] == [
        "azure"
    ]


def test_a_changed_pdf_or_reader_version_invalidates_the_cache(tmp_path):
    runner, log = make_batch(tmp_path)
    runner.run_act({"id": "ng-act"})
    blank_pdf(tmp_path / "ng-act.pdf", 2)
    assert all(not row["cached"] for row in runner.plan([{"id": "ng-act"}]))


def test_mineru_output_is_kept_and_a_page_without_blocks_is_empty(tmp_path, monkeypatch):
    kit = tmp_path / "mineru-kit"
    kit.write_text("#!/bin/sh\n")
    kit.chmod(0o755)
    reader = readers.MinerUReader(
        kit=kit, models=tmp_path, hashes=tmp_path / "readings" / ".model-hashes.json"
    )
    monkeypatch.setattr(reader, "version", lambda: {"tool": "test"})
    runs = []

    def fake_run(command, **kwargs):
        runs.append(command)
        out = Path(command[command.index("-o") + 1])
        out.write_text(
            json.dumps(
                {
                    "pages": [
                        {"page_idx": 0, "blocks": [{"type": "text", "content": "Hello"}]},
                        {"page_idx": 1},
                    ]
                }
            )
        )
        return type("Done", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(readers.subprocess, "run", fake_run)
    pdf = blank_pdf(tmp_path / "a.pdf", 2)
    first = reader.read(pdf)
    assert [p["text"] for p in first.pages] == ["Hello", ""]
    reader.read(pdf)
    assert len(runs) == 1  # the kept output is reused, not re-read
