"""Compare document readers on a fixed set of gazette pages against verified answer keys.

    python -m ingestion.benchmark images                 # render the benchmark pages (200 dpi)
    python -m ingestion.benchmark run glm-ocr            # read every page with one reader
    python -m ingestion.benchmark score                  # score all stored readings

Answer keys live in private-staging/bench/gold/<act>-p<page>.txt: one line per block, table rows
written as cells separated by " | ". Readings are stored per reader in private-staging/bench/
results/<reader>.json as {"<act>:<page>": {"text": ..., "tables": [[[cell, ...], ...], ...]}}.

Scores are word-level and case-insensitive (small capitals print ambiguously); words whose case
differs from the key are counted separately because a reader that recases text is rewriting it.
"""

import argparse
import base64
import copy
import difflib
import html
import io
import json
import os
import re
import subprocess
import time
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "private-staging" / "bench"
STAGING = ROOT / "private-staging" / "first-collection"
GOLD = BENCH / "gold"
RESULTS = BENCH / "results"
IMAGES = BENCH / "images"
KEY = re.compile(r"^(?P<act>ng-[a-z0-9-]+)-p(?P<page>\d+)\.txt$")


def pages(act: str | None = None) -> list[tuple[str, int]]:
    found = [KEY.match(p.name) for p in GOLD.glob("*.txt")]
    return sorted((m["act"], int(m["page"])) for m in found if m and act in (None, m["act"]))


def read_gold(act: str, page: int) -> dict:
    """Text in reading order (table rows in place) and the tables as cell grids."""
    lines, tables, current = [], [], []
    for line in (GOLD / f"{act}-p{page}.txt").read_text().splitlines():
        if " | " in line:
            current.append([cell.strip() for cell in line.split(" | ")])
            lines.append(" ".join(current[-1]))
            continue
        if current:
            tables.append(current)
            current = []
        lines.append(line)
    if current:
        tables.append(current)
    return {"text": "\n".join(lines), "tables": tables}


# --- normalisation and scoring -------------------------------------------------------------

QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "—", "―": "—"})
LEADERS = re.compile(r"\.{2,}|…+")
SPACE_BEFORE = re.compile(r"\s+([;:,.)\]])")
SPACE_AFTER = re.compile(r"([(\[])\s+")


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).translate(QUOTES)
    text = re.sub(r"(?<=\w)(?:\.{2,}|…+)(?=\w)", "", text)  # "202..6": a date written over a dotted line
    text = LEADERS.sub(" ", text)
    text = SPACE_AFTER.sub(r"\1", SPACE_BEFORE.sub(r"\1", text))
    text = text.replace("—", " — ")
    return text.split()


