from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from ingestion import layout_parser as lp


def text(kind, value, page=1, children=()):
    return {
        "text_block": {"text": value, "type_": kind, "blocks": list(children)},
        "page_span": {"page_start": page, "page_end": page},
    }


def cell(value, rows=1, cols=1):
    return {"blocks": [text("paragraph", value)], "row_span": rows, "col_span": cols}


TABLE = {
    "table_block": {
        "caption": "",
        "header_rows": [{"cells": [cell("Column 1"), cell("Column 2", cols=2)]}],
        "body_rows": [{"cells": [cell("Offence"), cell("Fine"), cell("N5,000")]}],
    },
    "page_span": {"page_start": 2, "page_end": 2},
}


def test_converts_headings_tables_and_absolute_pages():
    blocks = [
        text("page_header", "Copyright Act, 2022 A 123", page=1),
        text("heading-1", "PART I", page=1, children=[text("paragraph", "1. The objectives are", page=1)]),
        TABLE,
        text("page_footer", "A 124", page=2),
    ]
    nodes = lp.convert_blocks(blocks, offset=16)
    assert [n["kind"] for n in nodes] == ["furniture", "heading", "paragraph", "table", "furniture"]
    assert nodes[1]["page"] == 16 and nodes[3]["page"] == 17
    table = nodes[3]
    assert table["header_rows"][0][1] == {"text": "Column 2", "row_span": 1, "col_span": 2}
    assert [c["text"] for c in table["body_rows"][0]] == ["Offence", "Fine", "N5,000"]


def test_page_text_excludes_furniture_and_reads_tables_by_row():
    nodes = lp.convert_blocks(
        [text("page_header", "Gazette header"), TABLE, text("paragraph", "After", page=2)]
    )
    assert lp.page_text(nodes, 1) == ""
    assert lp.page_text(nodes, 2) == "Column 1 Column 2\nOffence Fine N5,000\nAfter"


def test_list_blocks_become_items():
    nodes = lp.convert_blocks(
        [
            {
                "list_block": {"list_entries": [{"blocks": [text("paragraph", "(a) first")]}]},
                "page_span": {"page_start": 3},
            }
        ]
    )
    assert nodes == [{"kind": "list_item", "level": "list_item", "text": "(a) first", "page": 3}]


def test_disagreements_mark_only_differing_candidate_words():
    differing, agreement = lp.disagreements("the Counci! may make rules", "the Council may make rules")
    assert differing == {1}
    assert 0.7 < agreement < 1


def test_render_marks_line_up_with_page_text_and_escape():
    nodes = lp.convert_blocks([text("paragraph", "a <b> c"), {**TABLE, "page_span": {"page_start": 1}}])
    words = lp.page_text(nodes, 1).split()
    html = lp.render_page(nodes, 1, {1, words.index("Fine")})
    assert "<mark>&lt;b&gt;</mark>" in html and "<mark>Fine</mark>" in html
    assert "<th colspan=2>" in html and "<script" not in html


def test_page_chunks_respect_online_limit():
    writer = PdfWriter()
    for _ in range(31):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    chunks = lp.page_chunks(buffer.getvalue())
    assert [first for first, _ in chunks] == [1, 16, 31]
    assert [len(PdfReader(BytesIO(raw)).pages) for _, raw in chunks] == [15, 15, 1]


def test_config_requires_environment_and_validates(monkeypatch):
    for key in ("ALVARY_DOCAI_PROJECT", "ALVARY_DOCAI_LOCATION", "ALVARY_DOCAI_PROCESSOR_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="Configure"):
        lp.DocAIConfig.from_environment()
    with pytest.raises(ValueError):
        lp.DocAIConfig("open-alvary", "asia", "abc123def456")
    assert lp.DocAIConfig("open-alvary", "eu", "abc123def456").location == "eu"


class FakeClient:
    def __init__(self):
        self.requests = []

    def processor_path(self, project, location, processor):
        return f"projects/{project}/locations/{location}/processors/{processor}"

    def process_document(self, request):
        from google.cloud import documentai_v1 as d

        self.requests.append(request)
        block = d.Document.DocumentLayout.DocumentLayoutBlock(
            text_block=d.Document.DocumentLayout.DocumentLayoutBlock.LayoutTextBlock(
                text="Body", type_="paragraph"
            ),
            page_span=d.Document.DocumentLayout.DocumentLayoutBlock.LayoutPageSpan(page_start=2, page_end=2),
        )
        return d.ProcessResponse(
            document=d.Document(document_layout=d.Document.DocumentLayout(blocks=[block]))
        )


def test_parse_offsets_each_chunk_and_never_requests_annotations():
    pytest.importorskip("google.cloud.documentai_v1")
    writer = PdfWriter()
    for _ in range(20):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    client = FakeClient()
    result = lp.parse(buffer.getvalue(), lp.DocAIConfig("open-alvary", "us", "abc123def456"), client=client)
    assert [n["page"] for n in result["nodes"]] == [2, 17]
    for request in client.requests:
        config = request.process_options.layout_config
        assert not config.enable_table_annotation and not config.enable_image_annotation
