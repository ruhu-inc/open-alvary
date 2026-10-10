"""Acts, machine readings and reviewed content for the workbench.

A page's machine reading becomes a list of editable units. A reviewer's page revision replaces
that list wholesale; the final content of an Act is, page by page, the latest revision if there
is one and otherwise the machine reading. Flags come from the reconciled reading and are
identified by page, position and text, so a resolution survives reloads.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from ingestion import first_collection, layout_parser
from workbench.store import canonical_json

KINDS = {"heading", "paragraph", "list_item", "table"}
MARK_TYPES = {"bold", "italic", "underline", "superscript", "subscript"}
MATERIALS = {
    "operative_tables": "Tables match the gazette cell by cell",
    "amounts": "Amounts and figures match",
    "dates": "Dates match",
    "negations": "Negations (not, no, unless, except) are intact",
    "cross_references": "Section and Act cross-references match",
    "identity_citation": "Title, number, year and citation match",
}


class Library:
    """The collection's Acts and their reconciled readings in private staging."""

    def __init__(self, staging: Path | None = None):
        self.staging = Path(staging or os.environ.get("ALVARY_REVIEW_STAGING") or first_collection.STAGING)
        self.manifest = first_collection.load_manifest()

    def documents(self) -> list[dict]:
        """Documents of any type in the collection that have a reconciled reading."""
        return [
            {
                **document,
                "jurisdiction": self.manifest.get("jurisdiction"),
                "collection": self.manifest.get("collection"),
            }
            for document in self.manifest["documents"]
            if (self.staging / f"{document['id']}.reading.json").exists()
        ]

    def act(self, act_id: str) -> dict:
        found = next((a for a in self.documents() if a["id"] == act_id), None)
        if found is None:
            raise KeyError(act_id)
        return found

    def reading(self, act_id: str) -> dict:
        self.act(act_id)
        return json.loads((self.staging / f"{act_id}.reading.json").read_text())

    def image(self, act_id: str, page: int) -> Path:
        """A legible page image (150 dpi), rendered once in the sealed tools container."""
        self.act(act_id)
        folder = self.staging / "review-images" / act_id
        path = folder / f"page-{page}.jpg"
        if not path.exists():
            if not shutil.which("docker"):
                raise FileNotFoundError(
                    f"page image not prepared: run python -m workbench.prepare --act {act_id}"
                )
            folder.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp",
                    "--cap-drop",
                    "ALL",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    f"{self.staging}:/in:ro",
                    "-v",
                    f"{folder}:/out",
                    first_collection.OCR_IMAGE,
                    f"pdftoppm -r 150 -jpeg -jpegopt quality=85 -singlefile -f {page} -l {page} "
                    f"/in/{act_id}.pdf /out/page-{page}",
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        return path


def regions(staging: Path, act_id: str, page: int) -> list[dict]:
    """Paragraph boxes on the page image (fractions of the page), from the positional check reader."""
    path = staging / "readings" / act_id / "azure.json"
    if not path.exists():
        return []
    analyze = json.loads(path.read_text()).get("raw", {}).get("analyze") or {}
    size = next(
        (
            (p.get("width"), p.get("height"))
            for p in analyze.get("pages") or []
            if p.get("pageNumber") == page
        ),
        None,
    )
    if not size or not size[0] or not size[1]:
        return []
    out = []
    for paragraph in analyze.get("paragraphs") or []:
        for region in paragraph.get("boundingRegions") or []:
            polygon = region.get("polygon") or []
            if region.get("pageNumber") != page or len(polygon) < 8:
                continue
            xs, ys = polygon[0::2], polygon[1::2]
            out.append(
                {
                    "text": paragraph.get("content", "")[:300],
                    "role": paragraph.get("role"),
                    "box": [min(xs) / size[0], min(ys) / size[1], max(xs) / size[0], max(ys) / size[1]],
                }
            )
    return out


def lines(staging: Path, act_id: str, page: int) -> list[dict]:
    """Printed lines on the page image (fractions of the page), from the positional check reader."""
    path = staging / "readings" / act_id / "azure.json"
    if not path.exists():
        return []
    analyze = json.loads(path.read_text()).get("raw", {}).get("analyze") or {}
    found = next((p for p in analyze.get("pages") or [] if p.get("pageNumber") == page), None)
    if not found or not found.get("width") or not found.get("height"):
        return []
    out = []
    for line in found.get("lines") or []:
        polygon = line.get("polygon") or []
        if len(polygon) < 8:
            continue
        xs, ys = polygon[0::2], polygon[1::2]
        out.append(
            {
                "text": line.get("content", "")[:300],
                "box": [
                    min(xs) / found["width"],
                    min(ys) / found["height"],
                    max(xs) / found["width"],
                    max(ys) / found["height"],
                ],
            }
        )
    return out


