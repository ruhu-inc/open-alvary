"""Google Document AI Layout Parser adapter (operator-only ingestion).

Sends gazette PDFs to a Layout Parser processor and converts its layout tree into a neutral,
verbatim structure: headings, paragraphs, lists and tables with real rows, header rows and
merged cells, each with its page. Page headers and footers are kept apart as furniture.
Generative table/image *annotations* are never requested: only text read from the page.

Configuration (all required except the version): ALVARY_DOCAI_PROJECT, ALVARY_DOCAI_LOCATION
(us or eu), ALVARY_DOCAI_PROCESSOR_ID, optional ALVARY_DOCAI_PROCESSOR_VERSION. Credentials use
Google Application Default Credentials (`gcloud auth application-default login` or a service
account with the Document AI API User role); none are read or stored here.
"""

import difflib
import html
import os
import re
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader, PdfWriter

ONLINE_PAGE_LIMIT = 15  # synchronous Layout Parser requests accept at most 15 pages
# Running heads and folios; newer processor versions call them "header" and "footer".
FURNITURE = {"page_header", "page_footer", "header", "footer"}
# Section and paragraph markers the parser strips from ordered lists: 91.  12A.  (a)  (iv)  (2)
MARKER = re.compile(r"^(?:\d{1,3}[A-Z]?\.|\([a-z]{1,2}\)|\([ivxl]{1,6}\)|\(\d{1,3}[A-Z]?\))$")
HEADINGS = {f"heading-{n}" for n in range(1, 7)} | {"subheading"}


@dataclass(frozen=True)
class DocAIConfig:
    project: str
    location: str
    processor_id: str
    processor_version: str | None = None

    def __post_init__(self):
        if self.location not in {"us", "eu"} or not re.fullmatch(
            r"[a-z][a-z0-9-]{4,61}[a-z0-9]", self.project
        ):
            raise ValueError("Invalid Document AI project or location")
        if not re.fullmatch(r"[0-9a-f]{8,32}", self.processor_id):
            raise ValueError("Invalid Document AI processor ID")
        if self.processor_version is not None and not re.fullmatch(
            r"[a-z0-9.-]{3,80}", self.processor_version
        ):
            raise ValueError("Invalid Document AI processor version")

    @classmethod
    def from_environment(cls) -> "DocAIConfig":
        from ingestion import settings

        settings.load()
        missing = [
            k
            for k in ("ALVARY_DOCAI_PROJECT", "ALVARY_DOCAI_LOCATION", "ALVARY_DOCAI_PROCESSOR_ID")
            if not os.environ.get(k)
        ]
        if missing:
            raise ValueError("Configure " + ", ".join(missing))
        return cls(
            project=os.environ["ALVARY_DOCAI_PROJECT"],
            location=os.environ["ALVARY_DOCAI_LOCATION"],
            processor_id=os.environ["ALVARY_DOCAI_PROCESSOR_ID"],
            processor_version=os.environ.get("ALVARY_DOCAI_PROCESSOR_VERSION") or None,
        )


def page_chunks(raw: bytes, size: int = ONLINE_PAGE_LIMIT) -> list[tuple[int, bytes]]:
    """Split a PDF into consecutive chunks of at most `size` pages: (first page number, bytes)."""
    reader = PdfReader(BytesIO(raw))
    chunks = []
    for start in range(0, len(reader.pages), size):
        writer = PdfWriter()
        for page in reader.pages[start : start + size]:
            writer.add_page(page)
        buffer = BytesIO()
        writer.write(buffer)
        chunks.append((start + 1, buffer.getvalue()))
    return chunks


def _type(block: dict) -> str:
    return block.get("type_") or block.get("type") or ""


def _text_of(blocks: list[dict]) -> str:
    """Verbatim text of nested blocks, in reading order, one block per line."""
    lines = []
    for block in blocks or []:
        if "text_block" in block:
            text = block["text_block"].get("text", "")
            if text:
                lines.append(text)
            lines.extend(filter(None, [_text_of(block["text_block"].get("blocks", []))]))
        elif "list_block" in block:
            for entry in block["list_block"].get("list_entries", []):
                lines.append(_text_of(entry.get("blocks", [])))
        elif "table_block" in block:
            table = block["table_block"]
            rows = table.get("header_rows", []) + table.get("body_rows", [])
            lines.append(_text_of([cell for row in rows for cell in row.get("cells", [])]))
        elif "blocks" in block:  # a table cell
            lines.append(_text_of(block["blocks"]))
    return "\n".join(line for line in lines if line)


