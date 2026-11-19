"""Scoring CLI for the multi-agent review qualifying test (section 7.1).

Compares an agent's saved page revisions against the answer key produced by
``scripts/plant_mistakes.py`` and reports mistakes found, mistakes missed, and
correct text changed wrongly.

Each planted mistake is judged at its own place: the key says which clean units the mistake
replaced (``clean_range``) and which planted units stand there instead (``planted_range``). The
saved page is lined up with the clean page unit by unit (kind, words, list marker); a mistake is
found only when every clean unit of its range is back, in order and side by side, a heading with
its rank, and none of the planted units is left between its neighbours.

The database is only ever read: it is opened read-only and never created or upgraded.

Usage:
    python -m workbench.qualify score --label claude-m1-01 --key qualify-key.json --database URL
    python -m workbench.qualify score --label claude-m1-01 --key qualify-key.json --revisions saved.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url


class QualifyError(Exception):
    """A plain-sentence reason the score cannot be taken."""


def _norm(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def _place_signature(unit: Mapping[str, Any]) -> tuple:
    """What must match for a unit to stand where the clean one stood: its kind, words and marker."""
    if unit.get("kind") == "table":
        rows = tuple(tuple(_norm(cell) for cell in row) for row in (unit.get("rows") or []))
        return ("table", _norm(unit.get("caption")), rows)
    return (str(unit.get("kind") or "paragraph"), _norm(unit.get("text")), _norm(unit.get("marker")))


def _content_signature(unit: Mapping[str, Any]) -> tuple:
    """What counts as a material change to a unit: its place signature, its joins with the previous
    page and, for a table, its spans."""
    base = _place_signature(unit)
    if unit.get("kind") == "table":
        spans = tuple(tuple(span) for span in (unit.get("spans") or []))
        return (*base, spans)
    joins = tuple(bool(unit.get(key)) for key in ("continues_previous", "dehyphenate", "keep_hyphen"))
    return (*base, joins)


def _describe(unit_signature: tuple) -> str:
    if unit_signature[0] == "table":
        return f"table {unit_signature[1]!r}"
    kind, words, marker = unit_signature[0], unit_signature[1], unit_signature[2]
    return f"{kind}: {marker + ' ' if marker else ''}{words}"


def _range(mistake: Mapping[str, Any], key: str) -> tuple[int, int]:
    found = mistake.get(key)
    if not (isinstance(found, list | tuple) and len(found) == 2):
        raise QualifyError(
            f"Mistake {mistake.get('id', '?')} has no {key}: the key was written by an older "
            "plant_mistakes.py, so plant the practice copy again."
        )
    return int(found[0]), int(found[1])


def _mistake_fixed(
    mistake: Mapping[str, Any],
    clean_units: Sequence[Mapping[str, Any]],
    planted_units: Sequence[Mapping[str, Any]],
    saved_units: Sequence[Mapping[str, Any]],
) -> bool:
    """Return True if the planted mistake is resolved at its own place in ``saved_units``."""
    if mistake["kind"] == "running_head_in_text":
        # The invented running head is no unit's words, alone or run into another unit.
        head = _norm(mistake.get("after_text"))
        return bool(head) and not any(
            head in _norm(u.get("text")) for u in saved_units if u.get("kind") != "table"
        )

    c0, c1 = _range(mistake, "clean_range")
    p0, p1 = _range(mistake, "planted_range")
    if c1 <= c0:
        return False

    clean_sigs = [_place_signature(u) for u in clean_units]
    saved_sigs = [_place_signature(u) for u in saved_units]
    matcher = difflib.SequenceMatcher(None, clean_sigs, saved_sigs, autojunk=False)
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            mapping[block.a + k] = block.b + k

    # Every clean unit of the mistake is back, in order and side by side.
    if any(t not in mapping for t in range(c0, c1)):
        return False
    placed = [mapping[t] for t in range(c0, c1)]
    if placed != list(range(placed[0], placed[0] + len(placed))):
        return False

    # A heading is back as a heading of the clean rank.
    for t in range(c0, c1):
        clean = clean_units[t]
        if clean.get("kind") == "heading" and clean.get("rank") is not None:
            if saved_units[mapping[t]].get("rank") != clean.get("rank"):
                return False

    # Nothing of the planted mistake is left between the clean neighbours.
    before = max((s for c, s in mapping.items() if c < c0), default=-1)
    after = min((s for c, s in mapping.items() if c >= c1), default=len(saved_units))
    planted_bad = {_place_signature(u) for u in planted_units[p0:p1]} - set(clean_sigs[c0:c1])
    return not any(sig in planted_bad for sig in saved_sigs[before + 1 : after])


def _wrong_changes_on_page(
    act_id: str,
    page: int,
    clean_units: Sequence[Mapping[str, Any]],
    planted_units: Sequence[Mapping[str, Any]],
    saved_units: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Correct units on ``page`` the agent changed, and units it added that neither the clean nor the
    planted page has."""
    clean_counter = Counter(_content_signature(u) for u in clean_units)
    planted_counter = Counter(_content_signature(u) for u in planted_units)
    saved_counter = Counter(_content_signature(u) for u in saved_units)

    untouched = clean_counter & planted_counter
    untouched_missing = untouched - saved_counter

    wrong: list[dict[str, Any]] = []
    missing_count = 0
    for signature, count in sorted(untouched_missing.items(), key=repr):
        for _ in range(count):
            missing_count += 1
            wrong.append(
                {
                    "act_id": act_id,
                    "page": page,
                    "reason": "untouched_unit_changed",
                    "expected": _describe(signature),
                }
            )

    allowed = clean_counter | planted_counter
    unexpected = saved_counter - allowed
    extra_remaining = max(0, sum(unexpected.values()) - missing_count)
    for signature, count in sorted(unexpected.items(), key=repr):
        for _ in range(count):
            if extra_remaining <= 0:
                break
            extra_remaining -= 1
            wrong.append(
                {
                    "act_id": act_id,
                    "page": page,
                    "reason": "unexpected_unit_added",
                    "found": _describe(signature),
                }
            )
    return wrong