# --- Act-wide find and replace ------------------------------------------------------------------


def _pattern(find: str, match_case: bool, whole_word: bool) -> re.Pattern:
    if not find or len(find) > 200:
        raise ValueError("search for 1 to 200 characters")
    body = re.escape(find)
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    return re.compile(body, 0 if match_case else re.IGNORECASE)


def _fields(unit: dict):
    """(field name, text) pairs a search covers, in reading order."""
    if unit["kind"] == "table":
        yield "caption", unit.get("caption", "")
        for r, row in enumerate(unit["rows"]):
            for c, cell in enumerate(row):
                yield f"cell:{r}:{c}", cell
        return
    yield "text", unit.get("text", "")
    for n, note in enumerate(unit.get("notes") or []):
        yield f"note:{n}", note


def search(
    pages: list[dict], find: str, match_case: bool = False, whole_word: bool = False, limit: int = 500
) -> list[dict]:
    pattern, hits = _pattern(find, match_case, whole_word), []
    for page in pages:
        for u, unit in enumerate(page["units"]):
            for field, text in _fields(unit):
                for match in pattern.finditer(text):
                    start, end = match.span()
                    hits.append(
                        {
                            "page": page["page"],
                            "unit": u,
                            "field": field,
                            "before": text[max(0, start - 50) : start],
                            "match": match.group(),
                            "after": text[end : end + 50],
                        }
                    )
                    if len(hits) >= limit:
                        return hits
    return hits


def replace_in(
    units: list[dict], find: str, replacement: str, match_case: bool, whole_word: bool
) -> tuple[list[dict], int]:
    """A copy of the units with every match replaced, and how many were replaced."""
    if len(replacement) > 200:
        raise ValueError("replace with at most 200 characters")
    pattern, total, out = _pattern(find, match_case, whole_word), 0, []
    for unit in units:
        unit = json.loads(json.dumps(unit))
        if unit["kind"] == "table":
            unit["caption"], count = pattern.subn(lambda _: replacement, unit.get("caption", ""))
            total += count
            for row in unit["rows"]:
                for c, cell in enumerate(row):
                    row[c], count = pattern.subn(lambda _: replacement, cell)
                    total += count
        else:
            unit["text"], count = pattern.subn(lambda _: replacement, unit.get("text", ""))
            total += count
            notes = []
            for note in unit.get("notes") or []:
                note, count = pattern.subn(lambda _: replacement, note)
                total += count
                notes.append(note)
            if notes:
                unit["notes"] = notes
        out.append(unit)
    return out, total


def units_from(page: dict) -> list[dict]:
    """Editable units from a reconciled page, in reading order."""
    units = []
    for node in page["nodes"]:
        if node["kind"] == "furniture":
            continue
        if node["kind"] == "table":
            rows = [[cell["text"] for cell in row] for row in node["header_rows"] + node["body_rows"]]
            units.append(
                {
                    "kind": "table",
                    "caption": node.get("caption", ""),
                    "header_rows": len(node["header_rows"]),
                    "rows": rows,
                }
            )
            continue
        unit = {"kind": node["kind"] if node["kind"] in KINDS else "paragraph", "text": node.get("text", "")}
        if node.get("marker"):
            unit["marker"] = node["marker"]
            unit["marker_source"] = node.get("marker_source")
        if node.get("notes"):
            unit["notes"] = list(node["notes"])
        units.append(unit)
    return units


def flag_id(page: int, flag: dict) -> str:
    raw = f"{page}:{flag['kind']}:{flag['tokens'][0]}:{flag['tokens'][1]}:{flag['primary']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def page_flags(page: dict) -> list[dict]:
    return [{**flag, "id": flag_id(page["page"], flag)} for flag in page["flags"]]


def reading_sha256(page: dict) -> str:
    return hashlib.sha256(
        canonical_json({"nodes": page["nodes"], "flags": page["flags"]}).encode()
    ).hexdigest()


