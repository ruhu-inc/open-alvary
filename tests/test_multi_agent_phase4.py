"""Unit and end-to-end tests for multi-agent review Phase 4 (audit, pace, qualify)."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import plant_mistakes
from workbench import audit, qualify


def _para(text_value: str, **extra) -> dict:
    return {"kind": "paragraph", "text": text_value, **extra}


def _heading(text_value: str, **extra) -> dict:
    return {"kind": "heading", "text": text_value, **extra}


def _list_item(text_value: str, marker: str = "", **extra) -> dict:
    item = {"kind": "list_item", "text": text_value, **extra}
    if marker:
        item["marker"] = marker
    return item


def _table(rows: list[list[str]], caption: str = "", **extra) -> dict:
    return {"kind": "table", "caption": caption, "header_rows": 1, "rows": rows, **extra}


# --- audit_diff ------------------------------------------------------------------------------


def test_audit_diff_clean_identical():
    units = [_heading("Article 1", rank=2), _para("This Law governs public procurement.")]
    result = audit.audit_diff(units, copy.deepcopy(units))
    assert result == {
        "paragraphs_changed": 0,
        "words_changed": 0,
        "other_changes": 0,
        "verdict": "clean",
    }


def test_audit_diff_word_change_is_material():
    rev = [_para("The fine shall not exceed 500 francs.")]
    aud = [_para("The fine shall not exceed 5000 francs.")]
    result = audit.audit_diff(rev, aud)
    assert result["verdict"] == "material"
    assert result["paragraphs_changed"] == 1
    assert result["words_changed"] == 1
    assert result["other_changes"] == 0


def test_audit_diff_paragraph_split_is_material():
    rev = [_para("First sentence of the article. Second sentence of the article.")]
    aud = [_para("First sentence of the article."), _para("Second sentence of the article.")]
    result = audit.audit_diff(rev, aud)
    assert result["verdict"] == "material"
    assert result["paragraphs_changed"] == 2
    assert result["words_changed"] == 0


def test_audit_diff_paragraph_merge_is_material():
    rev = [_para("First clause applies."), _para("Second clause applies.")]
    aud = [_para("First clause applies. Second clause applies.")]
    result = audit.audit_diff(rev, aud)
    assert result["verdict"] == "material"
    assert result["paragraphs_changed"] == 2
    assert result["words_changed"] == 0


def test_audit_diff_boundary_shift_is_material():
    rev = [_para("The Minister may issue orders."), _para("Such orders take effect immediately.")]
    aud = [_para("The Minister may issue"), _para("orders. Such orders take effect immediately.")]
    result = audit.audit_diff(rev, aud)
    assert result["verdict"] == "material"
    assert result["paragraphs_changed"] == 2
    assert result["words_changed"] == 0


def test_audit_diff_emphasis_alignment_rank_only_is_minor():
    rev = [
        _heading("Chapter I", rank=2, align="left"),
        _para("The Republic is sovereign."),
    ]
    aud = [
        _heading("Chapter I", rank=1, align="center"),
        _para("The Republic is sovereign.", marks=[{"type": "italic", "start": 4, "end": 12}]),
    ]
    result = audit.audit_diff(rev, aud)
    assert result["verdict"] == "minor"
    assert result["paragraphs_changed"] == 0
    assert result["words_changed"] == 0
    assert result["other_changes"] == 2


def test_audit_diff_table_cell_change_is_material_and_marks_only_is_minor():
    base = [_table([["Item", "Fee"], ["Passport", "50,000"]], caption="Schedule 1")]
    changed_cell = [_table([["Item", "Fee"], ["Passport", "60,000"]], caption="Schedule 1")]
    changed_marks = [
        _table(
            [["Item", "Fee"], ["Passport", "50,000"]],
            caption="Schedule 1",
            cell_marks={"0,0": [{"type": "bold", "start": 0, "end": 4}]},
        )
    ]

    res_cell = audit.audit_diff(base, changed_cell)
    assert res_cell["verdict"] == "material"
    assert res_cell["paragraphs_changed"] == 1
    assert res_cell["words_changed"] == 1

    res_marks = audit.audit_diff(base, changed_marks)
    assert res_marks["verdict"] == "minor"
    assert res_marks["paragraphs_changed"] == 0
    assert res_marks["words_changed"] == 0
    assert res_marks["other_changes"] == 1


# --- draw_pages ------------------------------------------------------------------------------


def test_draw_pages_determinism_and_input_forms():
    doc_pages = {
        "rw-law-1-en": list(range(1, 31)),
        "rw-law-1-fr": list(range(1, 31)),
    }
    flat_reversed = [(doc, p) for doc, pages in reversed(list(doc_pages.items())) for p in reversed(pages)]
    salt = "secret-audit-salt-for-testing-1234567890"

    draw_a = audit.draw_pages("rw-law-1", doc_pages, salt=salt, rate=0.05, minimum=2, release_number=1)
    draw_b = audit.draw_pages("rw-law-1", flat_reversed, salt=salt, rate=0.05, minimum=2, release_number=1)
    assert draw_a == draw_b
    assert len(draw_a) == 3  # ceil(0.05 * 60) == 3
    assert draw_a == sorted(draw_a)


def test_draw_pages_changes_with_release_number_and_salt():
    doc_pages = {"rw-law-2-en": list(range(1, 101))}
    salt_a = "salt-alpha-0000000000000000000000000000"
    salt_b = "salt-bravo-0000000000000000000000000000"

    r1 = audit.draw_pages("rw-law-2", doc_pages, salt=salt_a, rate=0.05, minimum=2, release_number=1)
    r2 = audit.draw_pages("rw-law-2", doc_pages, salt=salt_a, rate=0.05, minimum=2, release_number=2)
    r1_other_salt = audit.draw_pages(
        "rw-law-2", doc_pages, salt=salt_b, rate=0.05, minimum=2, release_number=1
    )

    assert len(r1) == 5
    assert r1 != r2
    assert r1 != r1_other_salt


def test_draw_pages_rate_zero_returns_empty_without_salt():
    doc_pages = {"rw-law-3-en": [1, 2, 3, 4, 5]}
    assert audit.draw_pages("rw-law-3", doc_pages, salt="", rate=0, minimum=2, release_number=1) == []


def test_draw_pages_minimum_and_validation():
    doc_pages = {"rw-law-4-en": [1, 2, 3]}
    salt = "salt-charlie-0000000000000000000000000"
    drawn = audit.draw_pages("rw-law-4", doc_pages, salt=salt, rate=0.05, minimum=2, release_number=1)
    assert len(drawn) == 2

    with pytest.raises(ValueError):
        audit.draw_pages("rw-law-4", doc_pages, salt="", rate=0.05, minimum=2, release_number=1)
    with pytest.raises(ValueError):
        audit.draw_pages("rw-law-4", doc_pages, salt=salt, rate=-0.1, minimum=2, release_number=1)


# --- evaluate_window -------------------------------------------------------------------------


def test_evaluate_window_insufficient_sample_and_early_stop():
    below_early = ["clean"] * 10 + ["material"] * 2
    res_pending = audit.evaluate_window(below_early)
    assert res_pending["status"] == "insufficient_sample"
    assert res_pending["breached"] is False
    assert res_pending["early_stopped"] is False
    assert res_pending["material"] == 2

    hit_early = ["clean"] * 10 + ["material"] * 3
    res_early = audit.evaluate_window(hit_early)
    assert res_early["status"] == "breached"
    assert res_early["breached"] is True
    assert res_early["early_stopped"] is True
    assert res_early["material"] == 3


def test_evaluate_window_rolling_100_pass_and_breach():
    within_mark = ["clean"] * 95 + ["minor"] * 3 + ["material"] * 2
    res_pass = audit.evaluate_window(within_mark)
    assert res_pass["status"] == "pass"
    assert res_pass["breached"] is False
    assert res_pass["early_stopped"] is False
    assert res_pass["window_size"] == 100
    assert res_pass["material"] == 2

    over_mark = ["clean"] * 97 + ["material"] * 3
    res_breach = audit.evaluate_window(over_mark)
    assert res_breach["status"] == "breached"
    assert res_breach["breached"] is True
    assert res_breach["early_stopped"] is False
    assert res_breach["material"] == 3

    # Rolling window drops older material pages once 100 clean pages follow.
    recovered = ["material"] * 5 + ["clean"] * 100
    res_recovered = audit.evaluate_window(recovered)
    assert res_recovered["status"] == "pass"
    assert res_recovered["breached"] is False
    assert res_recovered["total_audited"] == 105
    assert res_recovered["window_size"] == 100
    assert res_recovered["material"] == 0


def test_evaluate_window_rejects_unknown_verdict():
    with pytest.raises(ValueError):
        audit.evaluate_window(["clean", "bogus"])


# --- pace_report -----------------------------------------------------------------------------


def test_pace_report_flags_fast_full_pages_and_ignores_audit_task():
    t0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    long_text = " ".join(f"word{i}" for i in range(100))
    short_text = "Short title page."

    revisions = [
        # Fast agent: full page saved 12s after previous save -> flagged
        {
            "author": "claude@example.test",
            "label": "claude-m1-01",
            "task": "review",
            "act_id": "rw-law-1",
            "page": 2,
            "created_at": t0 + timedelta(seconds=12),
            "units": [_para(long_text)],
        },
        {
            "author": "claude@example.test",
            "label": "claude-m1-01",
            "task": "review",
            "act_id": "rw-law-1",
            "page": 1,
            "created_at": t0,
            "units": [_para(long_text)],
        },
        # Audit task saves must be filtered out completely
        {
            "author": "claude@example.test",
            "label": "claude-m1-01",
            "task": "audit",
            "act_id": "rw-law-9",
            "page": 1,
            "created_at": t0 + timedelta(seconds=15),
            "units": [_para(long_text)],
        },
        # Careful agent: short page in 5s (not flagged), full page in 45s (not flagged)
        {
            "author": "codex@example.test",
            "label": "codex-m1-01",
            "task": "review",
            "act_id": "rw-law-2",
            "page": 1,
            "created_at": t0.isoformat(),
            "units": [_para(long_text)],
        },
        {
            "author": "codex@example.test",
            "label": "codex-m1-01",
            "task": "review",
            "act_id": "rw-law-2",
            "page": 2,
            "created_at": (t0 + timedelta(seconds=5)).isoformat(),
            "units": [_para(short_text)],
        },
        {
            "author": "codex@example.test",
            "label": "codex-m1-01",
            "task": "review",
            "act_id": "rw-law-2",
            "page": 3,
            "created_at": (t0 + timedelta(seconds=50)).isoformat(),
            "units": [_para(long_text)],
        },
    ]

    report = audit.pace_report(revisions)
    assert len(report) == 2

    fast_row = next(r for r in report if r["label"] == "claude-m1-01")
    assert fast_row["pages_saved"] == 2
    assert fast_row["fast_full_pages"] == 1
    assert fast_row["flagged"] is True
    assert fast_row["min_seconds"] == 12.0

    careful_row = next(r for r in report if r["label"] == "codex-m1-01")
    assert careful_row["pages_saved"] == 3
    assert careful_row["fast_full_pages"] == 0
    assert careful_row["flagged"] is False
    assert careful_row["median_seconds"] == 25.0


# --- plant_mistakes + qualify end-to-end -----------------------------------------------------


def _synthetic_reading() -> dict:
    """Synthetic multi-page reading rich enough to accept all 8 mistake kinds."""
    return {
        "pages": [
            {
                "page": 1,
                "nodes": [
                    {"kind": "heading", "level": "heading-1", "text": "CHAPTER ONE: GENERAL PROVISIONS"},
                    {
                        "kind": "paragraph",
                        "text": "Article 1 establishes the regulatory framework for national telecommunications.",
                    },
                    {
                        "kind": "paragraph",
                        "text": "Every licensed operator shall submit annual reports to the Ministry.",
                    },
                    {
                        "kind": "paragraph",
                        "text": "The authority keeps a public register. Any citizen may inspect it during office hours.",
                    },
                ],
                "flags": [],
            },
            {
                "page": 2,
                "nodes": [
                    {"kind": "heading", "level": "heading-2", "text": "SECTION 2: OBLIGATIONS"},
                    {
                        "kind": "paragraph",
                        "text": "La République garantit la liberté de communication sur tout le territoire.",
                    },
                    {
                        "kind": "paragraph",
                        "text": "An application fee of 25000 francs is payable within 30 days of notice.",
                    },
                    {"kind": "list_item", "marker": "(a)", "text": "maintain accurate subscriber records;"},
                    {"kind": "list_item", "marker": "(b)", "text": "publish tariffs in plain language;"},
                    {"kind": "list_item", "marker": "(c)", "text": "protect personal data against misuse."},
                ],
                "flags": [],
            },
            {
                "page": 3,
                "nodes": [
                    {
                        "kind": "paragraph",
                        "text": "This final page records commencement and transitional savings.",
                    },
                    {
                        "kind": "paragraph",
                        "text": "Existing licences remain valid until their stated expiry date.",
                    },
                ],
                "flags": [],
            },
        ]
    }


def _clean_dir(tmp_path: Path, act_id: str = "rw-test-law", reading: dict | None = None) -> Path:
    in_dir = tmp_path / "clean"
    in_dir.mkdir()
    (in_dir / f"{act_id}.reading.json").write_text(
        json.dumps(reading or _synthetic_reading(), ensure_ascii=False), encoding="utf-8"
    )
    return in_dir


def _mistakes(doc_key: dict) -> list[tuple[str, dict]]:
    return [(page, m) for page, entry in doc_key.items() for m in entry["mistakes"]]


def test_plant_mistakes_all_kinds_and_determinism(tmp_path: Path):
    in_dir = _clean_dir(tmp_path)
    key_a = plant_mistakes.plant_directory(
        in_dir, tmp_path / "out_a", key_path=tmp_path / "key_a.json", seed=99
    )
    key_b = plant_mistakes.plant_directory(
        in_dir, tmp_path / "out_b", key_path=tmp_path / "key_b.json", seed=99
    )

    assert key_a == key_b
    assert (tmp_path / "out_a" / "rw-test-law.reading.json").read_bytes() == (
        tmp_path / "out_b" / "rw-test-law.reading.json"
    ).read_bytes()
    planted_kinds = {
        m["kind"]
        for doc in key_a["documents"].values()
        for page_entry in doc.values()
        for m in page_entry["mistakes"]
    }
    assert planted_kinds == set(plant_mistakes.MISTAKE_KINDS)
    assert key_a["total_mistakes"] == len(plant_mistakes.MISTAKE_KINDS)


def test_plant_mistakes_key_ranges_point_at_the_mistake(tmp_path: Path):
    in_dir = _clean_dir(tmp_path)
    key = plant_mistakes.plant_directory(in_dir, tmp_path / "out", key_path=tmp_path / "key.json", seed=7)
    for _, mistake in _mistakes(key["documents"]["rw-test-law"]):
        page = key["documents"]["rw-test-law"][str(mistake["page"])]
        c0, c1 = mistake["clean_range"]
        p0, p1 = mistake["planted_range"]
        clean = [qualify._norm(u.get("text")) for u in page["clean_units"][c0:c1]]
        planted = [qualify._norm(u.get("text")) for u in page["planted_units"][p0:p1]]
        if mistake["kind"] == "running_head_in_text":
            assert clean == [] and planted == [mistake["after_text"]]
        elif mistake["kind"] in {"merged_paragraphs", "heading_run_into_paragraph", "flattened_list"}:
            assert clean == mistake["before_texts"] and planted == [mistake["after_text"]]
        elif mistake["kind"] == "split_paragraph":
            assert clean == [mistake["before_text"]] and planted == mistake["after_texts"]
        else:
            assert clean == [mistake["before_text"]] and planted == [mistake["after_text"]]


def test_planted_page_gives_nothing_away(tmp_path: Path):
    reading = _synthetic_reading()
    reading["summary"] = {"pages": 3, "flags": {"high": 1, "medium": 0, "low": 0}}
    page2 = reading["pages"][1]
    page2["text"] = "\n".join(n["text"] for n in page2["nodes"])
    page2["flags"] = [
        {
            "severity": "high",
            "kind": "word",
            "primary": "25000",
            "checks": {"azure": "25000"},
            "tokens": [12, 13],
        }
    ]
    page2["checks"] = {"azure": page2["text"], "mineru": page2["text"]}
    page2["lines"] = [{"text": "La République garantit", "box": [0.1, 0.1, 0.9, 0.2]}]
    in_dir = _clean_dir(tmp_path, reading=reading)
    out_dir = tmp_path / "planted"
    key = plant_mistakes.plant_directory(in_dir, out_dir, key_path=tmp_path / "key.json", seed=7)

    practice = json.loads((out_dir / "rw-test-law.reading.json").read_text(encoding="utf-8"))
    assert practice["summary"]["flags"] == {"high": 0, "medium": 0, "low": 0}
    for page in practice["pages"]:
        entry = key["documents"]["rw-test-law"][str(page["page"])]
        assert page["flags"] == []
        # The page's text is made from the planted nodes, not the clean ones.
        assert page["text"].split("\n") == [qualify._norm(u["text"]) for u in entry["planted_units"]]
        assert not any(key_name.startswith("_plant") for node in page["nodes"] for key_name in node)
        assert content_units(page) == entry["planted_units"]
    planted2 = practice["pages"][1]
    assert planted2["checks"] == {"azure": "", "mineru": ""}
    assert planted2["lines"] == [{"text": "", "box": [0.1, 0.1, 0.9, 0.2]}]
    for _, mistake in _mistakes(key["documents"]["rw-test-law"]):
        if mistake.get("before_text"):
            assert mistake["before_text"] not in json.dumps(practice, ensure_ascii=False)
    assert (out_dir / plant_mistakes.MARKER_NAME).is_file()
    assert not (out_dir / "qualify-key.json").exists()


def content_units(page: dict) -> list[dict]:
    from workbench import content

    return content.units_from(page)


def test_plant_mistakes_refuses_unsafe_places(tmp_path: Path):
    in_dir = _clean_dir(tmp_path)
    clean_bytes = (in_dir / "rw-test-law.reading.json").read_bytes()
    key = tmp_path / "keys" / "key.json"

    def refused(output: Path, key_path: Path, **kw) -> str:
        with pytest.raises(plant_mistakes.PlantError) as caught:
            plant_mistakes.plant_directory(in_dir, output, key_path=key_path, **kw)
        return str(caught.value)

    assert "folder of its own" in refused(in_dir, key)
    assert "folder of its own" in refused(in_dir / "sub", key)
    assert "folder of its own" in refused(tmp_path, key)
    assert "outside the practice copy" in refused(tmp_path / "out", tmp_path / "out" / "key.json")
    assert "outside the practice copy" in refused(tmp_path / "out", in_dir / "key.json")

    real = tmp_path / "real-collection"
    real.mkdir()
    (real / "manifest.json").write_text("{}", encoding="utf-8")
    assert "did not write" in refused(real, key)
    (real / "manifest.json").unlink()
    (real / "ng-act.reading.json").write_text("{}", encoding="utf-8")
    assert "did not write" in refused(real, key)
    assert (real / "ng-act.reading.json").read_text(encoding="utf-8") == "{}"

    out = tmp_path / "out"
    plant_mistakes.plant_directory(in_dir, out, key_path=key, seed=3)
    assert "--overwrite" in refused(out, key)
    assert "--overwrite" in refused(out, tmp_path / "other-key.json")
    plant_mistakes.plant_directory(in_dir, out, key_path=key, seed=3, overwrite=True)
    assert (in_dir / "rw-test-law.reading.json").read_bytes() == clean_bytes


def test_plant_mistakes_cli_needs_a_key_and_refuses_in_one_sentence(tmp_path: Path):
    in_dir = _clean_dir(tmp_path)
    no_key = subprocess.run(
        [
            sys.executable,
            "scripts/plant_mistakes.py",
            "--input",
            str(in_dir),
            "--output",
            str(tmp_path / "o"),
        ],
        capture_output=True,
        text=True,
    )
    assert no_key.returncode == 2
    assert "--key" in no_key.stderr

    same = subprocess.run(
        [
            sys.executable,
            "scripts/plant_mistakes.py",
            "--input",
            str(in_dir),
            "--output",
            str(in_dir),
            "--key",
            str(tmp_path / "key.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert same.returncode == 2
    assert same.stderr.strip().count("\n") == 0
    assert same.stderr.startswith("plant_mistakes: ")
    assert (
        json.loads((in_dir / "rw-test-law.reading.json").read_text(encoding="utf-8")) == _synthetic_reading()
    )


def test_qualify_score_perfect_lazy_and_partial_with_wrong_changes(tmp_path: Path):
    in_dir = _clean_dir(tmp_path)
    key = plant_mistakes.plant_directory(in_dir, tmp_path / "planted", key_path=tmp_path / "key.json", seed=7)
    doc_key = key["documents"]["rw-test-law"]

    # 1. Perfect agent restores every page to clean_units
    perfect_revs = {("rw-test-law", int(p)): copy.deepcopy(e["clean_units"]) for p, e in doc_key.items()}
    perfect_score = qualify.score_revisions(key, perfect_revs, label="claude-m1-01")
    assert perfect_score["found_count"] == key["total_mistakes"]
    assert perfect_score["missed_count"] == 0
    assert perfect_score["wrong_change_count"] == 0
    assert perfect_score["recall"] == 1.0

    # 2. Lazy agent saves planted_units unchanged
    lazy_revs = {("rw-test-law", int(p)): copy.deepcopy(e["planted_units"]) for p, e in doc_key.items()}
    lazy_score = qualify.score_revisions(key, lazy_revs, label="lazy-01")
    assert lazy_score["found_count"] == 0
    assert lazy_score["missed_count"] == key["total_mistakes"]
    assert lazy_score["wrong_change_count"] == 0
    assert lazy_score["recall"] == 0.0

    # 3. Partial + careless agent: fixes page 1, misses page 2, and corrupts clean text on page 3
    partial_revs = {
        ("rw-test-law", 1): copy.deepcopy(doc_key["1"]["clean_units"]),
        ("rw-test-law", 2): copy.deepcopy(doc_key["2"]["planted_units"]),
        ("rw-test-law", 3): [
            _para("This final page records commencement and WRONG EDIT."),
            _para("Existing licences remain valid until their stated expiry date."),
            _para("Hallucinated extra paragraph."),
        ],
    }
    p1_mistakes = len(doc_key["1"]["mistakes"])
    p2_mistakes = len(doc_key["2"]["mistakes"])
    partial_score = qualify.score_revisions(key, partial_revs, label="careless-01")
    assert partial_score["found_count"] >= p1_mistakes
    assert partial_score["missed_count"] >= p2_mistakes
    assert partial_score["wrong_change_count"] >= 1


def test_qualify_restored_words_without_structure_score_as_missed(tmp_path: Path):
    """Reviewer's counter-example: the words are back but every unit is a plain paragraph."""
    in_dir = _clean_dir(tmp_path)
    key = plant_mistakes.plant_directory(
        in_dir, tmp_path / "planted", key_path=tmp_path / "key.json", seed=99
    )
    doc_key = key["documents"]["rw-test-law"]
    assert {m["kind"] for _, m in _mistakes(doc_key)} == set(plant_mistakes.MISTAKE_KINDS)
    flat = {("rw-test-law", int(p)): [_para(u["text"]) for u in e["clean_units"]] for p, e in doc_key.items()}
    score = qualify.score_revisions(key, flat, label="flat-01")
    missed = {m["kind"] for m in score["mistakes_missed"]}
    found = {m["kind"] for m in score["mistakes_found"]}
    assert {"heading_run_into_paragraph", "flattened_list"} <= missed
    assert {"heading_run_into_paragraph", "flattened_list"}.isdisjoint(found)
    # Mistakes whose clean units were plain paragraphs are found.
    assert {"merged_paragraphs", "split_paragraph"} <= found
    # The headings and list items it made paragraphs are correct units changed wrongly.
    assert score["wrong_change_count"] >= 1


