"""Deterministic mistake planter for the multi-agent review qualifying test (section 7.1).

Plants known mistakes into reading JSON files and writes a manifest key describing
each planted mistake alongside the clean reference units for every page, so
``python -m workbench.qualify score --label ...`` can measure mistakes found,
mistakes missed, and correct text changed wrongly.

Usage:
    python scripts/plant_mistakes.py --input DIR --output DIR [--key PATH] [--seed 42]
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
import shutil
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from workbench import content  # noqa: E402

# Ordered from most specific (rarest eligible nodes) to most general so one planter
# does not consume the only eligible node for a rarer kind.
MISTAKE_KINDS: tuple[str, ...] = (
    "dropped_accent",
    "flattened_list",
    "changed_digit",
    "heading_run_into_paragraph",
    "merged_paragraphs",
    "split_paragraph",
    "line_end_hyphen",
    "running_head_in_text",
)

_LONG_WORD_RE = re.compile(r"\b[A-Za-z]{7,}\b")
_WORD_RE = re.compile(r"\b[^\W\d_]+\b", re.UNICODE)
_NUMBER_RE = re.compile(r"\b\d+\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _strip_accents(word: str) -> str:
    decomposed = unicodedata.normalize("NFD", word)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _shift_used(used: set[int], at: int, delta: int) -> set[int]:
    return {idx + delta if idx >= at else idx for idx in used}


def _plant_merged_paragraphs(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    candidates = [
        i
        for i in range(len(nodes) - 1)
        if i not in used
        and (i + 1) not in used
        and nodes[i].get("kind") == "paragraph"
        and nodes[i + 1].get("kind") == "paragraph"
        and _norm(nodes[i].get("text"))
        and _norm(nodes[i + 1].get("text"))
    ]
    if not candidates:
        return None, used
    idx = rng.choice(candidates)
    first_text = _norm(nodes[idx]["text"])
    second_text = _norm(nodes[idx + 1]["text"])
    merged_text = f"{first_text} {second_text}"
    merged = copy.deepcopy(nodes[idx])
    merged["text"] = merged_text
    merged.pop("marks", None)
    nodes[idx : idx + 2] = [merged]
    new_used = _shift_used(used, idx + 2, -1)
    new_used.add(idx)
    return (
        {
            "kind": "merged_paragraphs",
            "page": page_num,
            "description": "Two adjacent paragraphs were merged into one.",
            "before_texts": [first_text, second_text],
            "after_text": merged_text,
        },
        new_used,
    )


def _plant_split_paragraph(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    candidates = [
        i
        for i, node in enumerate(nodes)
        if i not in used and node.get("kind") == "paragraph" and len(_norm(node.get("text")).split()) >= 6
    ]
    if not candidates:
        return None, used
    idx = rng.choice(candidates)
    orig_text = _norm(nodes[idx]["text"])
    sentences = _SENTENCE_SPLIT_RE.split(orig_text)
    if len(sentences) >= 2:
        mid = len(sentences) // 2
        part_a = " ".join(sentences[:mid]).strip()
        part_b = " ".join(sentences[mid:]).strip()
    else:
        words = orig_text.split()
        mid = len(words) // 2
        part_a = " ".join(words[:mid])
        part_b = " ".join(words[mid:])

    first = copy.deepcopy(nodes[idx])
    first["text"] = part_a
    first.pop("marks", None)
    second = {"kind": "paragraph", "text": part_b}
    nodes[idx : idx + 1] = [first, second]
    new_used = _shift_used(used, idx + 1, 1)
    new_used.update({idx, idx + 1})
    return (
        {
            "kind": "split_paragraph",
            "page": page_num,
            "description": "A single paragraph was split into two paragraphs.",
            "before_text": orig_text,
            "after_texts": [part_a, part_b],
        },
        new_used,
    )


def _plant_heading_run_into_paragraph(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    candidates = [
        i
        for i in range(len(nodes) - 1)
        if i not in used
        and (i + 1) not in used
        and nodes[i].get("kind") == "heading"
        and nodes[i + 1].get("kind") == "paragraph"
        and _norm(nodes[i].get("text"))
        and _norm(nodes[i + 1].get("text"))
    ]
    if not candidates:
        return None, used
    idx = rng.choice(candidates)
    heading_text = _norm(nodes[idx]["text"])
    para_text = _norm(nodes[idx + 1]["text"])
    merged_text = f"{heading_text} {para_text}"
    merged = copy.deepcopy(nodes[idx + 1])
    merged["kind"] = "paragraph"
    merged["text"] = merged_text
    merged.pop("marks", None)
    nodes[idx : idx + 2] = [merged]
    new_used = _shift_used(used, idx + 2, -1)
    new_used.add(idx)
    return (
        {
            "kind": "heading_run_into_paragraph",
            "page": page_num,
            "description": "A heading was merged into the following paragraph.",
            "before_texts": [heading_text, para_text],
            "after_text": merged_text,
        },
        new_used,
    )


def _plant_line_end_hyphen(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    candidates: list[tuple[int, re.Match[str]]] = []
    for i, node in enumerate(nodes):
        if i in used or node.get("kind") not in {"paragraph", "list_item"}:
            continue
        text = _norm(node.get("text"))
        matches = list(_LONG_WORD_RE.finditer(text))
        if matches:
            candidates.append((i, rng.choice(matches)))
    if not candidates:
        return None, used
    idx, match = rng.choice(candidates)
    orig_text = _norm(nodes[idx]["text"])
    word = match.group(0)
    mid = len(word) // 2
    hyphenated = f"{word[:mid]}-{word[mid:]}"
    mutated = orig_text[: match.start()] + hyphenated + orig_text[match.end() :]
    nodes[idx]["text"] = mutated
    nodes[idx].pop("marks", None)
    used = set(used) | {idx}
    return (
        {
            "kind": "line_end_hyphen",
            "page": page_num,
            "description": f"Spurious line-end hyphen left in '{word}' -> '{hyphenated}'.",
            "before_text": orig_text,
            "after_text": mutated,
            "before_token": word,
            "after_token": hyphenated,
        },
        used,
    )


def _plant_dropped_accent(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    candidates: list[tuple[int, re.Match[str], str]] = []
    for i, node in enumerate(nodes):
        if i in used or node.get("kind") not in {"paragraph", "heading", "list_item"}:
            continue
        text = _norm(node.get("text"))
        for match in _WORD_RE.finditer(text):
            word = match.group(0)
            stripped = _strip_accents(word)
            if stripped != word:
                candidates.append((i, match, stripped))
    if not candidates:
        return None, used
    idx, match, stripped = rng.choice(candidates)
    orig_text = _norm(nodes[idx]["text"])
    word = match.group(0)
    mutated = orig_text[: match.start()] + stripped + orig_text[match.end() :]
    nodes[idx]["text"] = mutated
    nodes[idx].pop("marks", None)
    used = set(used) | {idx}
    return (
        {
            "kind": "dropped_accent",
            "page": page_num,
            "description": f"Diacritic dropped from '{word}' -> '{stripped}'.",
            "before_text": orig_text,
            "after_text": mutated,
            "before_token": word,
            "after_token": stripped,
        },
        used,
    )


def _plant_running_head_in_text(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    del rng
    header_text = f"Official Gazette — Page {page_num}"
    nodes.insert(0, {"kind": "paragraph", "text": header_text})
    new_used = _shift_used(used, 0, 1)
    new_used.add(0)
    return (
        {
            "kind": "running_head_in_text",
            "page": page_num,
            "description": "A running page header was left in the body text.",
            "before_text": "",
            "after_text": header_text,
        },
        new_used,
    )


def _plant_changed_digit(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    candidates: list[tuple[int, re.Match[str]]] = []
    for i, node in enumerate(nodes):
        if i in used or node.get("kind") not in {"paragraph", "list_item", "heading"}:
            continue
        text = _norm(node.get("text"))
        matches = list(_NUMBER_RE.finditer(text))
        if matches:
            candidates.append((i, rng.choice(matches)))
    if not candidates:
        return None, used
    idx, match = rng.choice(candidates)
    orig_text = _norm(nodes[idx]["text"])
    token = match.group(0)
    last_digit = str((int(token[-1]) + 1) % 10)
    mutated_token = token[:-1] + last_digit
    mutated = orig_text[: match.start()] + mutated_token + orig_text[match.end() :]
    nodes[idx]["text"] = mutated
    nodes[idx].pop("marks", None)
    used = set(used) | {idx}
    return (
        {
            "kind": "changed_digit",
            "page": page_num,
            "description": f"Digit altered from '{token}' -> '{mutated_token}'.",
            "before_text": orig_text,
            "after_text": mutated,
            "before_token": token,
            "after_token": mutated_token,
        },
        used,
    )


def _plant_flattened_list(
    nodes: list[dict[str, Any]], used: set[int], rng: random.Random, page_num: int
) -> tuple[dict[str, Any] | None, set[int]]:
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(nodes):
        if i not in used and nodes[i].get("kind") == "list_item" and _norm(nodes[i].get("text")):
            start = i
            while (
                i < len(nodes)
                and i not in used
                and nodes[i].get("kind") == "list_item"
                and _norm(nodes[i].get("text"))
            ):
                i += 1
            if i - start >= 2:
                runs.append((start, i))
        else:
            i += 1
    if not runs:
        return None, used
    start, end = rng.choice(runs)
    items = nodes[start:end]
    before_texts = [_norm(item.get("text")) for item in items]
    parts = [
        f"{item['marker']} {_norm(item['text'])}" if item.get("marker") else _norm(item["text"])
        for item in items
    ]
    flattened_text = _norm(" ".join(parts))
    nodes[start:end] = [{"kind": "paragraph", "text": flattened_text}]
    removed = (end - start) - 1
    new_used = _shift_used(used, end, -removed)
    new_used.add(start)
    return (
        {
            "kind": "flattened_list",
            "page": page_num,
            "description": f"{len(items)} list items were flattened into a single paragraph.",
            "before_texts": before_texts,
            "after_text": flattened_text,
        },
        new_used,
    )


_PLANTERS = {
    "merged_paragraphs": _plant_merged_paragraphs,
    "split_paragraph": _plant_split_paragraph,
    "heading_run_into_paragraph": _plant_heading_run_into_paragraph,
    "line_end_hyphen": _plant_line_end_hyphen,
    "dropped_accent": _plant_dropped_accent,
    "running_head_in_text": _plant_running_head_in_text,
    "changed_digit": _plant_changed_digit,
    "flattened_list": _plant_flattened_list,
}


def plant_on_reading(
    act_id: str,
    reading: dict[str, Any],
    *,
    seed: int = 42,
    kinds: Sequence[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Plant up to one mistake of each requested kind across ``reading`` deterministically.

    Returns ``(mutated_reading, doc_key)`` where ``doc_key`` maps string page numbers to
    ``{"clean_units": [...], "planted_units": [...], "mistakes": [...]}``.
    """
    selected = list(MISTAKE_KINDS if kinds is None else kinds)
    unknown = set(selected) - set(MISTAKE_KINDS)
    if unknown:
        raise ValueError(f"unknown mistake kind(s): {sorted(unknown)}")

    rng = random.Random(f"{seed}:{act_id}")
    original_pages = {p["page"]: content.units_from(p) for p in reading.get("pages", [])}
    mutated = copy.deepcopy(reading)

    pages = mutated.get("pages", [])
    used_by_page: dict[int, set[int]] = {p["page"]: set() for p in pages}
    mistakes_by_page: dict[int, list[dict[str, Any]]] = {p["page"]: [] for p in pages}

    page_order = list(range(len(pages)))
    counter = 0
    for kind in selected:
        planter = _PLANTERS[kind]
        indices = list(page_order)
        rng.shuffle(indices)
        for p_idx in indices:
            page = pages[p_idx]
            page_num = int(page["page"])
            record, new_used = planter(page["nodes"], used_by_page[page_num], rng, page_num)
            if record is not None:
                counter += 1
                used_by_page[page_num] = new_used
                mistakes_by_page[page_num].append(
                    {
                        "id": f"{act_id}:p{page_num}:{kind}:{counter}",
                        "act_id": act_id,
                        **record,
                    }
                )
                break

    doc_key: dict[str, Any] = {}
    for page in pages:
        page_num = int(page["page"])
        doc_key[str(page_num)] = {
            "clean_units": original_pages[page_num],
            "planted_units": content.units_from(page),
            "mistakes": mistakes_by_page[page_num],
        }
    return mutated, doc_key