def validate_units(units) -> list[dict]:
    """Accept only the unit shapes the corpus can hold; bounded so a request cannot balloon."""
    if not isinstance(units, list) or len(units) > 400:
        raise ValueError("units must be a list of at most 400")
    clean = []
    for unit in units:
        if not isinstance(unit, dict) or unit.get("kind") not in KINDS:
            raise ValueError("every unit needs a kind among heading, paragraph, list_item, table")
        if unit["kind"] == "table":
            rows = unit.get("rows")
            if not isinstance(rows, list) or not rows or len(rows) > 100:
                raise ValueError("a table needs 1 to 100 rows")
            if any(
                not isinstance(r, list)
                or len(r) > 100
                or not all(isinstance(c, str) and len(c) <= 8192 for c in r)
                for r in rows
            ):
                raise ValueError("table rows must be lists of at most 100 text cells")
            clean.append(
                {
                    "kind": "table",
                    "caption": str(unit.get("caption", ""))[:2000],
                    "header_rows": max(0, min(int(unit.get("header_rows", 0)), len(rows))),
                    "rows": rows,
                }
            )
            continue
        text = unit.get("text", "")
        if not isinstance(text, str) or len(text) > 8192:
            raise ValueError("unit text must be at most 8192 characters")
        item = {"kind": unit["kind"], "text": text}
        level = unit.get("level")
        if level is not None:
            if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 3:
                raise ValueError("level must be 0 (section) to 3 (sub-paragraph)")
            item["level"] = level
        marks = unit.get("marks") or []
        if not isinstance(marks, list) or len(marks) > 200:
            raise ValueError("marks must be a list of at most 200 ranges")
        clean_marks = []
        for mark in marks:
            if (
                not isinstance(mark, dict)
                or mark.get("type") not in MARK_TYPES
                or not all(
                    isinstance(mark.get(k), int) and not isinstance(mark.get(k), bool)
                    for k in ("start", "end")
                )
                or not 0 <= mark["start"] < mark["end"] <= len(text)
            ):
                raise ValueError("each mark needs a type and a range within the text")
            clean_marks.append({"type": mark["type"], "start": mark["start"], "end": mark["end"]})
        if clean_marks:
            item["marks"] = clean_marks
        align = unit.get("align")
        if align is not None:
            if align not in {"left", "center", "right"}:
                raise ValueError("align must be left, center or right")
            item["align"] = align
        if unit.get("marker"):
            item["marker"] = str(unit["marker"])[:40]
            if unit.get("marker_source"):
                item["marker_source"] = unit["marker_source"]
        notes = unit.get("notes") or []
        if (
            not isinstance(notes, list)
            or len(notes) > 10
            or not all(isinstance(n, str) and len(n) <= 1000 for n in notes)
        ):
            raise ValueError("notes must be at most 10 short texts")
        if notes:
            item["notes"] = notes
        clean.append(item)
    return clean


def final_pages(reading: dict, latest: dict[int, dict]) -> list[dict]:
    """Each page's units: the latest reviewed revision, else the machine reading."""
    pages = []
    for page in reading["pages"]:
        revision = latest.get(page["page"])
        pages.append(
            {
                "page": page["page"],
                "units": revision["units"] if revision else units_from(page),
                "revision": revision["revision"] if revision else 0,
            }
        )
    return pages


def content_sha256(pages: list[dict]) -> str:
    return hashlib.sha256(
        canonical_json([{"page": p["page"], "units": p["units"]} for p in pages]).encode()
    ).hexdigest()


def plain_text(pages: list[dict]) -> str:
    """Readable text of the final content; margin notes follow their unit in brackets."""
    lines = []
    for page in pages:
        for unit in page["units"]:
            if unit["kind"] == "table":
                if unit.get("caption"):
                    lines.append(unit["caption"])
                lines.extend("\t".join(row) for row in unit["rows"])
                continue
            text = f"{unit['marker']} {unit['text']}" if unit.get("marker") else unit["text"]
            lines.append(text)
            lines.extend(f"[Margin note: {note}]" for note in unit.get("notes", []))
    return "\n".join(lines) + "\n"


def readiness(reading: dict, latest: dict[int, dict]) -> dict:
    """What still stands between this Act and approval."""
    unchecked, open_high, stale = [], [], []
    for page in reading["pages"]:
        revision = latest.get(page["page"])
        if not revision or revision["status"] != "checked":
            unchecked.append(page["page"])
        elif revision["reading_sha256"] != reading_sha256(page):
            stale.append(page["page"])
        resolved = revision["resolutions"] if revision else {}
        for flag in page_flags(page):
            if flag["severity"] == "high" and flag["id"] not in resolved:
                open_high.append({"page": page["page"], "id": flag["id"]})
    missing = reading["summary"].get("missing_checks") or []
    return {
        "pages": len(reading["pages"]),
        "unchecked_pages": unchecked,
        "open_high_flags": open_high,
        "stale_pages": stale,
        "missing_checks": missing,
        "ready": not unchecked and not open_high and not stale and not missing,
    }


def machine_html(page: dict) -> str:
    """The machine reading with its flags highlighted, as in the static review pack."""
    return first_collection.reconciled_body(page)[1]


__all__ = ["Library", "MATERIALS", "layout_parser"]