def edit_distance(a: list[str], b: list[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def bag_overlap(a: list[str], b: list[str]) -> int:
    counts = {}
    for word in a:
        counts[word] = counts.get(word, 0) + 1
    overlap = 0
    for word in b:
        if counts.get(word, 0) > 0:
            counts[word] -= 1
            overlap += 1
    return overlap


def cell_similarity(left: str, right: str) -> float:
    a, b = [w.lower() for w in words(left)], [w.lower() for w in words(right)]
    return 1.0 if not a and not b else difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


def table_score(gold: list[list[str]], tables: list[list[list[str]]]) -> dict:
    """Best match among the reader's tables: column count agreement and mean cell similarity."""
    columns = max(len(row) for row in gold)
    best = {"found": False, "columns": None, "cells": 0.0}
    for table in tables:
        if not table:
            continue
        width = max(len(row) for row in table)
        rows = [row + [""] * (width - len(row)) for row in table]
        scores = []
        for g_row in gold:
            candidates = [
                sum(cell_similarity(g, r) for g, r in zip(g_row, row, strict=False)) / columns for row in rows
            ]
            scores.append(max(candidates) if width == columns else 0.0)
        cells = sum(scores) / len(scores)
        if cells >= best["cells"]:
            best = {"found": True, "columns": width, "cells": cells}
    return best


def score(gold: dict, reading: dict) -> dict:
    g, r = words(gold["text"]), words(reading["text"])
    g_lower, r_lower = [w.lower() for w in g], [w.lower() for w in r]
    overlap = bag_overlap(g_lower, r_lower)
    matcher = difflib.SequenceMatcher(a=g_lower, b=r_lower, autojunk=False)
    recased = [
        (g[i + k], r[j + k])
        for i, j, size in matcher.get_matching_blocks()
        for k in range(size)
        if g[i + k] != r[j + k]
    ]
    result = {
        "word_error_rate": edit_distance(g_lower, r_lower) / max(len(g), 1),
        "recall": overlap / max(len(g), 1),
        "precision": overlap / max(len(r), 1),
        "recased": len(recased),
        "recased_examples": recased[:5],
    }
    if gold["tables"]:
        result["tables"] = [table_score(table, reading.get("tables", [])) for table in gold["tables"]]
    return result


# --- converting reader output to {"text", "tables"} -----------------------------------------


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell, self.depth = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            self.tables.append([])
        elif tag == "tr" and self.depth:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
            span = int(dict(attrs).get("colspan") or 1)
            self.row.extend([None] * (span - 1))  # placeholders so spanned columns still count
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.depth:
            self.tables[-1].append(["" if c is None else c for c in self.row])
            self.row = None
        elif tag == "table":
            self.depth = max(self.depth - 1, 0)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


PIPE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
PIPE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}")


def from_markup(markup: str) -> dict:
    """Markdown or HTML output (GLM-OCR, PaddleOCR-VL, MinerU, Azure markdown)."""
    tables = []

    def inline(match):  # keep each HTML table's text where it appears, one line per row
        parser = _Tables()
        parser.feed(match[0])
        found = [t for t in parser.tables if t]
        tables.extend(found)
        return "\n" + "\n".join(" ".join(row) for table in found for row in table) + "\n"

    markup = re.sub(r"<table.*?</table>", inline, markup, flags=re.S | re.I)
    current, lines = [], []
    for line in markup.splitlines():
        if PIPE_RULE.match(line):
            continue
        match = PIPE_ROW.match(line)
        if match:
            current.append([cell.strip() for cell in match[1].split("|")])
            lines.append(" ".join(current[-1]))
            continue
        if current:
            tables.append(current)
            current = []
        lines.append(line)
    if current:
        tables.append(current)
    text = html.unescape(re.sub(r"<[^>]+>", " ", "\n".join(lines)))
    text = re.sub(r"^\s*#+\s*|\*\*|__|^\s*[-*]\s+(?=\S)", "", text, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", text)  # Markdown escapes such as "1\."
    text = re.sub(r"[☐☒]|:(?:un)?selected:", " ", text)  # selection marks some readers add
    return {"text": text, "tables": tables}


def from_layout(nodes: list[dict], page: int) -> dict:
    from ingestion import layout_parser

    tables = [
        [[cell["text"] for cell in row] for row in node["header_rows"] + node["body_rows"]]
        for node in nodes
        if node["page"] == page and node["kind"] == "table"
    ]
    return {"text": layout_parser.page_text(nodes, page), "tables": tables}


# --- readers ---------------------------------------------------------------------------------


def image(act: str, page: int) -> Path:
    return IMAGES / f"{act}-p{page}.png"


def render_images() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    for act, page in pages():
        if image(act, page).exists():
            continue
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
                f"{STAGING}:/in:ro",
                "-v",
                f"{IMAGES}:/out",
                "open-alvary-ocr",
                f"pdftoppm -r 200 -png -singlefile -f {page} -l {page} /in/{act}.pdf /out/{act}-p{page}",
            ],
            check=True,
            capture_output=True,
        )


