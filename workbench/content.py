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


COLLECTION_ID = re.compile(r"[a-z]{2}-[a-z0-9-]{3,120}")


def extra_collections() -> list[tuple[dict, Path]]:
    """Further collections to review beside the first, each a folder holding its manifest.json and
    its documents' readings and PDFs, named in ALVARY_REVIEW_COLLECTIONS (separated by the path
    separator). A document id must be a plain slug, so no file outside its folder is reached."""
    out = []
    for folder in filter(None, os.environ.get("ALVARY_REVIEW_COLLECTIONS", "").split(os.pathsep)):
        staging = Path(folder)
        manifest = json.loads((staging / "manifest.json").read_text())
        for document in manifest["documents"]:
            if not COLLECTION_ID.fullmatch(document["id"]):
                raise ValueError(f"Invalid document id in {staging}: {document['id']!r}")
        out.append((manifest, staging))
    return out


def render_fonts() -> list[str]:
    """Fonts lent to the renderer, read-only: PDFs that do not embed their fonts (Times New Roman,
    Arial) render almost blank without them. ALVARY_RENDER_FONTS names a folder of font files;
    by default private-staging/fonts is used when present."""
    folder = os.environ.get("ALVARY_RENDER_FONTS") or str(first_collection.ROOT / "private-staging" / "fonts")
    return ["-v", f"{folder}:/usr/share/fonts/lent:ro"] if Path(folder).is_dir() else []


class Library:
    """The collections' documents and their reconciled readings in private staging."""

    def __init__(self, staging: Path | None = None):
        self.staging = Path(staging or os.environ.get("ALVARY_REVIEW_STAGING") or first_collection.STAGING)
        self.manifest = first_collection.load_manifest()
        self.collections = [(self.manifest, self.staging), *extra_collections()]

    def documents(self) -> list[dict]:
        """Documents of any type in the collections that have a reconciled reading."""
        return [
            {
                **document,
                "jurisdiction": document.get("jurisdiction") or manifest.get("jurisdiction"),
                "collection": manifest.get("collection"),
            }
            for manifest, staging in self.collections
            for document in manifest["documents"]
            if (staging / f"{document['id']}.reading.json").exists()
        ]

    def _where(self, act_id: str) -> tuple[dict, Path]:
        for manifest, staging in self.collections:
            if any(document["id"] == act_id for document in manifest["documents"]):
                return manifest, staging
        raise KeyError(act_id)

    def manifest_for(self, act_id: str) -> dict:
        return self._where(act_id)[0]

    def staging_for(self, act_id: str) -> Path:
        return self._where(act_id)[1]

    def act(self, act_id: str) -> dict:
        found = next((a for a in self.documents() if a["id"] == act_id), None)
        if found is None:
            raise KeyError(act_id)
        return found

    def provenance(self, act_id: str) -> dict:
        """How and when the original file was retrieved (written by the fetch step)."""
        path = self.staging_for(act_id) / f"{act_id}.provenance.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def original(self, act_id: str) -> Path:
        self.act(act_id)
        path = self.staging_for(act_id) / f"{act_id}.pdf"
        if not path.exists():
            raise FileNotFoundError(act_id)
        return path

    def reading(self, act_id: str) -> dict:
        self.act(act_id)
        return json.loads((self.staging_for(act_id) / f"{act_id}.reading.json").read_text())

    def image(self, act_id: str, page: int) -> Path:
        """A legible page image (150 dpi), rendered once in the sealed tools container."""
        self.act(act_id)
        staging = self.staging_for(act_id)
        folder = staging / "review-images" / act_id
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
                    "-e",
                    "HOME=/tmp",
                    *render_fonts(),
                    "-v",
                    f"{staging}:/in:ro",
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


def _from_reading(staging: Path, act_id: str, page: int, key: str) -> list[dict]:
    """Boxes a reading carries itself (a text-layer reading knows where every unit and line is)."""
    path = staging / f"{act_id}.reading.json"
    if not path.exists():
        return []
    found = next((p for p in json.loads(path.read_text())["pages"] if p["page"] == page), None)
    return list(found.get(key) or []) if found else []


