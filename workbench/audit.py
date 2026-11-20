"""Automated quality audit for multi-agent review (Phase 4, section 7).

Provides:
- ``audit_diff``: compares a reviewer's page units against an auditor's units, treating
  word/number/table-cell edits AND paragraph splits, merges, or text boundary shifts as
  ``material``, metadata-only diffs (alignment, heading rank, emphasis, notes) as
  ``minor``, and identical units as ``clean``.
- ``draw_pages``: a random draw of a work's pages, made when the work is released as done and
  recorded then, so nobody can know it in advance; ``[]`` when ``rate == 0``.
- ``evaluate_window``: rolling 100-page pass-mark evaluation with a minimum sample of 25
  pages and an early-stop trigger at 3+ material pages within the first 25.
- ``pace_report``: per-``(author, label)`` inter-save duration report beside page length,
  filtering out ``task == 'audit'`` and flagging full pages saved less than 20 seconds
  apart.
"""

from __future__ import annotations

import difflib
import math
import os
import random
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from statistics import median
from typing import Any

RATE_ENV = "ALVARY_REVIEW_AUDIT_RATE"
MINIMUM_ENV = "ALVARY_REVIEW_AUDIT_MINIMUM"

DEFAULT_RATE = 0.0  # off until the owner sets a rate: 0.05 is about one page in twenty
DEFAULT_MINIMUM = 2
FAST_PAGE_SECONDS = 20.0
FULL_PAGE_WORDS = 80
VERDICTS = {"clean", "minor", "material"}


def audit_config() -> dict[str, Any]:
    """The audit's draw settings. A rate that is no number between 0 and 1, or a minimum that is no
    whole number of 0 or more, is refused in words (the workbench checks them when it starts)."""
    rate_raw = (os.environ.get(RATE_ENV) or "").strip()
    min_raw = (os.environ.get(MINIMUM_ENV) or "").strip()
    try:
        rate = float(rate_raw) if rate_raw else DEFAULT_RATE
    except ValueError:
        rate = math.nan
    if not 0.0 <= rate <= 1.0:  # false for nan as well
        raise ValueError(f"{RATE_ENV} must be a number from 0 to 1, not {rate_raw!r}")
    try:
        minimum = int(min_raw) if min_raw else DEFAULT_MINIMUM
    except ValueError:
        minimum = -1
    if minimum < 0:
        raise ValueError(f"{MINIMUM_ENV} must be a whole number of 0 or more, not {min_raw!r}")
    return {"rate": rate, "minimum": minimum}


def _norm_text(text: str | None) -> str:
    """Words as they are compared: one space between them, and one way of writing an accented letter,
    so the same word typed with a combining accent is not a changed word."""
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def _unit_content(unit: dict[str, Any]) -> tuple:
    """What a unit puts into the published text: a table's caption, cells and merged cells; for anything
    else what it is (a heading, a paragraph, a list item: the published structure is built from these),
    its words, its number or letter (a list or heading marker is printed text: "12." for "13." is a
    changed number), and whether it is joined to the page before, which moves a paragraph's boundary
    and can remove a hyphen. Alignment, emphasis, notes and a heading's rank are not here: they differ
    as "minor"."""
    if unit.get("kind") == "table":
        caption = _norm_text(unit.get("caption"))
        rows = tuple(tuple(_norm_text(cell) for cell in row) for row in (unit.get("rows") or []))
        spans = tuple(sorted(tuple(span) for span in unit.get("spans") or []))  # [row, column, rows, columns]
        return ("table", caption, rows, spans)
    joined = tuple(bool(unit.get(key)) for key in ("continues_previous", "dehyphenate", "keep_hyphen"))
    kind = str(unit.get("kind") or "paragraph")
    return (kind, _norm_text(unit.get("text")), _norm_text(unit.get("marker")), joined)


