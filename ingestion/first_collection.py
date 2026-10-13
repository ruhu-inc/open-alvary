"""Acquire and prepare the first full-text collection for accountable review (operator only).

    python -m ingestion.first_collection fetch     # download each manifest PDF once, politely
    python -m ingestion.first_collection extract   # text, gazette details, section outline
    python -m ingestion.first_collection layout    # Document AI Layout Parser reading (tables kept)
    python -m ingestion.first_collection review    # private HTML review pack

Everything is written under private-staging/first-collection/ (git-ignored). Nothing is
published, approved or written to a database: the accountable reviewer compares each
extraction with its PDF and records the rights and content decisions separately.
"""

import argparse
import hashlib
import html
import json
import re
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader

from ingestion import layout_parser
from ingestion.pipeline import MAX_BYTES, normalize

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ingestion" / "catalogue" / "ng-first-collection.json"
STAGING = ROOT / "private-staging" / "first-collection"
ALLOWED_HOSTS = {"placng.org"}
USER_AGENT = "OpenAlvary/0.1 (operator review; contact ijidai@ruhu.ai)"
SECTION = re.compile(r"^\s*(\d{1,3}[A-Z]?)\.\s*[—–-]?\s*(\(1\)\s*)?(\S.*)$")
GAZETTE = re.compile(r"No\.\s*(\d+)\s*,?\s*Vol(?:ume)?\.?\s*(\d+)", re.I)
ACT_NUMBER = re.compile(r"\bAct\s+No\.\s*(\d+)\b", re.I)


DOCUMENT_TYPES = {"act", "bill", "regulation", "order", "judgment", "gazette", "treaty", "other"}


def load_manifest(path: Path = MANIFEST) -> dict:
    """A collection of legal documents of any type; older manifests list them under "acts"."""
    manifest = json.loads(path.read_text())
    documents = manifest.get("documents", manifest.get("acts", []))
    default = manifest.get("document_type", "act")
    for document in documents:
        document.setdefault("type", default)
        if document["type"] not in DOCUMENT_TYPES:
            raise ValueError(f"Unknown document type {document['type']!r}: {document['id']}")
    manifest["documents"] = manifest["acts"] = documents  # one list, two names for older callers
    for act in manifest["documents"]:
        parsed = urlsplit(act["url"])
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in {None, 443}:
            raise ValueError(f"Manifest URL not allowed: {act['id']}")
        if not re.fullmatch(r"ng-[a-z0-9-]{3,120}", act["id"]):
            raise ValueError(f"Invalid act id: {act['id']}")
    return manifest


def fetch_one(client: httpx.Client, act: dict, staging: Path) -> dict:
    """Download one approved URL exactly once; refuse redirects, oversize and non-PDF bodies."""
    pdf = staging / f"{act['id']}.pdf"
    record = staging / f"{act['id']}.provenance.json"
    if pdf.exists() and record.exists():
        return json.loads(record.read_text())
    with client.stream("GET", act["url"], follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError(f"{act['id']}: HTTP {response.status_code}")
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise ValueError(f"{act['id']}: exceeds the ingestion size limit")
        headers = {k: response.headers.get(k) for k in ("content-type", "last-modified", "etag")}
    if not raw.startswith(b"%PDF-"):
        raise ValueError(f"{act['id']}: response is not a PDF")
    pdf.write_bytes(raw)
    provenance = {
        "id": act["id"],
        "url": act["url"],
        "retrieved_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "headers": headers,
    }
    record.write_text(json.dumps(provenance, indent=2) + "\n")
    return provenance


def fetch(manifest: dict, staging: Path = STAGING, delay: float = 3.0) -> list[dict]:
    staging.mkdir(parents=True, exist_ok=True)
    results = []
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
        for index, act in enumerate(manifest["acts"]):
            already = (staging / f"{act['id']}.pdf").exists()
            results.append(fetch_one(client, act, staging))
            if not already and index < len(manifest["acts"]) - 1:
                time.sleep(delay)  # one request at a time, politely spaced
    return results


OCR_IMAGE = "open-alvary-ocr"
OCR_SCRIPT = (
    'for n in $PAGES; do pdftoppm -r 300 -gray -f "$n" -l "$n" -png /in/doc.pdf /tmp/p; '
    'f=$(ls /tmp/p-*.png); printf "\\f@@PAGE %s@@\\n" "$n"; tesseract "$f" - -l eng --psm 6 2>/dev/null; '
    "rm -f /tmp/p-*.png; done"
)


def ocr_pages(pdf: Path, pages: list[int]) -> dict[int, str]:
    """Local Tesseract OCR in a sealed container: no network, read-only input, unprivileged."""
    import subprocess

    if not pages:
        return {}
    completed = subprocess.run(
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
            "-e",
            "PAGES=" + " ".join(map(str, pages)),
            "-v",
            f"{pdf.resolve()}:/in/doc.pdf:ro",
            OCR_IMAGE,
            OCR_SCRIPT,
        ],
        capture_output=True,
        text=True,
        timeout=3600,
        check=True,
    )
    result = {}
    for block in completed.stdout.split("\f@@PAGE ")[1:]:
        number, _, text = block.partition("@@\n")
        result[int(number)] = text
    return result


