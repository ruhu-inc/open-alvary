"""DIG-01: a PDF's own text layer is classed page by page and checked against the readings before it is
evidence for the native route; the evidence is bound to the PDF and to the reading it assessed."""

import hashlib
import json
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from ingestion import native

BODY = [
    "3. (1) The Board shall keep proper accounts of its income and expenditure",
    "and shall not lend money to any person except with the approval of the Minister.",
    "(2) The accounts shall be audited within 90 days of the end of each financial year.",
]
TEXT = "\n".join(BODY)


def _escaped(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text(lines, mode=0) -> str:
    shown = " ".join(f"({_escaped(line)}) Tj T*" for line in lines)
    return f"BT {mode} Tr /F1 10 Tf 14 TL 40 760 Td {shown} ET"


def pdf(*pages) -> bytes:
    """A PDF of 612 x 792 pt pages, each drawing visible `lines`, invisible `hidden` lines (render mode
    3, as OCR software lays text over a scan), and a page-sized `picture` drawn "under" the text
    (before it) or "over" it (after it)."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    image = DecodedStreamObject()
    image.set_data(b"\x80")
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    picture_ref = writer._add_object(image)
    for spec in pages:
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
                NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): picture_ref}),
            }
        )
        picture = "q 612 0 0 792 0 0 cm /Im1 Do Q"
        parts = [picture] if spec.get("picture") == "under" else []
        parts += [_text(spec["lines"])] if spec.get("lines") else []
        parts += [_text(spec["hidden"], mode=3)] if spec.get("hidden") else []
        parts += [picture] if spec.get("picture") == "over" else []
        stream = DecodedStreamObject()
        stream.set_data(" ".join(parts).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def page(number, text=TEXT, checks=None, flags=(), furniture=(), notes=None):
    nodes = [{"kind": "paragraph", "text": line, "page": number} for line in text.split("\n")]
    if notes:
        nodes[0]["notes"] = list(notes)
        text = "\n".join([nodes[0]["text"], *notes, *text.split("\n")[1:]])
    return {
        "page": number, "nodes": nodes, "text": text, "tables": [], "furniture": list(furniture),
        "flags": list(flags), "checks": {"mineru": text, "azure": text} if checks is None else checks,
    }  # fmt: skip


def reading(raw, *pages, primary="docai"):
    return {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "primary": {"reader": primary, "version": {"version": "test"}},
        "checks": {"mineru": {}, "azure": {}},
        "summary": {"pages": len(pages), "flags": {}, "restored_markers": 0, "missing_checks": []},
        "pages": list(pages),
    }


def flag(start, end, primary, checks, kind="differs", severity="medium"):
    return {"severity": severity, "kind": kind, "primary": primary, "checks": checks, "tokens": [start, end],
            "readers_disagreeing": sorted(checks)}  # fmt: skip


def one(lines=BODY, **spec):
    """The evidence for page 1 of a one-page PDF drawing `lines`, against a reading of TEXT."""
    reading_page = spec.pop("reading_page", None) or page(1)
    raw = pdf({"lines": lines, **spec})
    primary = spec.pop("primary", "docai")
    return native.assess(raw, reading(raw, reading_page, primary=primary))["pages"]["1"]


def test_a_clean_text_layer_that_agrees_with_every_reading_passes():
    found = one()
    assert found["class"] == "native" and found["passed"] and found["profile"] == native.PROFILE
    assert found["agreement"] == 1.0 and found["numbers_agree"] and found["negations_agree"]
    assert found["readers"]["docai"]["agreement"] == found["readers"]["mineru"]["agreement"] == 1.0
    assert found["flags"] == {"supported": [], "unsupported": [], "unflagged_differences": 0}


def test_each_kind_of_page_is_classed_with_its_reason():
    raw = pdf(
        {},  # nothing drawn
        {"hidden": BODY, "picture": "under"},  # a scan with someone else's OCR laid over it
        {"lines": BODY, "picture": "over"},  # OCR text hidden under a page-sized picture
        {"lines": BODY, "hidden": BODY},  # its own text and a hidden OCR copy
        {"lines": BODY[:2]},  # the rest of the page is a picture the layer does not hold
        {"lines": BODY + BODY},  # the layer holds the page's words twice
        {"lines": BODY, "picture": "under"},  # a background picture behind real text
    )
    evidence = native.assess(raw, reading(raw, *(page(n) for n in range(1, 8))))
    classes = [(p["class"], p["reasons"][0]) for p in evidence["pages"].values()]
    assert classes[0][0] == "scan" and "no text layer" in classes[0][1]
    assert classes[1][0] == "scan" and "hidden OCR" in classes[1][1]
    assert classes[2][0] == "scan" and "drawn over the text layer" in classes[2][1]
    assert classes[3][0] == "mixed" and "hidden OCR layer" in classes[3][1]
    assert classes[4][0] == "mixed" and "part of the page is a picture" in classes[4][1]
    assert classes[5][0] == "mixed" and "repeats itself" in classes[5][1]
    assert classes[6][0] == "native" and evidence["pages"]["7"]["passed"]
    assert not any(p["passed"] for p in list(evidence["pages"].values())[:6])
    assert evidence["thresholds"]["agreement_min"] == native.AGREEMENT_MIN


@pytest.mark.parametrize(
    "garbled",
    [
        "The Bo�rd shall keep proper accounts",  # a replacement character
        "The Bord shall keep proper accounts",  # a private-use glyph inside a word
        "The Board shall keep (cid:12) accounts",  # an unmapped glyph code
        "The Board shall keep \x05 accounts",  # a control character
        "The Boardâ€™s accounts shall be kept",  # mojibake
    ],
)
def test_missing_glyphs_make_a_layer_corrupt(garbled):
    found = native.assess_page({"text": garbled + "\n" + TEXT, "visible": 999}, page(1), "docai")
    assert found["class"] == "corrupt" and "missing glyphs" in found["reasons"][0] and not found["passed"]


def test_a_symbol_standing_alone_is_not_a_missing_glyph_but_a_layer_spelling_nothing_is_corrupt():
    bullet = native.assess_page({"text": " " + TEXT, "visible": 999}, page(1), "docai")
    assert bullet["class"] == "native" and bullet["glyphs"]["symbols"] == 1
    gibberish = native.assess_page({"text": "Xq zvw kpl " * 12, "visible": 999}, page(1), "docai")
    assert gibberish["class"] == "corrupt" and "does not spell" in gibberish["reasons"][0]


def test_numbers_and_negation_words_must_agree_exactly():
    days = one([BODY[0], BODY[1], BODY[2].replace("90", "60")])
    assert not days["numbers_agree"] and not days["passed"]
    assert "the numbers differ: the text layer has 60 where the units have 90" in days["reasons"]
    lend = one([BODY[0], BODY[1].replace("shall not lend", "shall lend"), BODY[2]])
    assert not lend["negations_agree"] and not lend["passed"] and lend["numbers_agree"]


def test_typography_hyphenated_line_ends_and_running_headers_are_not_differences():
    lines = [
        "Official Gazette No. 12 Vol. 7 A 345",
        "3. (1) The Board shall keep pro-",
        "per accounts of its income and expen-",
        "diture and shall not lend money to any person except with the ap-",
        "proval of the Minister. (2) The accounts shall be audited within 90 days of the",
        "end of each financial year.",
    ]
    furniture = ["Official Gazette No. 12 Vol. 7 A 345"]
    found = one(lines, reading_page=page(1, furniture=furniture))
    assert found["passed"] and found["agreement"] == 1.0
    # Where the reading kept only the page number, the header of the document's other pages is taken
    # out as well, with this page's number.
    raw = pdf({"lines": ["Official Gazette No. 12 Vol. 7 A 346", *BODY]}, {"lines": BODY})
    header = "Official Gazette No. 12 Vol. 7 A 347"
    split = reading(raw, page(1, furniture=["A 346"]), page(2, furniture=[header]))
    assert native.assess(raw, split)["pages"]["1"]["passed"]


def test_a_margin_note_drawn_elsewhere_is_found_and_order_is_measured():
    noted = page(1, notes=["Accounts and audit of the Board."])
    found = one([*BODY, "Accounts and audit of the Board."], reading_page=noted)
    assert found["passed"] and found["agreement"] == 1.0 and found["order"] == 1.0
    halves = [w for line in BODY for w in line.split()]
    shuffled = [" ".join(halves[i : i + 4]) for i in range(0, len(halves), 4)]
    shuffled = shuffled[1::2] + shuffled[0::2]  # two columns read across
    scrambled = one(shuffled)
    assert (
        scrambled["class"] == "native" and scrambled["order"] < native.ORDER_MIN and not scrambled["passed"]
    )


def test_a_reading_taken_from_the_text_layer_has_no_independent_check():
    found = one(reading_page=page(1, checks={}), primary="text-layer")
    assert found["class"] == "native" and not found["passed"] and found["flags"] is None
    assert any("no independent reading" in reason for reason in found["reasons"])


def test_each_flag_is_supported_only_where_the_layer_reads_as_the_units():
    # Tokens of TEXT: 0 "3." 1 "(1)" 2 "The" 3 "Board" 4 "shall" 5 "keep" 6 "proper" 7 "accounts"
    # 8 "of" 9 "its" 10 "income" 11 "and"
    flags = [
        flag(7, 8, "accounts", {"mineru": "acounts"}),  # the layer reads as the units: supported
        flag(3, 4, "Board", {"mineru": "Bord"}),  # the layer has "Bord" too: not supported
        flag(11, 11, "", {"mineru": "total", "azure": "total"}, kind="missing_from_primary"),  # supported
    ]
    lines = [BODY[0].replace("Board", "Bord"), *BODY[1:]]
    found = one(lines, reading_page=page(1, flags=flags))
    assert found["flags"]["supported"] == [0, 2] and found["flags"]["unsupported"] == [1]
    extra = one(
        [BODY[0].replace("income and", "income total and"), *BODY[1:]], reading_page=page(1, flags=flags[2:])
    )
    assert extra["flags"]["unsupported"] == [0]


def test_words_that_differ_where_no_flag_says_so_are_counted():
    found = one([BODY[0].replace("proper", "properly kept"), *BODY[1:]])
    assert found["flags"]["unflagged_differences"] == 3  # "proper" against "properly kept"
    covered = one(
        [BODY[0].replace("proper", "properly"), *BODY[1:]],
        reading_page=page(1, flags=[flag(6, 7, "proper", {"mineru": "properly"})]),
    )
    assert covered["flags"]["unflagged_differences"] == 0 and covered["flags"]["unsupported"] == [0]


def test_evidence_is_bound_to_the_pdf_and_to_the_page_it_assessed():
    raw = pdf({"lines": BODY})
    found = reading(raw, page(1))
    evidence = native.assess(raw, found)
    attached = native.attach(json.loads(json.dumps(found)), evidence)
    assert attached["pages"][0]["native"]["passed"]
    changed = json.loads(json.dumps(found))
    changed["pages"][0]["flags"] = [flag(3, 4, "Board", {"mineru": "Bord"})]  # the reading was prepared again
    assert "native" not in native.attach(changed, evidence)["pages"][0]
    other = {**json.loads(json.dumps(found)), "input_sha256": "1" * 64}
    assert "native" not in native.attach(other, evidence)["pages"][0]
    with pytest.raises(ValueError, match="not the one the reading was made from"):
        native.assess(pdf({"lines": BODY[:2]}), found)


def test_the_command_writes_evidence_beside_the_readings_and_never_into_the_repository(tmp_path, capsys):
    raw = pdf({"lines": BODY}, {})
    (tmp_path / "ng-test-act.pdf").write_bytes(raw)
    (tmp_path / "ng-test-act.reading.json").write_text(json.dumps(reading(raw, page(1), page(2))))
    assert native.main(["assess", "--staging", str(tmp_path)]) == 0
    written = json.loads((tmp_path / "ng-test-act.native.json").read_text())
    assert written["pages"]["1"]["class"] == "native" and written["pages"]["2"]["class"] == "scan"
    assert json.loads(capsys.readouterr().out)["classes"] == {"native": 1, "scan": 1}
    with pytest.raises(SystemExit):
        native.main(["assess", "--staging", str(tmp_path), "--out", str(native.ROOT / "tests")])