def _page(block: dict, offset: int) -> tuple[int, int]:
    span = block.get("page_span") or {}
    start = int(span.get("page_start") or 1) + offset - 1
    end = int(span.get("page_end") or span.get("page_start") or 1) + offset - 1
    return start, end


def convert_blocks(blocks: list[dict], offset: int = 1) -> list[dict]:
    """Flatten a Layout Parser block tree into ordered nodes with absolute page numbers."""
    nodes = []
    for block in blocks or []:
        page_start, page_end = _page(block, offset)
        if "text_block" in block:
            text_block = block["text_block"]
            kind = _type(text_block)
            if kind in FURNITURE:
                nodes.append({"kind": "furniture", "text": text_block.get("text", ""), "page": page_start})
                continue
            if text_block.get("text"):
                nodes.append(
                    {
                        "kind": "heading"
                        if kind in HEADINGS
                        else "list_item"
                        if kind == "list_item"
                        else "paragraph",
                        "level": kind,
                        "text": text_block["text"],
                        "page": page_start,
                    }
                )
            nodes.extend(convert_blocks(text_block.get("blocks", []), offset))
        elif "table_block" in block:
            table = block["table_block"]

            def rows(key, table=table):
                return [
                    [
                        {
                            "text": _text_of(cell.get("blocks", [])),
                            "row_span": int(cell.get("row_span") or 1),
                            "col_span": int(cell.get("col_span") or 1),
                        }
                        for cell in row.get("cells", [])
                    ]
                    for row in table.get(key, [])
                ]

            nodes.append(
                {
                    "kind": "table",
                    "caption": table.get("caption", ""),
                    "header_rows": rows("header_rows"),
                    "body_rows": rows("body_rows"),
                    "page": page_start,
                    "page_end": page_end,
                }
            )
        elif "list_block" in block:
            ordered = _type(block["list_block"]) == "ordered"
            for entry in block["list_block"].get("list_entries", []):
                text = _text_of(entry.get("blocks", []))
                if text:
                    nodes.append(
                        {
                            "kind": "list_item",
                            "level": "list_item",
                            "text": text,
                            "page": page_start,
                            "ordered": ordered,
                        }
                    )
    return nodes


def parse(raw: bytes, config: DocAIConfig, client=None) -> dict:
    """Run the Layout Parser chunk by chunk (online limit) and merge into one node list."""
    from google.api_core.client_options import ClientOptions
    from google.cloud import documentai_v1 as documentai

    if client is None:
        endpoint = f"{config.location}-documentai.googleapis.com"
        client = documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=endpoint)
        )
    if config.processor_version:
        name = client.processor_version_path(
            config.project, config.location, config.processor_id, config.processor_version
        )
    else:
        name = client.processor_path(config.project, config.location, config.processor_id)
    nodes = []
    for first_page, chunk in page_chunks(raw):
        request = documentai.ProcessRequest(
            name=name,
            raw_document=documentai.RawDocument(content=chunk, mime_type="application/pdf"),
            process_options=documentai.ProcessOptions(
                layout_config=documentai.ProcessOptions.LayoutConfig(return_bounding_boxes=True)
            ),
        )
        document = client.process_document(request=request).document
        layout = documentai.Document.to_dict(document).get("document_layout", {})
        nodes.extend(convert_blocks(layout.get("blocks", []), offset=first_page))
    return {"processor": name, "nodes": nodes}


TOKEN = re.compile(r"\S+")


def restore_markers(nodes: list[dict], page: int, reference: str) -> int:
    """Copy list markers the parser dropped ("91.", "(a)") from the independent first reading.

    The whole page is aligned word by word; a marker is restored only where the alignment shows
    a marker-shaped word present in the reference and missing immediately before the entry's
    first word. Restored markers are labelled so the reviewer can see where they came from.
    """
    starts, index = {}, 0
    for node in nodes:
        if node["page"] != page or node["kind"] == "furniture":
            continue
        if node["kind"] == "list_item" and not node.get("marker"):
            starts[index] = node
        index += len(TOKEN.findall(_node_text(node)))
    _, missing, _ = disagreements(reference, page_text(nodes, page))
    restored = 0
    for position, node in starts.items():
        before = missing.get(position, "").split()
        if before and MARKER.match(before[-1]):
            node["marker"], node["marker_source"] = before[-1], "first reading"
            restored += 1
    return restored


