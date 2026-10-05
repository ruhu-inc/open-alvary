from io import BytesIO

import pytest
from pypdf import PdfWriter

from ingestion.pipeline import ManualText, OCRRequired, OfficialDocument, ingest, normalize, persist_ingestion


def test_unicode_and_offsets(catalogue):
    version, nodes, eligible = ingest(
        catalogue.sources[0], catalogue.rights[0], ManualText("  Cafe\u0301  law\r\nText  ")
    )
    assert version.canonical_text == "Café law\nText"
    assert nodes[0].end_byte == len(version.canonical_text.encode())
    assert not eligible


def test_html_removes_script_and_navigation():
    parser = OfficialDocument(set(), "text/html")
    assert (
        normalize(parser.parse(b"<nav>nav</nav><script>bad</script><main><h1>Title</h1><p>Test.</p></main>"))
        == "Title\nTest."
    )


def test_scanned_pdf_fails_without_ocr_and_calls_provider_per_page():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    parser = OfficialDocument(set(), "application/pdf")
    with pytest.raises(OCRRequired, match="Page 1"):
        parser.parse(buffer.getvalue())
    calls = []

    def ocr(raw, page):
        calls.append(page)
        return f"Synthetic OCR page {page}"

    parser.ocr = ocr
    assert "page 2" in parser.parse(buffer.getvalue())
    assert calls == [1, 2]


def test_empty_input_rejected(catalogue):
    with pytest.raises(ValueError, match="empty"):
        ingest(catalogue.sources[0], catalogue.rights[0], ManualText("  "))


def test_restricted_source_never_fetched(catalogue):
    catalogue.rights[0].status = "RESTRICTED"

    class NeverFetch:
        def fetch(self, source):
            pytest.fail("restricted source fetched")

    with pytest.raises(ValueError, match="Restricted"):
        ingest(catalogue.sources[0], catalogue.rights[0], NeverFetch())


def test_url_allowlist_is_mandatory(catalogue):
    with pytest.raises(ValueError, match="approved"):
        OfficialDocument(set(), "text/html").fetch(catalogue.sources[0])


def test_ingestion_is_idempotent(catalogue, store):
    store.save(catalogue)
    first, _ = persist_ingestion(store, catalogue.sources[0].id, ManualText("Synthetic text."))
    second, _ = persist_ingestion(store, catalogue.sources[0].id, ManualText("Synthetic text."))
    assert first.id == second.id
    assert first.retrieved_at == second.retrieved_at
    assert len(store.load().versions) == 1
