"""Tables rebuilt from the ruling lines a PDF draws and the words of its text layer."""

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from ingestion import ruled_tables


def drawn(content: str, tmp_path):
    """A one-page PDF (200 x 200 pt) whose content stream is `content`."""
    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    stream = DecodedStreamObject()
    stream.set_data(content.encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "ruled.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)
    return PdfReader(path).pages[0]


def word(text, x, y):
    return {"xmin": x, "ymin": y, "xmax": x + 5 * len(text), "ymax": y + 8, "text": text}


def test_a_ruled_table_keeps_its_merged_heading_and_its_words_as_printed(tmp_path):
    # Three rows, two columns (x 20-80-140, y 20-40-60-80 from the top); the heading row has no
    # border between its columns. A clipping rectangle ("re W n") is not a rule.
    rules = " ".join(
        [
            "0 0 200 200 re W n",
            "20 179.75 120 0.5 re f",  # y 20
            "20 159.75 120 0.5 re f",  # y 40
            "20 139.75 120 0.5 re f",  # y 60
            "20 119.75 120 0.5 re f",  # y 80
            "19.75 120 0.5 60 re f",  # x 20, full height
            "139.75 120 0.5 60 re f",  # x 140, full height
            "79.75 120 0.5 40 re f",  # x 80, only below the heading
        ]
    )
    page = drawn(rules, tmp_path)
    found = ruled_tables.rules(page)
    assert len(found) == 7
    words = [
        word("Annual", 30, 25),
        word("income", 70, 25),
        word("From", 25, 45),
        word("0%", 90, 45),
        word("360,001", 25, 65),
        word("20%", 90, 65),
        word("outside", 150, 150),
    ]
    tables = ruled_tables.page_tables(page, words, 200)
    assert len(tables) == 1
    table = tables[0]
    assert table["header_rows"] == [[{"text": "Annual income", "row_span": 1, "col_span": 2}]]
    assert [[c["text"] for c in row] for row in table["body_rows"]] == [["From", "0%"], ["360,001", "20%"]]
    assert "outside" not in [w["text"] for w in table["words"]]


def test_the_frame_of_the_language_columns_is_not_a_table(tmp_path):
    frame = " ".join(
        [
            "5 10 190 0.5 re f",
            "5 190 190 0.5 re f",
            "5 10 0.5 180 re f",
            "70 10 0.5 180 re f",
            "130 10 0.5 180 re f",
            "195 10 0.5 180 re f",
        ]
    )
    page = drawn(frame, tmp_path)
    words = [word("Ingingo", 10, 20), word("Article", 75, 20), word("Article", 135, 20)]
    assert ruled_tables.page_tables(page, words, 200, language_columns=3) == []