def regions(staging: Path, act_id: str, page: int) -> list[dict]:
    """Paragraph boxes on the page image (fractions of the page), from the positional check reader,
    or from the reading itself when it was taken from the text layer."""
    path = staging / "readings" / act_id / "azure.json"
    if not path.exists():
        return _from_reading(staging, act_id, page, "regions")
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
    """Printed lines on the page image (fractions of the page), from the positional check reader,
    or from the reading itself when it was taken from the text layer."""
    path = staging / "readings" / act_id / "azure.json"
    if not path.exists():
        return _from_reading(staging, act_id, page, "lines")
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


def table_grid(cells_by_row: list[list[dict]]) -> tuple[list[list[str]], list[list[int]]]:
    """A table's cells as a full grid, every column lined up: a merged cell's text in its first
    position, the positions it covers left empty, and the merges listed as [row, column, rows,
    columns] so that they are kept, shown and published as printed."""
    taken: set[tuple[int, int]] = set()
    placed: dict[tuple[int, int], str] = {}
    spans: list[list[int]] = []
    for r, row in enumerate(cells_by_row):
        c = 0
        for cell in row:
            while (r, c) in taken:
                c += 1
            rows_over, columns_over = (
                max(1, int(cell.get("row_span", 1))),
                max(1, int(cell.get("col_span", 1))),
            )
            placed[(r, c)] = cell["text"]
            for dr in range(rows_over):
                for dc in range(columns_over):
                    taken.add((r + dr, c + dc))
            if rows_over > 1 or columns_over > 1:
                spans.append([r, c, rows_over, columns_over])
            c += columns_over
    height = len(cells_by_row)
    width = max((c for _, c in taken), default=-1) + 1
    grid = [[placed.get((r, c), "") for c in range(width)] for r in range(height)]
    spans = [[r, c, min(rs, height - r), cs] for r, c, rs, cs in spans]
    return grid, spans


def units_from(page: dict) -> list[dict]:
    """Editable units from a reconciled page, in reading order."""
    units = []
    for node in page["nodes"]:
        if node["kind"] == "furniture":
            continue
        if node["kind"] == "table":
            rows, spans = table_grid(node["header_rows"] + node["body_rows"])
            unit = {
                "kind": "table",
                "caption": node.get("caption", ""),
                "header_rows": len(node["header_rows"]),
                "rows": rows,
            }
            if spans:
                unit["spans"] = spans
            units.append(unit)
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
            table = {
                "kind": "table",
                "caption": str(unit.get("caption", ""))[:2000],
                "header_rows": max(0, min(int(unit.get("header_rows", 0)), len(rows))),
                "rows": rows,
            }
            spans = unit.get("spans") or []
            width = max(len(r) for r in rows)
            if (
                not isinstance(spans, list)
                or len(spans) > 200
                or not all(
                    isinstance(span, list)
                    and len(span) == 4
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in span)
                    and 0 <= span[0] < len(rows)
                    and 0 <= span[1] < width
                    and 1 <= span[2] <= len(rows) - span[0]
                    and 1 <= span[3] <= width - span[1]
                    for span in spans
                )
            ):
                raise ValueError("table spans must be [row, column, rows, columns] within the table")
            if spans:
                table["spans"] = spans
            clean.append(table)
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
            if align not in {"left", "center", "right", "justify"}:
                raise ValueError("align must be left, center, right or justify")
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


# What a page's units become on the public site: the reviewer's structure and formatting, without
# where a restored number came from.
PUBLIC_KEYS = ("kind", "text", "marker", "level", "align", "marks", "notes")
TABLE_KEYS = ("kind", "caption", "header_rows", "rows", "spans")


def public_source(document: dict, manifest: dict) -> dict:
    """The catalogue record a reader sees; nothing is asserted that has not been reviewed."""
    return {
        "id": document["id"],
        "jurisdiction_id": document.get("jurisdiction") or manifest.get("jurisdiction") or "",
        "title": document["title"],
        "citation": document.get("citation") or document["title"],
        "document_type": document.get("type") or manifest.get("document_type") or "other",
        "canonical_url": document.get("url", ""),
        "legal_status": "unknown",
        "rights_status": "RIGHTS_UNDER_REVIEW",
        "catalogue_note": manifest.get("description", ""),
        "has_full_text": True,
        **({"language": document["language"]} if document.get("language") else {}),
    }


