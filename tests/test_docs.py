"""The developer documentation (web/src/docs): every link leads to a page and heading that exist,
every picture is the size the pages say, every Python example is valid Python, every recorded
example answer is what the API's own models accept, and the sitemap lists the pages."""

import ast
import json
import re
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from api.service import create_service, docs_pages
from api.v1 import create_v1_app
from schema import public

WEB = Path(__file__).resolve().parents[1] / "web"
DOCS = WEB / "src" / "docs"
CONTENT = {path.stem: path.read_text() for path in (DOCS / "content").glob("*.md")}
SPEC = json.loads((DOCS / "data" / "openapi.json").read_text())
TOOLS = json.loads((DOCS / "data" / "mcp-tools.json").read_text())
EXAMPLES = json.loads((DOCS / "data" / "examples.json").read_text())
ERROR_CODES = re.findall(r'\d{3},\s*"([a-z_]+)"', (DOCS / "errors.ts").read_text())


def slugify(text: str) -> str:
    # As markdown.ts: lower case, no backticks, runs of other characters become one hyphen.
    return re.sub(r"[\W_]+", "-", text.lower().replace("`", "")).strip("-")


def anchors(markdown: str) -> set[str]:
    found = set()
    for line in re.sub(r"```.*?```", "", markdown, flags=re.S).splitlines():
        heading = re.match(r"^#{1,4}\s+(.*?)(?:\s+\{#([\w-]+)\})?\s*$", line)
        if heading:
            found.add(heading[2] or slugify(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading[1])))
    return found


def pages() -> dict[str, set[str] | None]:
    """Every page's slug, with the headings links may point to (None: not checked)."""
    found: dict[str, set[str] | None] = {
        ("" if name == "index" else name): anchors(text) for name, text in CONTENT.items()
    }
    found["errors"] = set(ERROR_CODES)
    found["api/mcp"] = {tool["name"] for tool in TOOLS}
    for methods in SPEC["paths"].values():
        found["api/" + next(iter(methods.values()))["operationId"].replace("_", "-")] = None
    return found


def test_every_link_leads_to_a_page_and_heading_that_exist():
    known = pages()
    broken = []
    for name, text in CONTENT.items():
        here = "" if name == "index" else name
        for target in re.findall(r"\]\((/docs[^)\s]*|#[^)\s]+)\)", text):
            path, _, anchor = target.partition("#")
            slug = here if not path else path.removeprefix("/docs").strip("/")
            if slug not in known:
                broken.append(f"{name}: {target}")
            elif anchor and known[slug] is not None and anchor not in known[slug]:
                broken.append(f"{name}: {target} (no such heading)")
    assert not broken, broken


def test_every_picture_is_the_size_the_pages_say():
    declared = {
        name: (int(w), int(h))
        for name, w, h in re.findall(r'"?([\w-]+)"?: \[(\d+), (\d+)\]', (DOCS / "images.ts").read_text())
    }
    assert declared
    used = {name for text in CONTENT.values() for name in re.findall(r"\(/guide/([\w-]+)\.webp\)", text)}
    assert used <= set(declared), used - set(declared)
    for name, (width, height) in declared.items():
        data = (WEB / "public" / "guide" / f"{name}.webp").read_bytes()[:30]
        if data[12:16] == b"VP8X":  # extended WebP: 24-bit sizes, less one
            w, h = int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
        else:
            assert data[12:16] == b"VP8 ", name
            w, h = struct.unpack("<HH", data[26:30])
        assert ((w & 0x3FFF) // 2, (h & 0x3FFF) // 2) == (width, height), name


def test_every_python_example_is_valid_python():
    for name, text in CONTENT.items():
        for block in re.findall(r"^```python\n(.*?)\n```$", text, flags=re.S | re.M):
            ast.parse(block, filename=f"{name}.md")


@pytest.fixture
def v1(store, tmp_path):
    return create_v1_app(store, tmp_path, lambda: [])


def test_recorded_answers_are_what_the_api_answers(v1):
    operations = {next(iter(methods.values()))["operationId"] for methods in SPEC["paths"].values()}
    assert {key for key in EXAMPLES if not key.startswith("mcp:")} <= operations
    models = {
        route.operation_id or route.name: route.response_model
        for route in v1.routes
        if getattr(route, "response_model", None) is not None
    }
    by_path = {path: next(iter(methods.values()))["operationId"] for path, methods in SPEC["paths"].items()}
    checked = 0
    for route in v1.routes:
        operation = by_path.get(getattr(route, "path", None))
        model = getattr(route, "response_model", None)
        if operation in EXAMPLES and model is not None and "answer" in EXAMPLES[operation]:
            TypeAdapter(model).validate_python(EXAMPLES[operation]["answer"])
            checked += 1
    assert models and checked >= 7


def test_example_requests_send_what_each_route_requires():
    # The reference's example for each route works as written: every required query parameter is in it,
    # and a request body is valid for the route's own model.
    for methods in SPEC["paths"].values():
        [(method, operation)] = methods.items()
        example = EXAMPLES.get(operation["operationId"], {})
        sent = {name for name, _ in example.get("query", [])}
        required = {
            p["name"] for p in operation.get("parameters", []) if p["in"] == "query" and p.get("required")
        }
        assert required <= sent, (operation["operationId"], required - sent)
        if method == "post":
            assert "body" in example, operation["operationId"]
            ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
            TypeAdapter(getattr(public, ref)).validate_python(example["body"])


def test_the_sitemap_lists_the_documentation(catalogue, store, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.json").write_text(json.dumps(["/docs", "/docs/quickstart"]))
    assert docs_pages(tmp_path) == ["/docs", "/docs/quickstart"]
    assert docs_pages(None) == [] and docs_pages(tmp_path / "missing") == []
    store.save(catalogue)
    sitemap = TestClient(create_service(store, tmp_path, frontend=tmp_path)).get("/sitemap.xml").text
    assert "/docs/quickstart</loc>" in sitemap


def test_pictures_and_markdown_are_served_as_what_they_are(store, tmp_path):
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide" / "x.webp").write_bytes(b"RIFF....WEBPVP8 ")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("# X\n")
    client = TestClient(create_service(store, tmp_path, frontend=tmp_path))
    assert client.get("/guide/x.webp").headers["content-type"] == "image/webp"
    assert client.get("/docs/x.md").headers["content-type"].startswith("text/markdown")