def plant_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    key_path: Path | None = None,
    seed: int = 42,
    kinds: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Plant mistakes across every ``*.reading.json`` file in ``input_dir``.

    Writes mutated readings to ``output_dir`` (copying ``manifest.json`` if present)
    and writes the manifest key JSON to ``key_path`` (default ``output_dir / 'qualify-key.json'``).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    key_target = key_path or (output_dir / "qualify-key.json")

    manifest_src = input_dir / "manifest.json"
    if manifest_src.exists():
        shutil.copy2(manifest_src, output_dir / "manifest.json")

    documents: dict[str, Any] = {}
    total_mistakes = 0
    reading_paths = sorted(input_dir.glob("*.reading.json"))
    for path in reading_paths:
        act_id = path.name.removesuffix(".reading.json")
        reading = json.loads(path.read_text(encoding="utf-8"))
        mutated, doc_key = plant_on_reading(act_id, reading, seed=seed, kinds=kinds)
        (output_dir / path.name).write_text(
            json.dumps(mutated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        documents[act_id] = doc_key
        total_mistakes += sum(len(entry["mistakes"]) for entry in doc_key.values())

    key_data = {
        "version": 1,
        "seed": seed,
        "kinds": list(MISTAKE_KINDS if kinds is None else kinds),
        "total_mistakes": total_mistakes,
        "documents": documents,
    }
    key_target.parent.mkdir(parents=True, exist_ok=True)
    key_target.write_text(json.dumps(key_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return key_data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="Directory holding clean *.reading.json files"
    )
    parser.add_argument("--output", required=True, type=Path, help="Directory to write mutated readings")
    parser.add_argument("--key", type=Path, default=None, help="Path for the manifest key JSON")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic RNG seed (default: 42)")
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=MISTAKE_KINDS,
        default=None,
        help="Subset of mistake kinds to plant (default: all)",
    )
    args = parser.parse_args(argv)
    key_data = plant_directory(args.input, args.output, key_path=args.key, seed=args.seed, kinds=args.kinds)
    print(json.dumps({"documents": len(key_data["documents"]), "total_mistakes": key_data["total_mistakes"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
