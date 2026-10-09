"""Document readers for the batch pipeline, each returning one common per-page shape.

    {"page": n, "text": str, "tables": [[[cell, ...], ...], ...], "furniture": [str, ...]}

Document AI pages also carry "nodes" (the structured reading the corpus text is built from).
Every reader reports what produced the reading ("version") and how many pages were billed.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pypdf import PdfReader

from ingestion import layout_parser, settings
from ingestion.compare import HtmlTables

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Reading:
    reader: str
    version: dict
    pages: list[dict]
    billed_pages: int = 0
    raw: dict = field(default_factory=dict)


def page_count(pdf: Path) -> int:
    return len(PdfReader(pdf).pages)


def _html_tables(markup: str) -> list[list[list[str]]]:
    parser = HtmlTables()
    parser.feed(markup)
    return [table for table in parser.tables if table]


# --- Document AI Layout Parser (primary) ------------------------------------------------------


class DocAIReader:
    name = "docai"
    billed = True

    def __init__(self, config: layout_parser.DocAIConfig | None = None):
        self.config = config or layout_parser.DocAIConfig.from_environment()
        if not self.config.processor_version:
            raise SystemExit("Pin ALVARY_DOCAI_PROCESSOR_VERSION for batch runs")
        # Gemini-based versions take up to a minute and a half per page, so short requests run
        # side by side keep each one far inside its deadline.
        self.workers = int(os.environ.get("ALVARY_DOCAI_WORKERS") or 8)
        self.pages_per_request = int(os.environ.get("ALVARY_DOCAI_PAGES_PER_REQUEST") or 2)

    def version(self) -> dict:
        return {
            "processor": self.config.processor_id,
            "version": self.config.processor_version,
            "pages_per_request": self.pages_per_request,
        }

    def read(self, pdf: Path) -> Reading:
        result = layout_parser.parse(
            pdf.read_bytes(), self.config, workers=self.workers, pages_per_request=self.pages_per_request
        )
        nodes, count = result["nodes"], page_count(pdf)
        pages = []
        for number in range(1, count + 1):
            mine = [node for node in nodes if node["page"] == number]
            pages.append(
                {
                    "page": number,
                    "text": layout_parser.page_text(mine, number),
                    "tables": [
                        [[cell["text"] for cell in row] for row in node["header_rows"] + node["body_rows"]]
                        for node in mine
                        if node["kind"] == "table"
                    ],
                    "furniture": [node["text"] for node in mine if node["kind"] == "furniture"],
                    "nodes": mine,
                }
            )
        return Reading(
            self.name,
            self.version(),
            pages,
            count,
            {"layout": result["layout"], "retries": result["retries"]},
        )


# --- MinerU (local, independent check) --------------------------------------------------------

MINERU_FURNITURE = {"header", "footer", "page_number", "page_header", "page_footer", "page_footnote"}
MINERU_SKIP = {"image_body", "chart", "chart_body", "image"}


def _mineru_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(filter(None, (_mineru_text(part) for part in content)))
    if isinstance(content, dict):
        return _mineru_text(content.get("content"))
    return ""


def mineru_page(blocks: list[dict]) -> dict:
    """One MinerU middle-JSON page in the common shape."""
    lines, tables, furniture = [], [], []
    for block in blocks:
        kind = block.get("type")
        if kind in MINERU_FURNITURE:
            furniture.append(_mineru_text(block.get("content")))
        elif kind == "table":
            for part in block.get("content", []):
                if part.get("type") == "table_body" and isinstance(part.get("content"), str):
                    found = _html_tables(part["content"])
                    tables.extend(found)
                    lines.extend(" ".join(row) for table in found for row in table)
                else:
                    lines.append(_mineru_text(part.get("content")))
        elif kind == "list":
            lines.extend(_mineru_text(item.get("content")) for item in block.get("content", []))
        elif kind == "image" and block.get("sub_type") == "text_image":
            lines.append(_mineru_text(block.get("content")))  # handwriting MinerU read as an image
        elif kind not in MINERU_SKIP:
            lines.append(_mineru_text(block.get("content")))
    return {"text": "\n".join(line for line in lines if line), "tables": tables, "furniture": furniture}


def _file_sha256(path: Path, cache: Path) -> str:
    """Model files are large: hash once per (path, size, mtime)."""
    stat = path.stat()
    marker = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    known = json.loads(cache.read_text()) if cache.exists() else {}
    if marker not in known:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        known[marker] = digest.hexdigest()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known, indent=1))
    return known[marker]


# Deny all network access except loopback (MinerU's local inference engine) on macOS.
OFFLINE_PROFILE = (
    '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
    '(deny network-inbound)(allow network-inbound (local ip "localhost:*"))'
)


class MinerUReader:
    name = "mineru"
    billed = False

    def __init__(self, kit: Path | None = None, models: Path | None = None, hashes: Path | None = None):
        settings.load()
        default_kit = ROOT / "private-staging" / "bench" / "mineru-venv" / "bin" / "mineru-kit"
        self.kit = Path(kit or os.environ.get("ALVARY_MINERU_KIT") or default_kit)
        if not self.kit.exists():
            raise SystemExit(f"MinerU not found at {self.kit}; set ALVARY_MINERU_KIT")
        self.models = Path(
            models or os.environ.get("ALVARY_MINERU_MODELS") or Path.home() / ".mineru" / "models"
        )
        self.hashes = hashes or ROOT / "private-staging" / "readings" / ".model-hashes.json"

    def version(self) -> dict:
        done = subprocess.run([str(self.kit), "version"], capture_output=True, text=True, timeout=120)
        weights = sorted(self.models.glob("MinerU2.5-Pro-*-GGUF/*.gguf"))
        return {
            "tool": done.stdout.strip().splitlines()[0] if done.stdout.strip() else "unknown",
            "tier": "advanced",
            "weights": {p.name: _file_sha256(p, self.hashes) for p in weights},
        }

    def read(self, pdf: Path) -> Reading:
        version = self.version()
        environment = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "MINERU_MODEL_SOURCE": "local",
        }
        command = [str(self.kit), "parse", str(pdf), "-f", "middle_json", "--tier", "advanced"]
        if Path("/usr/bin/sandbox-exec").exists():
            command = ["/usr/bin/sandbox-exec", "-p", OFFLINE_PROFILE, *command]
        count = page_count(pdf)
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "reading.json"
            done = subprocess.run(
                [*command, "-o", str(out)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=120 + 60 * count,
            )
            if done.returncode != 0 or not out.exists():
                raise RuntimeError(f"MinerU failed ({done.returncode}): {done.stderr[-800:]}")
            middle = json.loads(out.read_text())
        by_index = {page["page_idx"]: page["blocks"] for page in middle["pages"]}
        pages = [{"page": n, **mineru_page(by_index.get(n - 1, []))} for n in range(1, count + 1)]
        return Reading(self.name, version, pages, 0, {"producer": middle.get("metadata", {}).get("producer")})


# --- Azure Document Intelligence layout (cloud, independent check) ----------------------------

AZURE_FURNITURE = {"pageHeader", "pageFooter", "pageNumber"}
API_VERSION = "2024-11-30"


def azure_pages(result: dict, count: int) -> list[dict]:
    """Azure analyzeResult in the common shape: paragraphs in reading order, tables as grids."""
    pages = {n: {"page": n, "lines": [], "tables": [], "furniture": []} for n in range(1, count + 1)}
    for paragraph in result.get("paragraphs", []):
        regions = paragraph.get("boundingRegions") or [{"pageNumber": 1}]
        page = pages.get(regions[0]["pageNumber"])
        if page is None:
            continue
        target = "furniture" if paragraph.get("role") in AZURE_FURNITURE else "lines"
        page[target].append(paragraph.get("content", ""))
    for table in result.get("tables", []):
        regions = table.get("boundingRegions") or [{"pageNumber": 1}]
        grid = [[""] * table["columnCount"] for _ in range(table["rowCount"])]
        for cell in table["cells"]:
            grid[cell["rowIndex"]][cell["columnIndex"]] = cell.get("content", "")
        if regions[0]["pageNumber"] in pages:
            pages[regions[0]["pageNumber"]]["tables"].append(grid)
    return [
        {"page": n, "text": "\n".join(p["lines"]), "tables": p["tables"], "furniture": p["furniture"]}
        for n, p in sorted(pages.items())
    ]


class AzureReader:
    name = "azure"
    billed = True

    def __init__(self, client: httpx.Client | None = None, sleep=time.sleep):
        config = settings.require("AZURE_DI_ENDPOINT", "AZURE_DI_KEY")
        self.endpoint = config["AZURE_DI_ENDPOINT"].rstrip("/")
        self.headers = {"Ocp-Apim-Subscription-Key": config["AZURE_DI_KEY"]}
        self.client = client or httpx.Client(timeout=120)
        self.sleep = sleep

    def version(self) -> dict:
        return {"model": "prebuilt-layout", "api_version": API_VERSION}

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Retry throttling and server errors, honouring Retry-After."""
        for attempt in range(6):
            response = self.client.request(
                method, url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            self.sleep(float(response.headers.get("Retry-After") or 2 ** (attempt + 1)))
        response.raise_for_status()
        return response

    def read(self, pdf: Path, deadline: float = 1800) -> Reading:
        url = f"{self.endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze?api-version={API_VERSION}"
        started = self._send(
            "POST", url, headers={"Content-Type": "application/pdf"}, content=pdf.read_bytes()
        )
        location, waited = started.headers["Operation-Location"], 0.0
        while True:
            poll = self._send("GET", location)
            body = poll.json()
            if body["status"] == "succeeded":
                break
            if body["status"] == "failed" or waited > deadline:
                raise RuntimeError(f"Azure analysis {body['status']}: {body.get('error')}")
            pause = float(poll.headers.get("Retry-After") or 2)
            self.sleep(pause)
            waited += pause
        result, count = body["analyzeResult"], page_count(pdf)
        # Service details are recorded but kept out of the version: it is the cache key.
        service = {"model_id": result.get("modelId"), "service_api": result.get("apiVersion")}
        return Reading(self.name, self.version(), azure_pages(result, count), count, service)


READERS = {"docai": DocAIReader, "mineru": MinerUReader, "azure": AzureReader}