def score_revisions(
    key: Mapping[str, Any],
    revisions: Mapping[tuple[str, int], list[dict[str, Any]]],
    *,
    label: str = "",
) -> dict[str, Any]:
    """Compare saved page revisions against a ``plant_mistakes`` answer key."""
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
            for mistake in mistakes:
                if _mistake_fixed(mistake, clean_units, planted_units, saved_units):
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


def open_read_only(url: str) -> Engine:
    """An engine that can only read the workbench database at ``url``: never creates or upgrades
    anything. SQLite files are opened read-only; PostgreSQL sessions are read-only."""
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    if backend == "sqlite":
        database = parsed.database or ""
        if not database or database == ":memory:" or database.startswith("file:"):
            raise QualifyError("Give the SQLite database as sqlite:///path/to/file.db.")
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise QualifyError(f"There is no database file at {path}.")
        uri = f"{path.as_uri()}?mode=ro"
        return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    if backend == "postgresql":
        return create_engine(
            parsed,
            connect_args={"options": "-c default_transaction_read_only=on"},
            pool_pre_ping=True,
        )
    raise QualifyError("The database must be SQLite or PostgreSQL.")


def load_revisions_for_label(
    engine: Engine,
    label: str,
    *,
    author: str | None = None,
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Load the latest saved ``page_revision.units`` per ``(act_id, page)`` for ``label``."""
    inspector = inspect(engine)
    if not inspector.has_table("page_revision"):
        raise QualifyError("The database has no page_revision table, so it is not a review workbench's.")
    columns = {col["name"] for col in inspector.get_columns("page_revision")}
    if "label" not in columns:
        raise QualifyError(
            "The page_revision table has no label column, so saves cannot be told apart by agent label."
        )

    clauses = ["label = :label"]
    params: dict[str, Any] = {"label": label}
    if author:
        clauses.append("author = :author")
        params["author"] = author.lower()
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
    score_cmd.add_argument("--key", required=True, type=Path, help="Answer key JSON from plant_mistakes.py")
    score_cmd.add_argument(
        "--database",
        default=None,
        help="Workbench database URL, read only (defaults to ALVARY_REVIEW_DATABASE_URL)",
    )
    score_cmd.add_argument("--author", default=None, help="Optional author email filter")
    score_cmd.add_argument(
        "--revisions",
        type=Path,
        default=None,
        help="Optional JSON file of saved revisions (alternative to --database)",
    )

    args = parser.parse_args(argv)
    if args.command != "score":
        return 1
    try:
        key = json.loads(args.key.read_text(encoding="utf-8"))
        if args.revisions is not None:
            revisions = load_revisions_file(args.revisions)
        else:
            db_url = args.database or os.environ.get("ALVARY_REVIEW_DATABASE_URL")
            if not db_url:
                parser.error("provide --database, ALVARY_REVIEW_DATABASE_URL, or --revisions")
            engine = open_read_only(db_url)
            try:
                revisions = load_revisions_for_label(engine, args.label, author=args.author)
            finally:
                engine.dispose()
        report = score_revisions(key, revisions, label=args.label)
    except QualifyError as error:
        print(f"qualify: {error}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
