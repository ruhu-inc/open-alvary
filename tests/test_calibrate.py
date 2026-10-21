"""The calibration of structure rules on cloud readings (ingestion.calibrate): a sample's Layout Parser
reading, checked against its OCR, becomes the workbench's review units and is read by a family's rules,
and language versions are compared provision by provision."""

import json

from ingestion import calibrate
from workbench import structure


def sample(folder, nodes, words):
    """A sample as private-staging/structure-samples/ holds one: layout.json and ocr.json, no PDF."""
    folder.mkdir()
    (folder / "layout.json").write_text(json.dumps({"processor": "layout", "nodes": nodes, "retries": []}))
    pages = [
        {"page": n, "width": 1000, "height": 1400, "languages": [], "words": w} for n, w in words.items()
    ]
    (folder / "ocr.json").write_text(json.dumps({"processor": "ocr", "pages": pages, "retries": []}))
    return folder


def word(text, x0, y0):
    return {"text": text, "x0": x0, "y0": y0, "x1": x0 + 0.05, "y1": y0 + 0.01, "confidence": 0.99}


def act(folder, sections):
    nodes = [{"kind": "heading", "level": 1, "page": 1, "text": "THE DATA PROTECTION ACT, 2019"}]
    nodes += [{"kind": "heading", "level": 1, "page": 1, "text": "PART I—PRELIMINARY"}]
    for number in sections:
        nodes.append(
            {"kind": "paragraph", "level": 0, "page": 1, "text": f"{number}. Interpretation of {number}."}
        )
    nodes.append({"kind": "furniture", "level": 0, "page": 1, "text": "901"})
    return sample(folder, nodes, {1: [word("PART", 0.1, 0.1), word("I—PRELIMINARY", 0.16, 0.1)]})


def test_a_sample_is_read_as_the_workbench_would_hold_it_then_by_its_familys_rules(tmp_path):
    report = calibrate.calibrate(act(tmp_path / "ke", ["1", "2", "3"]), structure.KE_ACT, None)
    assert report["pages"] == 1 and report["kinds"]["section"] == 3 and report["kinds"]["part"] == 1
    assert report["provisions"] == ["sec_1", "sec_2", "sec_3"]
    assert "901" not in json.dumps(report["outline"])  # the page's furniture is never a unit


def test_language_versions_are_compared_provision_by_provision(tmp_path):
    whole = calibrate.calibrate(act(tmp_path / "en", ["1", "2", "3"]), structure.KE_ACT, "en")
    short = calibrate.calibrate(act(tmp_path / "sw", ["1", "3"]), structure.KE_ACT, "sw")
    assert calibrate.compare([whole, short]) == {
        "en": {"count": 3, "missing": []},
        "sw": {"count": 2, "missing": ["sec_2"]},
    }


def test_the_ocr_words_are_laid_out_column_by_column_and_line_by_line():
    left = [word("second", 0.1, 0.30), word("First", 0.1, 0.10), word("line", 0.15, 0.101)]
    right = [word("side", 0.65, 0.1), word("right", 0.6, 0.1)]
    page = {"width": 1000, "height": 1000, "words": left + right}
    assert calibrate.ocr_text(page).splitlines() == ["First line", "second", "right side"]