def test_qualify_heading_needs_its_rank_and_the_mistake_must_be_gone(tmp_path: Path):
    in_dir = _clean_dir(tmp_path)
    key = plant_mistakes.plant_directory(
        in_dir,
        tmp_path / "planted",
        key_path=tmp_path / "key.json",
        seed=7,
        kinds=["heading_run_into_paragraph", "merged_paragraphs"],
    )
    doc_key = key["documents"]["rw-test-law"]
    page, heading = next((p, m) for p, m in _mistakes(doc_key) if m["kind"] == "heading_run_into_paragraph")
    saved = copy.deepcopy(doc_key[page]["clean_units"])
    c0 = heading["clean_range"][0]
    saved[c0]["rank"] = saved[c0]["rank"] % 6 + 1
    score = qualify.score_revisions(key, {("rw-test-law", int(page)): saved})
    assert heading["id"] in {m["id"] for m in score["mistakes_missed"]}

    page, merged = next((p, m) for p, m in _mistakes(doc_key) if m["kind"] == "merged_paragraphs")
    saved = copy.deepcopy(doc_key[page]["clean_units"])
    c0, c1 = merged["clean_range"]
    saved[c1:c1] = [_para(merged["after_text"])]  # both paragraphs back, the merged one left too
    score = qualify.score_revisions(key, {("rw-test-law", int(page)): saved})
    assert merged["id"] in {m["id"] for m in score["mistakes_missed"]}