def read_first(act: str, page: int) -> dict:
    return {"text": json.loads((STAGING / f"{act}.pages.json").read_text())[page - 1], "tables": []}


def _nodes(stored: dict, page: int) -> list[dict]:
    """Nodes for a stored Layout Parser reading, re-converted from the raw layout when kept."""
    from ingestion import layout_parser

    if "layout" in stored:
        return [
            node
            for chunk in stored["layout"]
            for node in layout_parser.convert_blocks(chunk["blocks"], offset=page + chunk["first_page"] - 1)
        ]
    return copy.deepcopy(stored["nodes"])


def read_docai(version: str, live: bool = False):
    """Pilot readings where they exist (unless live); otherwise one billed Layout Parser call."""
    pilot_file = STAGING / "pilot-layout.json"
    pilot = json.loads(pilot_file.read_text()) if pilot_file.exists() and not live else {}

    def reader(act: str, page: int) -> dict:
        from pypdf import PdfReader, PdfWriter

        from ingestion import layout_parser

        stored = {"nodes": pilot.get(f"{act}:{page}:pretrained-layout-parser-{version}")}
        if stored["nodes"] is None:
            writer = PdfWriter()
            writer.add_page(PdfReader(STAGING / f"{act}.pdf").pages[page - 1])
            buffer = io.BytesIO()
            writer.write(buffer)
            config = layout_parser.DocAIConfig.from_environment()
            config = layout_parser.DocAIConfig(
                config.project, config.location, config.processor_id, f"pretrained-layout-parser-{version}"
            )
            stored = {"layout": layout_parser.parse(buffer.getvalue(), config)["layout"]}
        nodes = _nodes(stored, page)
        for node in nodes:  # pilot files predate the newer furniture labels
            if node.get("level") in layout_parser.FURNITURE:
                node["kind"] = "furniture"
        layout_parser.restore_markers(nodes, page, read_first(act, page)["text"])
        return {**from_layout(nodes, page), **stored}

    return reader


def read_combined(main: str, reference: str):
    """A stored Layout Parser reading with dropped markers restored from another reader's text."""

    def reader(act: str, page: int) -> dict:
        from ingestion import layout_parser

        key = f"{act}:{page}"
        nodes = _nodes(json.loads((RESULTS / f"{main}.json").read_text())[key], page)
        layout_parser.restore_markers(
            nodes, page, json.loads((RESULTS / f"{reference}.json").read_text())[key]["text"]
        )
        return from_layout(nodes, page)

    return reader


def read_glm_ocr(act: str, page: int) -> dict:
    payload = base64.b64encode(image(act, page).read_bytes()).decode()
    response = httpx.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": "glm-ocr",
            "prompt": "Text Recognition:",
            "images": [payload],
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096},
        },
        timeout=900,
    )
    response.raise_for_status()
    return from_markup(response.json()["response"])


