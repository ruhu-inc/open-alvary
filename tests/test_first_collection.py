"""First-collection acquisition tool: manifest safety, furniture removal and OCR fallback."""

import json

import pytest

from ingestion.first_collection import extract_pdf, load_manifest, remove_running_headers


def test_committed_manifest_is_allowlisted_and_unique():
    manifest = load_manifest()
    ids = [act["id"] for act in manifest["acts"]]
    assert len(ids) == len(set(ids)) == 15
    assert all(act["url"].startswith("https://placng.org/") for act in manifest["acts"])


@pytest.mark.parametrize(
    "url", ["http://placng.org/x.pdf", "https://evil.example/x.pdf", "https://placng.org:8443/x.pdf"]
)
def test_manifest_rejects_unapproved_urls(tmp_path, url):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"acts": [{"id": "ng-test-act", "title": "T", "year": 2023, "url": url}]}))
    with pytest.raises(ValueError):
        load_manifest(path)


def test_running_headers_are_removed_and_reported():
    pages = [f"A {100 + n} 2023 No. 8 Copyright Act, 2022\nSection {n} text." for n in range(6)]
    cleaned, removed = remove_running_headers(pages)
    assert all(page.startswith("Section") for page in cleaned)
    assert len(removed) == 6 and removed[0] == "A 100 2023 No. 8 Copyright Act, 2022"


def test_body_lines_are_never_treated_as_headers():
    pages = ["Preamble\n1. Short title.", "2. Interpretation.\nPreamble"]
    cleaned, removed = remove_running_headers(pages)
    assert cleaned == pages and removed == []


def blank_pdf(pages):
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_pages_without_text_use_ocr_and_are_recorded():
    raw = blank_pdf(2)
    result = extract_pdf(raw, ocr=lambda pages: {n: f"1. Section from page {n}." for n in pages})
    assert result["ocr_pages"] == [1, 2] and not result["needs_ocr"]
    assert [entry["section"] for entry in result["outline"]] == ["1", "1"]


def test_without_ocr_scanned_pages_are_flagged_not_guessed():
    result = extract_pdf(blank_pdf(1))
    assert result["needs_ocr"] and result["pages_without_text"] == [1] and result["characters"] == 0
