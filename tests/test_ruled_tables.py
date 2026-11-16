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


def test_a_table_on_a_turned_page_is_read_where_the_text_layer_places_its_words(tmp_path):
    """Consolidated findings item 48: a page with /Rotate gave its rules unturned and its words turned
    (pdftotext places a turned page's words as it is shown), so its tables came out empty."""
    rules = " ".join(
        ["20 179.75 120 0.5 re f", "20 159.75 120 0.5 re f", "20 139.75 120 0.5 re f",
         "19.75 140 0.5 40 re f", "79.75 140 0.5 40 re f", "139.75 140 0.5 40 re f"]
    )  # fmt: skip
    writer = PdfWriter()
    writer.append(PdfReader(drawn(rules, tmp_path).pdf.stream))
    writer.pages[0].rotate(90)
    path = tmp_path / "turned.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)
    page = PdfReader(path).pages[0]

    def shown(text, x, top):  # where pdftotext puts a word drawn at (x, top) on a page turned 90°
        return {
            "xmin": 200 - (top + 8),
            "ymin": x,
            "xmax": 200 - top,
            "ymax": x + 5 * len(text),
            "text": text,
        }

    words = [shown("Year", 30, 25), shown("Rate", 90, 25), shown("2024", 30, 45), shown("5%", 90, 45)]
    [table] = ruled_tables.page_tables(page, words, 200)
    assert sorted(w["text"] for w in table["words"]) == ["2024", "5%", "Rate", "Year"]


def test_a_scan_wrapped_in_a_form_object_counts_as_a_scan(tmp_path):
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NumberObject

    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    image = DecodedStreamObject()
    image.set_data(b"\\x00")
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                  NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8)})  # fmt: skip
    form = DecodedStreamObject()
    form.set_data(b"q 200 0 0 200 0 0 cm /Im1 Do Q")
    form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                 NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(200), FloatObject(200)]),
                 NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): DictionaryObject(
                     {NameObject("/Im1"): writer._add_object(image)})})})  # fmt: skip
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Fm1"): writer._add_object(form)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"/Fm1 Do")
    page[NameObject("/Contents")] = writer._add_object(content)
    path = tmp_path / "wrapped.pdf"
    with open(path, "wb") as handle:
        writer.write(handle)
    assert ruled_tables.image_coverage(PdfReader(path).pages[0]) == 1.0
    assert ruled_tables.image_pages(path) == {1}
