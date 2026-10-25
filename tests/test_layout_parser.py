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
    assert nodes == [
        {"kind": "list_item", "level": "list_item", "text": "(a) first", "page": 3, "ordered": False}
    ]


def test_a_line_break_returned_as_backslash_n_is_read_as_a_line_break():
    escaped = {"blocks": [text("paragraph", r"Taux\nannuel")], "row_span": 1, "col_span": 1}
    nodes = lp.convert_blocks(
        [
            text("heading-1", r"CHAPITRE IV\nDISPOSITIONS FINALES"),
            text("paragraph", r"les États\nmembres"),
            ordered_list(r"Article 32\nMesures"),
            {"table_block": {"body_rows": [{"cells": [escaped]}]}, "page_span": {"page_start": 1}},
        ]
    )
    assert [n.get("text") for n in nodes[:3]] == [
        "CHAPITRE IV\nDISPOSITIONS FINALES",
        "les États\nmembres",
        "Article 32\nMesures",
    ]
    assert nodes[3]["body_rows"][0][0]["text"] == "Taux\nannuel"
    # A heading's two lines; within a paragraph, words.
    assert lp.page_text(nodes, 1).split("\n")[:2] == ["CHAPITRE IV", "DISPOSITIONS FINALES"]
    assert "les États membres" in " ".join(lp.page_text(nodes, 1).split())
    # Not between words, the two characters are left as read.
    assert lp.convert_blocks([text("paragraph", r"voir \n")])[0]["text"] == r"voir \n"


def test_disagreements_mark_only_differing_candidate_words():
    differing, missing, agreement = lp.disagreements(
        "the Counci! may make rules", "the Council may make rules"
    )
    assert differing == {1} and missing == {}
    assert 0.7 < agreement < 1


def test_words_only_in_the_reference_are_shown_struck_out():
    reference, candidate = "91. Establishment of Fund. 92. Expenditure", "Establishment of Fund. Expenditure"
    differing, missing, _ = lp.disagreements(reference, candidate)
    assert differing == set() and missing == {0: "91.", 3: "92."}
    nodes = lp.convert_blocks([text("paragraph", candidate)])
    rendered = lp.render_page(nodes, 1, differing, missing)
    assert rendered.index("<del title='not in this reading'>91.</del>") < rendered.index("Establishment")
    assert "92.</del> Expenditure" in rendered
    trailing = lp.render_page(nodes, 1, set(), {5: "Citation."})
    assert trailing.endswith("<p><del title='not in this reading'>Citation.</del></p>")


def ordered_list(*entries, page=1):
    return {
        "list_block": {
            "type_": "ordered",
            "list_entries": [{"blocks": [text("paragraph", e)]} for e in entries],
        },
        "page_span": {"page_start": page},
    }


def test_dropped_list_markers_are_restored_from_the_first_reading_only_when_present():
    nodes = lp.convert_blocks(
        [
            ordered_list(
                "Establishment of Fund for the Commission.",
                "Expenditure of the Commission.",
                "Annual report.",
            )
        ]
    )
    reference = "PART XI\n91. Establishment of Fund for the Commission.\nExpenditure of the Commission.\n94. Annual report."
    assert lp.restore_markers(nodes, 1, reference) == 2
    assert [n.get("marker") for n in nodes] == ["91.", None, "94."]
    assert nodes[0]["marker_source"] == "first reading" and nodes[0]["ordered"]
    assert lp.page_text(nodes, 1).splitlines()[0] == "91. Establishment of Fund for the Commission."
    assert "<span class='restored'" in lp.render_page(nodes, 1)


def test_newer_header_and_footer_types_are_furniture():
    nodes = lp.convert_blocks(
        [text("header", "A 182 2023 No. 8 Copyright Act, 2022"), text("footer", "A 183")]
    )
    assert [n["kind"] for n in nodes] == ["furniture", "furniture"]