def _one_page_reading(*texts: str) -> dict:
    return {"pages": [{"page": 1, "nodes": [{"kind": "paragraph", "text": t} for t in texts], "flags": []}]}


def _key_for(reading: dict, kind: str) -> tuple[dict, dict]:
    _, doc_key = plant_mistakes.plant_on_reading("doc", reading, seed=5, kinds=[kind])
    (_, mistake) = _mistakes(doc_key)[0]
    return {"documents": {"doc": doc_key}}, mistake


def test_qualify_digit_changed_to_another_wrong_value_is_missed():
    """Reviewer's counter-example: "30" planted as "31", saved as "39", while "30" stands elsewhere."""
    reading = _one_page_reading("The period is 30 days.", "Section 30 of the Act applies.")
    key, mistake = _key_for(reading, "changed_digit")
    assert mistake["before_token"] == "30" and mistake["after_token"] == "31"
    c0 = mistake["clean_range"][0]
    saved = copy.deepcopy(key["documents"]["doc"]["1"]["clean_units"])
    saved[c0]["text"] = saved[c0]["text"].replace("30", "39")
    score = qualify.score_revisions(key, {("doc", 1): saved})
    assert score["found_count"] == 0
    assert score["missed_count"] == 1


def test_qualify_correct_fix_found_when_the_wrong_token_stands_elsewhere():
    reading = _one_page_reading("The period is 30 days.", "Article 31 is repealed.")
    key, mistake = _key_for(reading, "changed_digit")
    assert mistake["after_token"] == "31"
    saved = copy.deepcopy(key["documents"]["doc"]["1"]["clean_units"])
    score = qualify.score_revisions(key, {("doc", 1): saved})
    assert score["found_count"] == 1
    assert score["wrong_change_count"] == 0