def _node_text(node: dict) -> str:
    if node["kind"] == "table":
        rows = node["header_rows"] + node["body_rows"]
        return "\n".join(
            [node["caption"]] * bool(node["caption"]) + [" ".join(c["text"] for c in r) for r in rows]
        )
    return f"{node['marker']} {node['text']}" if node.get("marker") else node["text"]


def page_text(nodes: list[dict], page: int) -> str:
    """Comparable plain text for one page (furniture excluded; tables read row by row)."""
    return "\n".join(_node_text(n) for n in nodes if n["page"] == page and n["kind"] != "furniture")


def disagreements(reference: str, candidate: str) -> tuple[set[int], dict[int, str], float]:
    """Compare the candidate with the independent reference, word by word.

    Returns the indices of candidate words that differ, the reference words missing from the
    candidate keyed by the candidate index they belong before, and the overall agreement.
    """
    left, right = TOKEN.findall(reference), TOKEN.findall(candidate)
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    differing, missing = set(), {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        differing.update(range(j1, j2))
        if tag == "delete":
            missing[j1] = " ".join(left[i1:i2])
    agreement = 1.0 if not right and not left else matcher.ratio()
    return differing, missing, agreement


PIECES = re.compile(r"(\s+)")


class _Marker:
    """Escapes text and marks tokens whose running index the comparison flagged."""

    def __init__(self, differing: set[int], missing: dict[int, str]):
        self.differing, self.missing, self.index = differing, missing, 0

    def gap(self) -> str:
        """Reference words the candidate lacks at the current position, struck through."""
        words = self.missing.pop(self.index, "")
        return f"<del title='only in the first reading'>{html.escape(words)}</del> " if words else ""

    def __call__(self, text: str) -> str:
        out = []
        for piece in PIECES.split(text):
            if not piece:
                continue
            if piece.isspace():
                out.append(html.escape(piece))
                continue
            out.append(self.gap())
            escaped = html.escape(piece)
            out.append(f"<mark>{escaped}</mark>" if self.index in self.differing else escaped)
            self.index += 1
        return "".join(out)


def render_page(
    nodes: list[dict], page: int, differing: set[int] = frozenset(), missing: dict[int, str] | None = None
) -> str:
    """HTML for one page's structure; token order matches page_text so marks line up."""
    mark, parts = _Marker(set(differing), dict(missing or {})), []
    for node in nodes:
        if node["page"] != page or node["kind"] == "furniture":
            continue
        if node["kind"] == "table":
            rows = []
            for tag, group in (("th", node["header_rows"]), ("td", node["body_rows"])):
                for row in group:
                    cells = "".join(
                        f"<{tag}{f' rowspan={c["row_span"]}' if c['row_span'] > 1 else ''}"
                        f"{f' colspan={c["col_span"]}' if c['col_span'] > 1 else ''}>{mark(c['text'])}</{tag}>"
                        for c in row
                    )
                    rows.append(f"<tr>{cells}</tr>")
            caption = f"<caption>{mark(node['caption'])}</caption>" if node["caption"] else ""
            parts.append(f"<table>{caption}{''.join(rows)}</table>")
        elif node["kind"] == "heading":
            parts.append(f"<p class='h'>{mark(node['text'])}</p>")
        elif node["kind"] == "list_item":
            marker = ""
            if node.get("marker"):
                marker = f"<span class='restored' title='marker from the first reading'>{mark(node['marker'])}</span> "
            parts.append(f"<p class='li'>{marker}{mark(node['text'])}</p>")
        else:
            parts.append(f"<p>{mark(node['text'])}</p>")
    tail = "".join(
        f"<del title='only in the first reading'>{html.escape(w)}</del>" for w in mark.missing.values()
    )
    return "".join(parts) + (f"<p>{tail}</p>" if tail else "")