def read_external(script: str, python: Path):
    """Readers whose dependencies live in their own environment under private-staging/bench."""

    def reader(act: str, page: int) -> dict:
        done = subprocess.run(
            [str(python), str(Path(__file__).with_name("bench_readers") / script), str(image(act, page))],
            check=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        return from_markup(done.stdout)

    return reader


def read_azure(act: str, page: int) -> dict:
    from ingestion import settings

    config = settings.require("AZURE_DI_ENDPOINT", "AZURE_DI_KEY")
    endpoint = config["AZURE_DI_ENDPOINT"].rstrip("/")
    headers = {"Ocp-Apim-Subscription-Key": config["AZURE_DI_KEY"]}
    url = (
        f"{endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze"
        "?api-version=2024-11-30&outputContentFormat=markdown"
    )
    started = httpx.post(
        url,
        headers={**headers, "Content-Type": "image/png"},
        content=image(act, page).read_bytes(),
        timeout=120,
    )
    started.raise_for_status()
    while True:
        time.sleep(2)
        poll = httpx.get(started.headers["Operation-Location"], headers=headers, timeout=60).json()
        if poll["status"] in {"succeeded", "failed"}:
            break
    if poll["status"] != "succeeded":
        raise RuntimeError(f"Azure analysis failed: {poll.get('error')}")
    result = poll["analyzeResult"]
    tables = []
    for table in result.get("tables", []):
        grid = [[""] * table["columnCount"] for _ in range(table["rowCount"])]
        for cell in table["cells"]:
            grid[cell["rowIndex"]][cell["columnIndex"]] = cell.get("content", "")
        tables.append(grid)
    return {"text": from_markup(result["content"])["text"], "tables": tables}


VENV = BENCH / "{}-venv" / "bin" / "python"
READERS = {
    "first-reading": read_first,
    "docai-v1.0": read_docai("v1.0-2024-06-03"),
    "docai-v1.6-pro": read_docai("v1.6-pro-2025-12-01", live=True),
    "docai-v1.6-flash": read_docai("v1.6-2026-01-13"),
    "docai-v3.1-lite": read_docai("v3.1-lite-2026-08-11"),
    "docai-v1.6-pro+mineru": read_combined("docai-v1.6-pro", "mineru-2.5-pro"),
    "glm-ocr-page": read_glm_ocr,
    "glm-ocr": read_external("glm_ocr_regions.py", Path(str(VENV).format("paddle"))),
    "paddleocr-vl-1.6": read_external("paddleocr_vl.py", Path(str(VENV).format("paddle"))),
    "mineru-2.5-pro": read_external("mineru.py", Path(str(VENV).format("mineru"))),
    "azure-layout": read_azure,
}


def run(name: str, only: str | None = None) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    stored = json.loads(path.read_text()) if path.exists() else {}
    for act, page in pages(only):
        key = f"{act}:{page}"
        if key in stored:
            continue
        started = time.monotonic()
        stored[key] = {**READERS[name](act, page), "seconds": round(time.monotonic() - started, 1)}
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=1))
        print(f"{name} {key} {stored[key]['seconds']}s", flush=True)


def score_all(only: str | None = None) -> dict:
    report = {}
    run("first-reading", only)  # free and local; every other reader is run explicitly
    for name in READERS:
        path = RESULTS / f"{name}.json"
        if not path.exists():
            continue
        stored = json.loads(path.read_text())
        report[name] = {
            f"{act}:{page}": score(read_gold(act, page), stored[f"{act}:{page}"])
            for act, page in pages(only)
            if f"{act}:{page}" in stored
        }
        if not report[name]:
            del report[name]
    return report


def summary(report: dict) -> str:
    lines = [
        f"{'reader':18} {'pages':>5} {'WER':>6} {'recall':>7} {'precis.':>7} {'recased':>7} {'tables (cols ok, cells)':>24}"
    ]
    for name, results in report.items():
        n = len(results)

        def mean(key, results=results, n=n):
            return sum(r[key] for r in results.values()) / max(n, 1)

        tables = [t for r in results.values() for t in r.get("tables", [])]
        table_note = ", ".join(
            f"{'✓' if t['found'] and t['columns'] == 5 else '✗'} {t['cells']:.0%}" for t in tables
        )
        lines.append(
            f"{name:18} {n:5} {mean('word_error_rate'):6.1%} {mean('recall'):7.1%} {mean('precision'):7.1%} "
            f"{sum(r['recased'] for r in results.values()):7} {table_note:>24}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["images", "run", "score"])
    parser.add_argument("reader", nargs="?", choices=sorted(READERS))
    parser.add_argument("--act", help="only the answer-key pages of this manifest id")
    args = parser.parse_args(argv)
    if args.action == "images":
        render_images()
    elif args.action == "run":
        if not args.reader:
            parser.error("run needs a reader")
        run(args.reader, args.act)
    else:
        report = score_all(args.act)
        (BENCH / "scores.json").write_text(json.dumps(report, indent=1))
        print(summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
