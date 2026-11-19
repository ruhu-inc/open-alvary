"""Scoring CLI for the multi-agent review qualifying test (section 7.1).

Compares an agent's saved page revisions against the manifest key produced by
``scripts/plant_mistakes.py`` and reports mistakes found, mistakes missed, and
correct text changed wrongly.

Usage:
    python -m workbench.qualify score --label claude-m1-01 --key qualify-key.json --database URL
    python -m workbench.qualify score --label claude-m1-01 --key qualify-key.json --revisions saved.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from workbench.audit import _norm_text, _unit_content, audit_diff
from workbench.store import connect


def _mistake_fixed(mistake: Mapping[str, Any], saved_units: Sequence[dict[str, Any]]) -> bool:
    """Return True if the planted mistake is resolved in ``saved_units``."""
    kind = mistake["kind"]
    saved_texts = [_norm_text(u.get("text")) for u in saved_units if u.get("kind") != "table"]
    saved_text_set = set(saved_texts)

    if kind == "merged_paragraphs":
        before_texts = list(mistake.get("before_texts") or [])
        after_text = mistake.get("after_text", "")
        return (
            bool(before_texts)
            and all(t in saved_text_set for t in before_texts)
            and after_text not in saved_text_set
        )

    if kind == "split_paragraph":
        before_text = mistake.get("before_text", "")
        return bool(before_text) and before_text in saved_text_set

    if kind == "heading_run_into_paragraph":
        before_texts = list(mistake.get("before_texts") or [])
        after_text = mistake.get("after_text", "")
        if len(before_texts) < 2 or after_text in saved_text_set:
            return False
        heading_text, para_text = before_texts[0], before_texts[1]
        has_heading = any(
            u.get("kind") == "heading" and _norm_text(u.get("text")) == heading_text for u in saved_units
        )
        return has_heading and para_text in saved_text_set

    if kind == "flattened_list":
        before_texts = list(mistake.get("before_texts") or [])
        after_text = mistake.get("after_text", "")
        if not before_texts or after_text in saved_text_set:
            return False
        list_texts = {_norm_text(u.get("text")) for u in saved_units if u.get("kind") == "list_item"}
        return all(t in list_texts for t in before_texts)

    if kind == "running_head_in_text":
        after_text = mistake.get("after_text", "")
        return bool(after_text) and after_text not in saved_text_set

    if kind in {"line_end_hyphen", "dropped_accent", "changed_digit"}:
        before_text = mistake.get("before_text", "")
        after_text = mistake.get("after_text", "")
        if before_text and before_text in saved_text_set:
            return True
        if after_text and after_text in saved_text_set:
            return False
        before_token = mistake.get("before_token", "")
        after_token = mistake.get("after_token", "")
        joined = " ".join(saved_texts)
        return (
            bool(before_token) and before_token in joined and (not after_token or after_token not in joined)
        )

    return False


def _wrong_changes_on_page(
    act_id: str,
    page: int,
    clean_units: Sequence[dict[str, Any]],
    planted_units: Sequence[dict[str, Any]],
    saved_units: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Identify correct text on ``page`` that was wrongly altered or spurious units added."""
    if audit_diff(clean_units, saved_units)["verdict"] in ("clean", "minor"):
        return []

    clean_counter = Counter(_unit_content(u) for u in clean_units)
    planted_counter = Counter(_unit_content(u) for u in planted_units)
    saved_counter = Counter(_unit_content(u) for u in saved_units)

    untouched = clean_counter & planted_counter
    untouched_missing = untouched - saved_counter

    wrong: list[dict[str, Any]] = []
    missing_count = 0
    for content_key, count in sorted(untouched_missing.items()):
        for _ in range(count):
            missing_count += 1
            wrong.append(
                {
                    "act_id": act_id,
                    "page": page,
                    "reason": "untouched_unit_changed",
                    "expected": content_key[1] if content_key[0] == "text" else str(content_key),
                }
            )

    allowed = clean_counter | planted_counter
    unexpected = saved_counter - allowed
    extra_remaining = max(0, sum(unexpected.values()) - missing_count)
    if extra_remaining > 0:
        for content_key, count in sorted(unexpected.items()):
            for _ in range(count):
                if extra_remaining <= 0:
                    break
                extra_remaining -= 1
                wrong.append(
                    {
                        "act_id": act_id,
                        "page": page,
                        "reason": "unexpected_unit_added",
                        "found": content_key[1] if content_key[0] == "text" else str(content_key),
                    }
                )
    return wrong