def test_table_caption_is_part_of_the_page_text():
    nodes = lp.convert_blocks([{**TABLE, "table_block": {**TABLE["table_block"], "caption": "SCHEDULE"}}])
    assert lp.page_text(nodes, 2).startswith("SCHEDULE\n")


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


def test_page_chunks_halve_a_chunk_over_the_byte_limit():
    writer = PdfWriter()
    for _ in range(15):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    four_pages = len(lp.page_chunks(buffer.getvalue(), size=4)[0][1])
    chunks = lp.page_chunks(buffer.getvalue(), max_bytes=four_pages)
    assert [len(PdfReader(BytesIO(raw)).pages) for _, raw in chunks] == [3, 4, 4, 4]
    assert [first for first, _ in chunks] == [1, 4, 8, 12]
    assert all(len(raw) <= four_pages for _, raw in chunks)
    single = lp.page_chunks(buffer.getvalue(), size=2, max_bytes=1)  # a page over the limit is still sent
    assert [first for first, _ in single] == list(range(1, 16))


def test_a_chunk_of_late_pages_carries_only_the_pictures_its_pages_draw():
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    drawn = {}
    for n in range(6):  # each page's resources hold every earlier page's picture, as some producers write
        form = DecodedStreamObject()
        form.set_data(b"0 0 m %" + bytes([65 + n]) * 50_000)
        form.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject([NumberObject(0)] * 4),
                NameObject("/Resources"): DictionaryObject(),
            }
        )
        drawn[f"/Fm{n}"] = writer._add_object(form)
        page = writer.add_blank_page(width=72, height=72)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject(k): v for k, v in drawn.items()})}
        )
        content = DecodedStreamObject()
        content.set_data(f"q /Fm{n} Do Q".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    buffer = BytesIO()
    writer.write(buffer)
    chunks = lp.page_chunks(buffer.getvalue(), size=3, max_bytes=200_000)
    assert [first for first, _ in chunks] == [1, 4]
    late = PdfReader(BytesIO(chunks[1][1]))
    assert [list(p["/Resources"]["/XObject"]) for p in late.pages] == [["/Fm3"], ["/Fm4"], ["/Fm5"]]
    assert len(chunks[1][1]) < 200_000  # three pictures, not all six


def test_config_requires_environment_and_validates(monkeypatch, tmp_path):
    monkeypatch.setattr("ingestion.settings.ENV_FILE", tmp_path / "absent.env")
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

    def process_document(self, request, **options):
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


def test_an_ocr_error_in_the_reference_never_shifts_a_marker_onto_the_wrong_entry():
    nodes = lp.convert_blocks(
        [
            ordered_list(
                "Hydroelectric Power Producing Areas Act, No.7, 2010.",
                "Hydroelectric Power Producing Areas Act, 2010.",
                "Electric Power Sector Reform Act.",
            )
        ]
    )
    reference = (
        "1. Hydroelectric Power Prod.cing Areas Act, No.7, 2010.\n"
        "2. Hydroelectric Power Producing Areas Act, 2010.\n"
        "5. Electric Power Sector Reform Act."
    )
    lp.restore_markers(nodes, 1, reference)
    assert [n.get("marker") for n in nodes] == ["1.", "2.", "5."]


def test_nested_paragraphs_of_an_amending_section_stay_separate_and_get_their_markers():
    nested = ordered_list(
        "in subsection (2) by substituting the word", "in subsection (4) by inserting the words"
    )
    section = {
        "list_block": {
            "type_": "ordered",
            "list_entries": [
                {
                    "blocks": [
                        text("paragraph", "Amendment of section 17"),
                        text("paragraph", "Section 17 is amended —"),
                        nested,
                    ]
                }
            ],
        },
        "page_span": {"page_start": 1},
    }
    nodes = lp.convert_blocks([section])
    assert [n["text"] for n in nodes] == [
        "Amendment of section 17\nSection 17 is amended —",
        "in subsection (2) by substituting the word",
        "in subsection (4) by inserting the words",
    ]
    reference = "2. Section 17 is amended — Amendment of section 17\n(a) in subsection (2) by substituting the word\n(b) in subsection (4) by inserting the words"
    lp.restore_markers(nodes, 1, reference)
    assert [n.get("marker") for n in nodes] == ["2.", "(a)", "(b)"]  # "2." sits behind the margin note


def test_a_marker_behind_a_leading_margin_note_is_restored_but_not_past_matched_words():
    nodes = lp.convert_blocks(
        [ordered_list("Amendment of section 17 Section 17 of the Principal Act is amended —")]
    )
    lp.restore_markers(nodes, 1, "2. Section 17 of the Principal Act is amended — Amendment of section 17")
    assert nodes[0]["marker"] == "2."
    other = lp.convert_blocks([ordered_list("The Minister may by order amend the Schedule")])
    lp.restore_markers(other, 1, "The Minister may 4. by order amend the Schedule")
    assert "marker" not in other[0]


def test_a_section_number_in_place_of_a_margin_note_heading_is_restored_with_its_subsection():
    nodes = lp.convert_blocks(
        [
            text("heading-2", "Power to accept gifts"),
            ordered_list("The Agency may accept gifts of land", "The Agency shall not accept any gift"),
        ]
    )
    reference = "156. (1) The Agency may accept gifts of land (2) The Agency shall not accept any gift"
    lp.restore_markers(nodes, 1, reference)
    assert [n.get("marker") for n in nodes] == [None, "156. (1)", "(2)"]


def test_a_number_after_a_misread_last_word_or_before_a_split_first_word_is_restored():
    nodes = lp.convert_blocks(
        [ordered_list("Pension and gratuity", "Structure of the Agency", "Feed-in tariffs rates")]
    )
    reference = "139. Pension and gratu-ry 140. Structure of the Agency 168. Feed -in tariffs rates"
    lp.restore_markers(nodes, 1, reference)
    assert [n.get("marker") for n in nodes] == ["139.", "140.", "168."]


def test_an_ocr_page_gives_every_word_with_its_box_and_confidence():
    from types import SimpleNamespace as NS

    text = "Article premier.\na) cafés"

    def token(start, end, x0, y0, x1, y1, confidence=0.98):
        vertices = [NS(x=x0, y=y0), NS(x=x1, y=y0), NS(x=x1, y=y1), NS(x=x0, y=y1)]
        layout = NS(
            text_anchor=NS(text_segments=[NS(start_index=start, end_index=end)]),
            bounding_poly=NS(normalized_vertices=vertices),
            confidence=confidence,
        )
        return NS(layout=layout)

    page = NS(
        tokens=[
            token(0, 8, 0.1, 0.1, 0.2, 0.12),
            token(8, 17, 0.21, 0.1, 0.3, 0.12),
            token(17, 17, 0, 0, 0, 0),
            token(17, 18, 0.1, 0.13, 0.11, 0.15, 0.9),  # "a", no space after it
            token(18, 20, 0.11, 0.13, 0.12, 0.15),  # ") "
            token(20, 25, 0.13, 0.13, 0.2, 0.15),
        ],
        dimension=NS(width=1654.0, height=2339.0),
        detected_languages=[NS(language_code="fr", confidence=0.97)],
    )
    read = lp.ocr_page(page, text, 4)
    assert read["page"] == 4 and read["width"] == 1654.0
    # An empty token is dropped; "a" and ")" with no space between are one printed word.
    assert [w["text"] for w in read["words"]] == ["Article", "premier.", "a)", "cafés"]
    assert read["words"][2] == {
        "text": "a)",
        "x0": 0.1,
        "y0": 0.13,
        "x1": 0.12,
        "y1": 0.15,
        "confidence": 0.9,
    }
    assert read["words"][1] == {
        "text": "premier.",
        "x0": 0.21,
        "y0": 0.1,
        "x1": 0.3,
        "y1": 0.12,
        "confidence": 0.98,
    }
    assert read["languages"] == [{"code": "fr", "confidence": 0.97}]