READERS = {
    "docai": "Google Document AI Layout Parser",
    "mineru": "MinerU, an open-source reader run offline",
    "azure": "Azure AI Document Intelligence (layout)",
}


def reader_names(reading: dict) -> dict:
    """The machine readers behind a reading, named for the public."""
    primary = reading["primary"]["reader"]
    return {
        "primary": READERS.get(primary, primary),
        "primary_version": (reading["primary"].get("version") or {}).get("version"),
        "checks": [READERS.get(name, name) for name in reading.get("checks", {})],
    }


def public_pages(final: list[dict], latest: dict[int, dict]) -> list[dict]:
    """Each page's blocks as the public site renders them, and whether a reviewer checked it."""
    pages = []
    for page in final:
        revision = latest.get(page["page"])
        blocks = []
        for unit in page["units"]:
            keys = TABLE_KEYS if unit["kind"] == "table" else PUBLIC_KEYS
            block = {key: unit[key] for key in keys if unit.get(key) not in (None, "", [])}
            if unit["kind"] != "table":
                block.setdefault("text", "")  # an emptied paragraph still has words: none
            blocks.append(block)
        pages.append(
            {
                "page": page["page"],
                "checked": bool(revision and revision["status"] == "checked"),
                "blocks": blocks,
            }
        )
    return pages


def details_sha256(record: dict | None) -> str | None:
    """The fingerprint of a details record: what an approval of the details binds to."""
    if not record:
        return None
    return hashlib.sha256(
        canonical_json({"details": record["details"], "absent": record["absent"]}).encode()
    ).hexdigest()


def details_state(record: dict | None, gaps: list[str]) -> dict:
    """Whether the document's details are confirmed and complete (gaps: what is neither given nor
    accounted for, from workbench/record_view.py)."""
    if not record:
        return {"revision": 0, "status": None, "missing": None, "confirmed": False}
    return {
        "revision": record["revision"],
        "status": record["status"],
        "missing": gaps,
        "confirmed": record["status"] == "confirmed" and not gaps,
    }


def readiness(reading: dict, latest: dict[int, dict], described: dict | None = None) -> dict:
    """What still stands between this document and approval: its pages and its details."""
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
    described = described or details_state(None, [])
    return {
        "pages": len(reading["pages"]),
        "unchecked_pages": unchecked,
        "open_high_flags": open_high,
        "stale_pages": stale,
        "missing_checks": missing,
        "details": described,
        "ready": not unchecked and not open_high and not stale and not missing and described["confirmed"],
    }


def machine_html(page: dict) -> str:
    """The machine reading with its flags highlighted, as in the static review pack."""
    return first_collection.reconciled_body(page)[1]


__all__ = ["Library", "MATERIALS", "layout_parser"]


# --- a law's language versions side by side -------------------------------------------------------------


def articles_of(final: list[dict], language: str) -> list[dict]:
    """The articles of one language version as reviewed: each heading's number, marker and title,
    the start of its text and the page it begins on (the contents list is not the body)."""
    from ingestion import rw_gazette

    pattern = rw_gazette.ARTICLE.get(language)
    out: list[dict] = []
    for page in final:
        for unit in page["units"]:
            if unit.get("kind") == "table":
                continue
            line = " ".join(filter(None, [unit.get("marker"), unit.get("text")])).strip()
            match = pattern.match(line) if pattern and unit.get("kind") == "heading" else None
            if match:
                out.append(
                    {
                        "article": rw_gazette._number(match.group(1)),
                        "marker": unit.get("marker") or "",
                        "title": unit.get("text") or "",
                        "text": "",
                        "page": page["page"],
                    }
                )
            elif out and unit.get("kind") in ("paragraph", "list_item") and len(out[-1]["text"]) < 600:
                out[-1]["text"] = (out[-1]["text"] + " " + line).strip()[:600]
    return out


def side_by_side(versions: dict[str, list[dict]]) -> list[dict]:
    """Rows of articles, one per number in the order first met, each with every version's article."""
    order: list[str] = []
    for found in versions.values():
        for article in found:
            if article["article"] not in order:
                order.append(article["article"])
    by_number = {language: {a["article"]: a for a in found} for language, found in versions.items()}
    return [
        {
            "article": number,
            "versions": {language: by_number[language].get(number) for language in versions},
            "complete": all(number in by_number[language] for language in versions),
        }
        for number in order
    ]