def test_qualify_correct_accent_fix_found_when_the_wrong_token_is_inside_another_word():
    """Reviewer's counter-example: "été" planted as "ete", with "complete" on the page; the clean
    reading is decomposed (NFD) and the editor saves composed (NFC) text."""
    decomposed = unicodedata.normalize("NFD", "En été la liste est complete.")
    reading = _one_page_reading(decomposed, "Une autre phrase complete.")
    key, mistake = _key_for(reading, "dropped_accent")
    assert mistake["after_token"] == "ete"
    saved = [
        {**u, "text": unicodedata.normalize("NFC", u["text"])}
        for u in key["documents"]["doc"]["1"]["clean_units"]
    ]
    score = qualify.score_revisions(key, {("doc", 1): saved})
    assert score["found_count"] == 1
    assert score["wrong_change_count"] == 0


def _workbench_db(path: Path, *, label: bool = True) -> str:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE page_revision (act_id TEXT, page INTEGER, revision INTEGER, units TEXT, "
        "author TEXT, task TEXT" + (", label TEXT" if label else "") + ")"
    )
    db.commit()
    db.close()
    return f"sqlite:///{path}"


def _tables(path: Path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        db.close()


def _score_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "workbench.qualify", "score", *args], capture_output=True, text=True
    )


def test_qualify_cli_end_to_end_with_database_and_revisions_file(tmp_path: Path):
    in_dir = _clean_dir(tmp_path, act_id="rw-cli-law")
    out_dir = tmp_path / "planted"
    key_path = tmp_path / "keys" / "qualify-key.json"
    plant_proc = subprocess.run(
        [
            sys.executable,
            "scripts/plant_mistakes.py",
            "--input",
            str(in_dir),
            "--output",
            str(out_dir),
            "--key",
            str(key_path),
            "--seed",
            "11",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    plant_summary = json.loads(plant_proc.stdout)
    assert plant_summary["documents"] == 1
    assert plant_summary["total_mistakes"] == len(plant_mistakes.MISTAKE_KINDS)

    key = json.loads(key_path.read_text(encoding="utf-8"))
    doc_key = key["documents"]["rw-cli-law"]

    db_path = tmp_path / "qualify.db"
    db_url = _workbench_db(db_path)
    db = sqlite3.connect(db_path)
    for page_str, entry in doc_key.items():
        db.execute(
            "INSERT INTO page_revision VALUES (?, ?, 1, ?, 'claude@example.test', 'review', 'claude-m1-01')",
            ("rw-cli-law", int(page_str), json.dumps(entry["clean_units"])),
        )
    # An audit-task row that must be ignored by qualify
    db.execute(
        "INSERT INTO page_revision VALUES "
        "('rw-cli-law', 1, 2, '[]', 'claude@example.test', 'audit', 'claude-m1-01')"
    )
    db.commit()
    db.close()
    before = db_path.read_bytes()

    cli_proc = _score_cli("--label", "claude-m1-01", "--key", str(key_path), "--database", db_url)
    assert cli_proc.returncode == 0, cli_proc.stderr
    report = json.loads(cli_proc.stdout)
    assert report["label"] == "claude-m1-01"
    assert report["found_count"] == key["total_mistakes"]
    assert report["missed_count"] == 0
    assert report["wrong_change_count"] == 0
    # Read only: nothing was created or changed.
    assert db_path.read_bytes() == before
    assert _tables(db_path) == ["page_revision"]

    # Also test --revisions JSON file mode
    rev_file = tmp_path / "revs.json"
    rev_file.write_text(
        json.dumps({"rw-cli-law": {p: e["planted_units"] for p, e in doc_key.items()}}),
        encoding="utf-8",
    )
    file_proc = _score_cli("--label", "lazy-json", "--key", str(key_path), "--revisions", str(rev_file))
    assert file_proc.returncode == 0, file_proc.stderr
    file_report = json.loads(file_proc.stdout)
    assert file_report["found_count"] == 0
    assert file_report["missed_count"] == key["total_mistakes"]


def test_qualify_cli_never_builds_a_schema(tmp_path: Path):
    key_path = tmp_path / "key.json"
    key_path.write_text(json.dumps({"documents": {}}), encoding="utf-8")

    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE unrelated (x INTEGER)")
    db.commit()
    db.close()
    before = other.read_bytes()
    proc = _score_cli("--label", "a", "--key", str(key_path), "--database", f"sqlite:///{other}")
    assert proc.returncode == 2
    assert "no page_revision table" in proc.stderr
    assert proc.stderr.strip().count("\n") == 0
    assert _tables(other) == ["unrelated"]
    assert other.read_bytes() == before

    unlabelled = tmp_path / "old.db"
    url = _workbench_db(unlabelled, label=False)
    proc = _score_cli("--label", "a", "--key", str(key_path), "--database", url)
    assert proc.returncode == 2
    assert "no label column" in proc.stderr

    missing = tmp_path / "missing.db"
    proc = _score_cli("--label", "a", "--key", str(key_path), "--database", f"sqlite:///{missing}")
    assert proc.returncode == 2
    assert not missing.exists()


def test_qualify_does_not_use_the_workbench_store():
    assert not hasattr(qualify, "connect")
    engine = qualify.open_read_only("postgresql+psycopg://user:pw@127.0.0.1:1/none")
    try:
        assert engine.dialect.name == "postgresql"
    finally:
        engine.dispose()