def score_revisions(
    key: Mapping[str, Any],
    revisions: Mapping[tuple[str, int], list[dict[str, Any]]],
    *,
    label: str = "",
) -> dict[str, Any]:
    """Compare saved page revisions against a ``plant_mistakes`` manifest key."""
    mistakes_found: list[dict[str, Any]] = []
    mistakes_missed: list[dict[str, Any]] = []
    wrong_changes: list[dict[str, Any]] = []
    pages_in_key = 0
    pages_saved = 0

    documents = key.get("documents") or {}
    for act_id in sorted(documents):
        pages = documents[act_id]
        for page_str in sorted(pages, key=lambda p: int(p)):
            page = int(page_str)
            pages_in_key += 1
            entry = pages[page_str]
            clean_units = entry.get("clean_units") or []
            planted_units = entry.get("planted_units") or []
            mistakes = entry.get("mistakes") or []

            saved_units = revisions.get((act_id, page))
            if saved_units is None:
                for mistake in mistakes:
                    mistakes_missed.append({**mistake, "reason": "page_not_saved"})
                continue

            pages_saved += 1
            page_clean = audit_diff(clean_units, saved_units)["verdict"] in ("clean", "minor")
            for mistake in mistakes:
                if page_clean or _mistake_fixed(mistake, saved_units):
                    mistakes_found.append(dict(mistake))
                else:
                    mistakes_missed.append({**mistake, "reason": "not_fixed"})

            wrong_changes.extend(
                _wrong_changes_on_page(act_id, page, clean_units, planted_units, saved_units)
            )

    total_mistakes = len(mistakes_found) + len(mistakes_missed)
    return {
        "label": label,
        "pages_in_key": pages_in_key,
        "pages_saved": pages_saved,
        "total_mistakes": total_mistakes,
        "found_count": len(mistakes_found),
        "missed_count": len(mistakes_missed),
        "wrong_change_count": len(wrong_changes),
        "recall": round(len(mistakes_found) / total_mistakes, 4) if total_mistakes else 1.0,
        "mistakes_found": mistakes_found,
        "mistakes_missed": mistakes_missed,
        "wrong_changes": wrong_changes,
    }


def load_revisions_for_label(
    engine: Engine,
    label: str,
    *,
    author: str | None = None,
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Load the latest saved ``page_revision.units`` per ``(act_id, page)`` for ``label``."""
    columns = {col["name"] for col in inspect(engine).get_columns("page_revision")}
    clauses = []
    params: dict[str, Any] = {}

    if "label" in columns:
        clauses.append("label = :label")
        params["label"] = label
        if author:
            clauses.append("author = :author")
            params["author"] = author.lower()
    else:
        clauses.append("author = :author")
        params["author"] = (author or label).lower()

    if "task" in columns:
        clauses.append("(task IS NULL OR task <> 'audit')")

    where_sql = " AND ".join(clauses)
    query = text(
        f"SELECT act_id, page, revision, units FROM page_revision "
        f"WHERE {where_sql} ORDER BY act_id, page, revision"
    )
    latest: dict[tuple[str, int], list[dict[str, Any]]] = {}
    with engine.connect() as db:
        for row in db.execute(query, params):
            units = row.units
            if isinstance(units, str):
                units = json.loads(units)
            latest[(str(row.act_id), int(row.page))] = list(units or [])
    return latest


def load_revisions_file(path: Path) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Load revisions from a JSON file mapping ``{act_id: {page: units}}`` or a list of records."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    if isinstance(raw, list):
        for item in raw:
            out[(str(item["act_id"]), int(item["page"]))] = list(item.get("units") or [])
    elif isinstance(raw, dict):
        for act_id, pages in raw.items():
            for page_str, units in pages.items():
                out[(str(act_id), int(page_str))] = list(units or [])
    else:
        raise ValueError("revisions JSON must be a list of records or a {act_id: {page: units}} mapping")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    score_cmd = subparsers.add_parser("score", help="Score saved revisions for a label against a key")
    score_cmd.add_argument("--label", required=True, help="Agent label to score")
    score_cmd.add_argument("--key", required=True, type=Path, help="Manifest key JSON from plant_mistakes.py")
    score_cmd.add_argument(
        "--database",
        default=None,
        help="Workbench database URL (defaults to ALVARY_REVIEW_DATABASE_URL)",
    )
    score_cmd.add_argument("--author", default=None, help="Optional author email filter")
    score_cmd.add_argument(
        "--revisions",
        type=Path,
        default=None,
        help="Optional JSON file of saved revisions (alternative to --database)",
    )

    args = parser.parse_args(argv)
    if args.command == "score":
        key = json.loads(args.key.read_text(encoding="utf-8"))
        if args.revisions is not None:
            revisions = load_revisions_file(args.revisions)
        else:
            db_url = args.database or os.environ.get("ALVARY_REVIEW_DATABASE_URL")
            if not db_url:
                parser.error("provide --database, ALVARY_REVIEW_DATABASE_URL, or --revisions")
            engine = connect(db_url)
            revisions = load_revisions_for_label(engine, args.label, author=args.author)
        report = score_revisions(key, revisions, label=args.label)
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