HEADER_DIGITS = re.compile(r"\d+")


def remove_running_headers(pages: list[str]) -> tuple[list[str], list[str]]:
    """Drop gazette page furniture: a page's FIRST line that repeats (ignoring page numbers) on at
    least a third of pages. Only first lines are considered, so body text is never removed; every
    removed line is returned for the reviewer."""
    keys = {}
    for text in pages:
        for line in text.splitlines()[:1]:
            key = HEADER_DIGITS.sub("#", line.strip())
            if len(key) >= 8:
                keys[key] = keys.get(key, 0) + 1
    repeated = {k for k, count in keys.items() if count >= max(3, len(pages) // 3)}
    cleaned, removed = [], []
    for text in pages:
        lines = text.splitlines()
        kept = []
        for index, line in enumerate(lines):
            if index == 0 and HEADER_DIGITS.sub("#", line.strip()) in repeated:
                removed.append(line.strip())
                continue
            kept.append(line)
        cleaned.append("\n".join(kept))
    return cleaned, removed


def extract_pdf(raw: bytes, ocr=None) -> dict:
    """Text layer per page; pages without one use local OCR when available, else are flagged."""
    reader = PdfReader(BytesIO(raw))
    pages = [normalize(page.extract_text() or "") for page in reader.pages]
    missing = [n for n, text in enumerate(pages, 1) if not text]
    ocr_used = []
    if missing and ocr is not None:
        for number, text in ocr(missing).items():
            if normalize(text):
                pages[number - 1] = normalize(text)
                ocr_used.append(number)
    still_missing = [n for n, text in enumerate(pages, 1) if not text]
    pages, furniture = remove_running_headers(pages)
    text = "\n\n".join(p for p in pages if p)
    head = text[:4000]
    gazette = GAZETTE.search(head)
    act_number = ACT_NUMBER.search(head)
    outline = []
    for line in text.splitlines():
        match = SECTION.match(line)
        if match and len(outline) < 1000:
            outline.append({"section": match[1], "opening": match[3][:120]})
    return {
        "pages": len(pages),
        "pages_without_text": still_missing,
        "ocr_pages": ocr_used,
        "needs_ocr": bool(still_missing),
        "removed_furniture": furniture,
        "characters": len(text),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "gazette": {"number": gazette[1], "volume": gazette[2]} if gazette else None,
        "act_number": act_number[1] if act_number else None,
        "outline": outline,
        "text": text,
        "page_texts": pages,
    }


def extract(manifest: dict, staging: Path = STAGING) -> list[dict]:
    summaries = []
    for act in manifest["acts"]:
        pdf = staging / f"{act['id']}.pdf"
        if not pdf.exists():
            raise FileNotFoundError(f"{act['id']}: fetch first")
        result = extract_pdf(pdf.read_bytes(), ocr=lambda pages, pdf=pdf: ocr_pages(pdf, pages))
        (staging / f"{act['id']}.txt").write_text(result.pop("text"))
        (staging / f"{act['id']}.pages.json").write_text(
            json.dumps(result.pop("page_texts"), ensure_ascii=False)
        )
        (staging / f"{act['id']}.extraction.json").write_text(json.dumps(result, indent=2) + "\n")
        keys = ("pages", "needs_ocr", "characters")
        summaries.append(
            {"id": act["id"], **{k: result[k] for k in keys}, "ocr_pages": len(result["ocr_pages"])}
        )
    return summaries


def layout(manifest: dict, staging: Path = STAGING, only: str | None = None) -> list[dict]:
    """Second, structure-preserving reading by the Document AI Layout Parser (paid cloud call)."""
    if only and only not in {act["id"] for act in manifest["acts"]}:
        raise ValueError(f"{only}: not in the manifest")
    config = layout_parser.DocAIConfig.from_environment()
    summaries = []
    for act in manifest["acts"]:
        if only and act["id"] != only:
            continue
        raw = (staging / f"{act['id']}.pdf").read_bytes()
        result = layout_parser.parse(raw, config)
        first_reading = json.loads((staging / f"{act['id']}.pages.json").read_text())
        result["restored_markers"] = sum(
            layout_parser.restore_markers(result["nodes"], number, text)
            for number, text in enumerate(first_reading, 1)
        )
        result.update(
            source_sha256=hashlib.sha256(raw).hexdigest(),
            parsed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        (staging / f"{act['id']}.layout.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
        tables = sum(node["kind"] == "table" for node in result["nodes"])
        summaries.append({"id": act["id"], "nodes": len(result["nodes"]), "tables": tables})
    return summaries


def render_pages(pdf: Path, out: Path) -> int:
    """Page images for the side-by-side review (local Poppler in the sealed OCR container)."""
    import os
    import subprocess

    out.mkdir(parents=True, exist_ok=True)
    if any(out.glob("page-*.jpg")):
        return len(list(out.glob("page-*.jpg")))
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{pdf.resolve()}:/in/doc.pdf:ro",
            "-v",
            f"{out.resolve()}:/out",
            OCR_IMAGE,
            "pdftoppm -r 90 -jpeg -jpegopt quality=70 /in/doc.pdf /out/page",
        ],
        capture_output=True,
        timeout=3600,
        check=True,
    )
    # Normalize zero-padded names (pdftoppm pads to the page count's width).
    for image in out.glob("page-*.jpg"):
        number = int(image.stem.split("-")[1])
        image.rename(out / f"page-{number}.jpg")
    return len(list(out.glob("page-*.jpg")))


SIDE_BY_SIDE = """<!doctype html><meta charset="utf-8"><title>{title}: page review</title>
<style>
body{{font:15px/1.5 system-ui;margin:0;background:#f4f4ef;color:#1d2420}}
header{{position:sticky;top:0;background:#fff;border-bottom:1px solid #ccc;padding:.6rem 1rem;z-index:1}}
.page{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1rem;padding:1rem;border-bottom:1px solid #ddd}}
.page img{{width:100%;border:1px solid #bbb;background:#fff}}
.text{{background:#fff;border:1px solid #bbb;padding:.8rem;white-space:pre-wrap;font:14px/1.55 ui-monospace,monospace}}
.ocr .text{{border:2px solid #c77700}} .badge{{font-weight:600}} .ocr .badge{{color:#a35f00}}
.done{{opacity:.55}} label{{cursor:pointer}}
.layout{{background:#fff;border:1px solid #bbb;padding:.8rem;font:14px/1.55 ui-monospace,monospace}}
.layout p{{margin:.2rem 0;white-space:pre-wrap}} .layout .h{{font-weight:700}} .layout .li{{padding-left:1rem}}
.layout table{{border-collapse:collapse;margin:.5rem 0;width:100%}}
.layout td,.layout th{{border:1px solid #888;padding:.2rem .4rem;vertical-align:top;white-space:pre-wrap}}
mark{{background:#ffd666}} mark.high{{background:#ff9e9e}} mark.low{{background:none}}
body.showlow mark.low{{background:#ececec}}
.layout .d0{{margin-top:.7rem}} .layout .d1{{padding-left:1.2rem}} .layout .d2{{padding-left:2.6rem}} .layout .d3{{padding-left:4rem}}
.layout .unit{{display:grid;grid-template-columns:minmax(0,1fr) 9.5rem;gap:.8rem;align-items:start}}
.layout .unit>p{{margin:.2rem 0}} .layout .note{{font:12px/1.35 system-ui;color:#4a4a4a;border-left:2px solid #ccc;padding-left:.4rem;margin:.25rem 0}}
.flags{{background:#fff;border:1px solid #bbb;padding:.5rem .5rem .5rem 2rem;font-size:13px}}
.flags li.high b{{color:#a00}} .flags li.medium b{{color:#8a5a00}} .flags q{{font-family:ui-monospace,monospace}} del{{background:#ffd0d0;color:#8a1010}} .restored{{color:#1f5fa8;font-weight:600}} details{{margin-top:.6rem}}
@media (max-width:800px){{.page{{grid-template-columns:1fr}}}}
</style>
<header><b>{title}</b> · {pages} pages · <span id="progress"></span> ·
<a href="review.html">all Acts</a> · <a href="{pdf}">original PDF</a> ·
<label><input type="checkbox" onchange="document.body.classList.toggle('showlow', this.checked)"> show punctuation-only differences</label><br>
<small>Left: the gazette page (authoritative). Right: the reading, laid out like the gazette: sections,
subsections and paragraphs on their own lines, margin notes beside them, tables as tables. Red words: a
number, date or reference the independent check readings disagree with, or words no check reading saw.
Yellow: other disagreements. Struck out: words only the check readings have. Blue numbers were missing
from the main reading and restored where both check readings agree. Orange border: the page has a red
flag. Compare every flag with the image; what is printed wins. Tick a page when it matches (saved only
in this browser).</small></header>
{rows}
<script>
const key = (n) => "oa-review:{act}:" + n;
function update() {{
  const boxes = [...document.querySelectorAll("input[type=checkbox]")];
  const done = boxes.filter((b) => b.checked).length;
  document.getElementById("progress").textContent = done + " of " + boxes.length + " pages checked";
}}
for (const box of document.querySelectorAll("input[type=checkbox]")) {{
  try {{ box.checked = localStorage.getItem(key(box.dataset.page)) === "1"; }} catch (e) {{}}
  box.closest(".page").classList.toggle("done", box.checked);
  box.addEventListener("change", () => {{
    try {{ localStorage.setItem(key(box.dataset.page), box.checked ? "1" : "0"); }} catch (e) {{}}
    box.closest(".page").classList.toggle("done", box.checked);
    update();
  }});
}}
update();
</script>
"""


SEVERITY = {"high": 0, "medium": 1, "low": 2}
KIND = {
    "differs": "reads differently",
    "only_in_primary": "not seen by the check readings",
    "missing_from_primary": "only in the check readings",
    "marker_conflict": "check readings give different numbers",
    "moved": "placed differently by the check readings",
    "not_aligned": "articles differ between the language versions",
    "heading_form": "article heading printed in an unusual form",
    "heading_long": "check where this heading ends",
    "borrowed_ocr": "the page is a picture; its text came from someone else's OCR",
    "one_reading": "the page is a picture read by our OCR; no second reading has checked its words",
    "title_from_listing": "no title in the text was recognised: the title is the source's file name",
    "number_differs": "the number is printed differently in two places",
    "reading_differs": "Document AI read words differently from the OCR layer",
    "accents_restored": "accents restored where the OCR layer dropped them",
}


def reconciled_body(reading: dict) -> tuple[str, str]:
    """Badge and body for one reconciled page: severity-coloured words, flag list, check texts."""
    classes, missing = {}, {}
    for flag in reading["flags"]:
        start, end = flag["tokens"]
        for index in range(start, end):
            current = classes.get(index)
            if current is None or SEVERITY[flag["severity"]] < SEVERITY[current]:
                classes[index] = flag["severity"]
        if flag["kind"] == "missing_from_primary":
            missing[start] = " / ".join(flag["checks"].values())
    counts = {level: sum(f["severity"] == level for f in reading["flags"]) for level in SEVERITY}
    badge = (
        "Document AI · " + " · ".join(f"{n} {level}" for level, n in counts.items() if n)
        if any(counts.values())
        else "Document AI · no flags"
    )
    items = "".join(
        f"<li class='{f['severity']}'><b>{f['severity']}</b> {html.escape(KIND.get(f['kind'], f['kind']))}: "
        f"<q>{html.escape(f['primary'] or '—')}</q> "
        + " ".join(
            f"· {html.escape(name)}: <q>{html.escape(text or '—')}</q>" for name, text in f["checks"].items()
        )
        + "</li>"
        for f in reading["flags"]
        if f["severity"] != "low"
    )
    checks = "".join(
        f"<details><summary>{html.escape(name)} reading</summary><div class='text'>{html.escape(text)}</div></details>"
        for name, text in reading["checks"].items()
    )
    body = (
        (f"<ol class='flags'>{items}</ol>" if items else "")
        + f"<div class='layout'>{layout_parser.render_page(reading['nodes'], reading['page'], classes, missing) or '<em>(no text)</em>'}</div>"
        + checks
    )
    return badge, body


def side_by_side(act: dict, staging: Path) -> Path:
    reading_file = staging / f"{act['id']}.reading.json"
    if reading_file.exists():
        return reconciled_side_by_side(act, staging, json.loads(reading_file.read_text()))
    pages = json.loads((staging / f"{act['id']}.pages.json").read_text())
    extraction = json.loads((staging / f"{act['id']}.extraction.json").read_text())
    ocr = set(extraction["ocr_pages"])
    layout_file = staging / f"{act['id']}.layout.json"
    nodes = json.loads(layout_file.read_text())["nodes"] if layout_file.exists() else None
    images = staging / "pages" / act["id"]
    render_pages(staging / f"{act['id']}.pdf", images)
    rows = []
    for number, text in enumerate(pages, 1):
        source = "OCR — check every word" if number in ocr else "PDF text layer"
        body = f"<div class='text'>{html.escape(text) or '<em>(no text extracted)</em>'}</div>"
        if nodes is not None:
            differing, missing, agreement = layout_parser.disagreements(
                text, layout_parser.page_text(nodes, number)
            )
            source = f"Layout Parser · {agreement:.1%} agrees with {source.split(' —')[0]}"
            body = (
                f"<div class='layout'>{layout_parser.render_page(nodes, number, differing, missing) or '<em>(no text)</em>'}</div>"
                f"<details><summary>First reading (PDF text layer or OCR)</summary>{body}</details>"
            )
        rows.append(
            f"<section class='page{' ocr' if number in ocr else ''}' id='p{number}'>"
            f"<div><img loading='lazy' src='pages/{act['id']}/page-{number}.jpg' alt='Gazette page {number}'></div>"
            f"<div><p><span class='badge'>Page {number} · {source}</span> · "
            f"<label><input type='checkbox' data-page='{number}'> checked against the gazette</label></p>"
            f"{body}</div></section>"
        )
    page = staging / f"{act['id']}.review.html"
    page.write_text(
        SIDE_BY_SIDE.format(
            title=html.escape(act["title"]),
            pages=len(pages),
            pdf=f"{act['id']}.pdf",
            act=act["id"],
            rows="".join(rows),
        )
    )
    return page


def reconciled_side_by_side(act: dict, staging: Path, reading: dict) -> Path:
    render_pages(staging / f"{act['id']}.pdf", staging / "pages" / act["id"])
    rows = []
    missing = reading["summary"].get("missing_checks")
    if missing:
        rows.append(
            f"<p class='flags'><b>Incomplete:</b> the {html.escape(', '.join(missing))} reading failed; "
            "flags below rest on fewer independent checks. Rerun the batch before recording a decision.</p>"
        )
    for page in reading["pages"]:
        number = page["page"]
        badge, body = reconciled_body(page)
        worst = min((SEVERITY[f["severity"]] for f in page["flags"]), default=3)
        rows.append(
            f"<section class='page{' ocr' if worst == 0 else ''}' id='p{number}'>"
            f"<div><img loading='lazy' src='pages/{act['id']}/page-{number}.jpg' alt='Gazette page {number}'></div>"
            f"<div><p><span class='badge'>Page {number} · {html.escape(badge)}</span> · "
            f"<label><input type='checkbox' data-page='{number}'> checked against the gazette</label></p>"
            f"{body}</div></section>"
        )
    path = staging / f"{act['id']}.review.html"
    path.write_text(
        SIDE_BY_SIDE.format(
            title=html.escape(act["title"]),
            pages=len(reading["pages"]),
            pdf=f"{act['id']}.pdf",
            act=act["id"],
            rows="".join(rows),
        )
    )
    return path


def review(manifest: dict, staging: Path = STAGING) -> Path:
    """One private page per collection: provenance, extraction quality and full text per Act."""
    rows, sections = [], []
    for act in manifest["acts"]:
        provenance = json.loads((staging / f"{act['id']}.provenance.json").read_text())
        extraction = json.loads((staging / f"{act['id']}.extraction.json").read_text())
        text = (staging / f"{act['id']}.txt").read_text()
        status = (
            "needs OCR"
            if extraction["needs_ocr"]
            else f"OCR on {len(extraction['ocr_pages'])} pages — check carefully"
            if extraction["ocr_pages"]
            else "text layer"
        )
        gazette = extraction["gazette"]
        rows.append(
            f"<tr><td><a href='{side_by_side(act, staging).name}'>{html.escape(act['title'])}</a>"
            f" (<a href='#{act['id']}'>summary</a>)</td><td>{extraction['pages']}</td>"
            f"<td>{status}</td><td>{len(extraction['outline'])}</td>"
            f"<td>{html.escape(f'No. {gazette["number"]}, Vol. {gazette["volume"]}' if gazette else 'not found')}</td>"
            f"<td><code>{provenance['sha256'][:16]}…</code></td></tr>"
        )
        sections.append(
            f"<section id='{act['id']}'><h2>{html.escape(act['title'])}</h2>"
            f"<p>Source: <a href='{html.escape(act['url'])}'>{html.escape(act['url'])}</a> · retrieved "
            f"{html.escape(provenance['retrieved_at'])} · SHA-256 <code>{provenance['sha256']}</code> · "
            f"PDF: <a href='{act['id']}.pdf'>local copy</a></p>"
            f"<p>Pages {extraction['pages']}; pages without text: {extraction['pages_without_text'] or 'none'}; "
            f"sections detected: {len(extraction['outline'])}; OCR pages: {extraction['ocr_pages'] or 'none'}</p>"
            f"<details><summary>Removed page headers ({len(extraction['removed_furniture'])})</summary>"
            f"<pre>{html.escape(chr(10).join(extraction['removed_furniture']))}</pre></details>"
            f"<details><summary>Extracted text ({extraction['characters']} characters)</summary>"
            f"<pre>{html.escape(text)}</pre></details></section>"
        )
    page = staging / "review.html"
    page.write_text(
        "<!doctype html><meta charset='utf-8'><title>First collection review</title>"
        "<style>body{font:15px/1.5 system-ui;margin:2rem;max-width:72rem}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:.3rem .5rem;text-align:left}pre{white-space:pre-wrap;"
        "background:#f6f6f2;padding:1rem}section{border-top:1px solid #ddd;margin-top:2rem}</style>"
        f"<h1>{html.escape(manifest['collection'])}: review pack (private)</h1>"
        f"<p>{html.escape(manifest['description'])}</p><p>Rights basis: {html.escape(manifest['rights_basis'])}</p>"
        "<p>Compare each extraction with its PDF before recording any decision. Nothing here is published.</p>"
        "<table><tr><th>Act</th><th>Pages</th><th>Text</th><th>Sections</th><th>Gazette</th><th>PDF hash</th></tr>"
        + "".join(rows)
        + "</table>"
        + "".join(sections)
    )
    return page


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["fetch", "extract", "layout", "review"])
    parser.add_argument("--act", help="layout: parse only this manifest id (each call is billed)")
    args = parser.parse_args(argv)
    manifest = load_manifest()
    if args.action == "fetch":
        for record in fetch(manifest):
            print(f"{record['id']}: {record['bytes']} bytes sha256 {record['sha256'][:16]}…")
    elif args.action == "extract":
        for summary in extract(manifest):
            print(summary)
    elif args.action == "layout":
        for summary in layout(manifest, only=args.act):
            print(summary)
    else:
        print(review(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
