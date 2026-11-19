"""Unit and end-to-end tests for multi-agent review Phase 4 (audit, pace, qualify)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from scripts import plant_mistakes
from workbench import audit, content, qualify
from workbench.store import Store, connect


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


def test_plant_mistakes_all_kinds_and_determinism(tmp_path: Path):
    in_dir = tmp_path / "clean"
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    in_dir.mkdir()
    (in_dir / "rw-test-law.reading.json").write_text(json.dumps(_synthetic_reading()), encoding="utf-8")

    key_a = plant_mistakes.plant_directory(in_dir, out_a, seed=99)
    key_b = plant_mistakes.plant_directory(in_dir, out_b, seed=99)

    assert key_a == key_b
    planted_kinds = {
        m["kind"]
        for doc in key_a["documents"].values()
        for page_entry in doc.values()
        for m in page_entry["mistakes"]
    }
    assert planted_kinds == set(plant_mistakes.MISTAKE_KINDS)
    assert key_a["total_mistakes"] == len(plant_mistakes.MISTAKE_KINDS)


def test_qualify_score_perfect_lazy_and_partial_with_wrong_changes(tmp_path: Path):
    in_dir = tmp_path / "clean"
    out_dir = tmp_path / "planted"
    in_dir.mkdir()
    clean_reading = _synthetic_reading()
    (in_dir / "rw-test-law.reading.json").write_text(json.dumps(clean_reading), encoding="utf-8")

    key = plant_mistakes.plant_directory(in_dir, out_dir, seed=7)
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
    # Ensure page 3 had no mistakes planted except possibly running_head; count page-1 vs page-2 mistakes
    p1_mistakes = len(doc_key["1"]["mistakes"])
    p2_mistakes = len(doc_key["2"]["mistakes"])
    partial_score = qualify.score_revisions(key, partial_revs, label="careless-01")
    assert partial_score["found_count"] >= p1_mistakes
    assert partial_score["missed_count"] >= p2_mistakes
    assert partial_score["wrong_change_count"] >= 2  # 1 untouched unit altered + 1 unexpected unit added


def test_qualify_cli_end_to_end_with_database_and_revisions_file(tmp_path: Path):
    in_dir = tmp_path / "clean"
    out_dir = tmp_path / "planted"
    in_dir.mkdir()
    (in_dir / "rw-cli-law.reading.json").write_text(json.dumps(_synthetic_reading()), encoding="utf-8")

    key_path = out_dir / "qualify-key.json"
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

    # Create a SQLite workbench DB with Phase-2 `label` and `task` columns on page_revision
    db_path = tmp_path / "qualify.db"
    db_url = f"sqlite:///{db_path}"
    engine = connect(db_url)
    Store(engine)
    existing_cols = {c["name"] for c in inspect(engine).get_columns("page_revision")}
    with engine.begin() as conn:
        if "label" not in existing_cols:
            conn.execute(text("ALTER TABLE page_revision ADD COLUMN label VARCHAR(40)"))
        if "task" not in existing_cols:
            conn.execute(text("ALTER TABLE page_revision ADD COLUMN task VARCHAR(20)"))
        now_iso = datetime.now(UTC).isoformat()
        for page_str, entry in doc_key.items():
            conn.execute(
                text(
                    "INSERT INTO page_revision "
                    "(act_id, page, revision, reading_sha256, units, status, resolutions, comment, author, created_at, label, task) "
                    "VALUES (:act_id, :page, :rev, :sha, :units, 'checked', '{}', '', :author, :at, :label, :task)"
                ),
                {
                    "act_id": "rw-cli-law",
                    "page": int(page_str),
                    "rev": 1,
                    "sha": content.reading_sha256({"nodes": [], "flags": []}),
                    "units": json.dumps(entry["clean_units"]),
                    "author": "claude@example.test",
                    "at": now_iso,
                    "label": "claude-m1-01",
                    "task": "review",
                },
            )
        # Also insert an audit-task row that must be ignored by qualify
        conn.execute(
            text(
                "INSERT INTO page_revision "
                "(act_id, page, revision, reading_sha256, units, status, resolutions, comment, author, created_at, label, task) "
                "VALUES ('rw-cli-law', 1, 2, 'sha', '[]', 'checked', '{}', '', 'claude@example.test', :at, 'claude-m1-01', 'audit')"
            ),
            {"at": now_iso},
        )

    cli_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workbench.qualify",
            "score",
            "--label",
            "claude-m1-01",
            "--key",
            str(key_path),
            "--database",
            db_url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(cli_proc.stdout)
    assert report["label"] == "claude-m1-01"
    assert report["found_count"] == key["total_mistakes"]
    assert report["missed_count"] == 0
    assert report["wrong_change_count"] == 0

    # Also test --revisions JSON file mode
    rev_file = tmp_path / "revs.json"
    rev_file.write_text(
        json.dumps({"rw-cli-law": {p: e["planted_units"] for p, e in doc_key.items()}}),
        encoding="utf-8",
    )
    file_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workbench.qualify",
            "score",
            "--label",
            "lazy-json",
            "--key",
            str(key_path),
            "--revisions",
            str(rev_file),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    file_report = json.loads(file_proc.stdout)
    assert file_report["found_count"] == 0
    assert file_report["missed_count"] == key["total_mistakes"]