def _unit_words(units: Sequence[dict[str, Any]]) -> list[str]:
    """All whitespace-delimited words across units (text, table captions, and table cells)."""
    words: list[str] = []
    for unit in units:
        if unit.get("kind") == "table":
            words.extend(_norm_text(unit.get("caption")).split())
            for row in unit.get("rows") or []:
                for cell in row:
                    words.extend(_norm_text(cell).split())
        else:
            words.extend(_norm_text(unit.get("text")).split())
    return words


def audit_diff(
    reviewer_units: Sequence[dict[str, Any]],
    auditor_units: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Compare reviewer and auditor unit lists for a single page.

    Returns ``{paragraphs_changed, words_changed, other_changes, verdict}`` where
    ``verdict`` is one of ``"clean"``, ``"minor"``, or ``"material"``.
    """
    rev = list(reviewer_units)
    aud = list(auditor_units)
    if rev == aud:
        return {
            "paragraphs_changed": 0,
            "words_changed": 0,
            "other_changes": 0,
            "verdict": "clean",
        }

    rev_content = [_unit_content(u) for u in rev]
    aud_content = [_unit_content(u) for u in aud]

    content_matcher = difflib.SequenceMatcher(None, rev_content, aud_content, autojunk=False)
    paragraphs_changed = 0
    other_changes = 0
    for tag, i1, i2, j1, j2 in content_matcher.get_opcodes():
        if tag == "equal":
            for r_unit, a_unit in zip(rev[i1:i2], aud[j1:j2], strict=True):
                if r_unit != a_unit:
                    other_changes += 1
        else:
            paragraphs_changed += max(i2 - i1, j2 - j1)

    rev_words = _unit_words(rev)
    aud_words = _unit_words(aud)
    word_matcher = difflib.SequenceMatcher(None, rev_words, aud_words, autojunk=False)
    words_changed = sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in word_matcher.get_opcodes() if tag != "equal"
    )

    if rev_content != aud_content:
        verdict = "material"
    else:
        verdict = "minor"

    return {
        "paragraphs_changed": paragraphs_changed,
        "words_changed": words_changed,
        "other_changes": other_changes,
        "verdict": verdict,
    }


def draw_pages(
    doc_pages: Mapping[str, Sequence[int]] | Sequence[tuple[str, int]],
    *,
    rate: float = DEFAULT_RATE,
    minimum: int = DEFAULT_MINIMUM,
    rng: random.Random | None = None,
) -> list[tuple[str, int]]:
    """The pages of a work just released as done that a second agent checks: about ``rate`` of them and
    ``minimum`` at least (or all, if there are fewer), chosen at random. The draw is made at release and
    recorded then (review_audit), so no reviewer can know it beforehand, and there is no key to keep.

    ``doc_pages`` may be a mapping ``{act_id: [page, ...]}`` or a sequence of ``(act_id, page)`` pairs.
    Returns a sorted list of ``(act_id, page)`` pairs, ``[]`` when ``rate == 0``.
    """
    if not 0.0 <= rate <= 1.0:
        raise ValueError("audit rate must be between 0 and 1")
    if minimum < 0:
        raise ValueError("audit minimum must be non-negative")
    if rate == 0:
        return []
    if isinstance(doc_pages, Mapping):
        raw_candidates = [(str(act_id), int(page)) for act_id, pages in doc_pages.items() for page in pages]
    else:
        raw_candidates = [(str(act_id), int(page)) for act_id, page in doc_pages]
    candidates = sorted(set(raw_candidates))
    size = min(len(candidates), max(minimum, math.ceil(rate * len(candidates))))
    if size <= 0:
        return []
    return sorted((rng or random.SystemRandom()).sample(candidates, size))


def evaluate_window(
    verdicts: Sequence[str],
    *,
    max_material: int = 2,
    window: int = 100,
    min_sample: int = 25,
    early_stop: int = 3,
) -> dict[str, Any]:
    """Evaluate a label's chronological audit verdicts against the pass mark (design 7.4).

    - Over the mark: more than ``max_material`` material pages (default 2) among the most recent
      ``window`` audited pages (default 100), once ``min_sample`` pages (default 25) have been audited.
    - Before that, only ``early_stop`` or more material pages (default 3) is over the mark.

    The caller leaves out pages the owner decided for the reviewer: there the auditor was the one wrong.
    """
    if max_material < 0:
        raise ValueError("max_material must be 0 or more")
    if window < 1:
        raise ValueError("window must be at least 1")
    if min_sample < 1:
        raise ValueError("min_sample must be at least 1")
    if early_stop < 1:
        raise ValueError("early_stop must be at least 1")

    items = list(verdicts)
    unknown = set(items) - VERDICTS
    if unknown:
        raise ValueError(f"unknown audit verdict(s): {sorted(unknown)}")

    total = len(items)
    recent = items[-window:]
    sample_size = len(recent)
    material_count = sum(1 for v in recent if v == "material")
    minor_count = sum(1 for v in recent if v == "minor")
    clean_count = sum(1 for v in recent if v == "clean")
    material_rate = (material_count / sample_size) if sample_size else 0.0

    early_stopped = False
    breached = False
    if total < min_sample:
        if material_count >= early_stop:
            early_stopped = True
            breached = True
            status = "breached"
        else:
            status = "insufficient_sample"
    else:
        if material_count > max_material:
            breached = True
            status = "breached"
        else:
            status = "pass"

    return {
        "status": status,
        "breached": breached,
        "early_stopped": early_stopped,
        "total_audited": total,
        "window_size": sample_size,
        "material": material_count,
        "minor": minor_count,
        "clean": clean_count,
        "material_rate": round(material_rate, 6),
    }


def _parse_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _revision_word_count(revision: Mapping[str, Any]) -> int:
    if "words" in revision and revision["words"] is not None:
        return int(revision["words"])
    return len(_unit_words(revision.get("units") or []))


def pace_report(
    revisions: Sequence[Mapping[str, Any]],
    *,
    fast_seconds: float = FAST_PAGE_SECONDS,
    full_page_words: int = FULL_PAGE_WORDS,
) -> list[dict[str, Any]]:
    """Compute per-``(author, label)`` inter-save durations beside page length.

    Revisions with ``task == "audit"`` are excluded. Full pages (at least
    ``full_page_words`` words) saved less than ``fast_seconds`` apart are flagged.
    """
    grouped: dict[tuple[str, str], list[tuple[datetime, Mapping[str, Any]]]] = {}
    for rev in revisions:
        if rev.get("task") == "audit":
            continue
        author = str(rev.get("author") or "")
        label = str(rev.get("label") or "")
        ts = _parse_timestamp(rev["created_at"])
        grouped.setdefault((author, label), []).append((ts, rev))

    report: list[dict[str, Any]] = []
    for (author, label), entries in sorted(grouped.items()):
        entries.sort(key=lambda item: item[0])
        word_counts = [_revision_word_count(rev) for _, rev in entries]
        intervals: list[dict[str, Any]] = []
        durations: list[float] = []
        fast_full_pages = 0

        for idx in range(1, len(entries)):
            prev_ts, _ = entries[idx - 1]
            curr_ts, curr_rev = entries[idx]
            seconds = (curr_ts - prev_ts).total_seconds()
            words = word_counts[idx]
            is_fast = words >= full_page_words and seconds < fast_seconds
            if is_fast:
                fast_full_pages += 1
            durations.append(seconds)
            intervals.append(
                {
                    "act_id": curr_rev.get("act_id"),
                    "page": curr_rev.get("page"),
                    "words": words,
                    "seconds": round(seconds, 3),
                    "fast": is_fast,
                }
            )

        report.append(
            {
                "author": author,
                "label": label,
                "pages_saved": len(entries),
                "median_seconds": round(median(durations), 3) if durations else None,
                "min_seconds": round(min(durations), 3) if durations else None,
                "mean_words": round(sum(word_counts) / len(word_counts), 1) if word_counts else 0.0,
                "fast_full_pages": fast_full_pages,
                "flagged": fast_full_pages > 0,
                "intervals": intervals,
            }
        )
    return report
