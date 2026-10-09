"""Acquire and prepare the first full-text collection for accountable review (operator only).

    python -m ingestion.first_collection fetch     # download each manifest PDF once, politely
    python -m ingestion.first_collection extract   # text, gazette details, section outline
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

from ingestion.pipeline import MAX_BYTES, normalize

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ingestion" / "catalogue" / "ng-first-collection.json"
STAGING = ROOT / "private-staging" / "first-collection"
ALLOWED_HOSTS = {"placng.org"}
USER_AGENT = "OpenAlvary/0.1 (operator review; contact ijidai@ruhu.ai)"
SECTION = re.compile(r"^\s*(\d{1,3}[A-Z]?)\.\s*[—–-]?\s*(\(1\)\s*)?(\S.*)$")
GAZETTE = re.compile(r"No\.\s*(\d+)\s*,?\s*Vol(?:ume)?\.?\s*(\d+)", re.I)
ACT_NUMBER = re.compile(r"\bAct\s+No\.\s*(\d+)\b", re.I)


def load_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(path.read_text())
    for act in manifest["acts"]:
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
    }


def extract(manifest: dict, staging: Path = STAGING) -> list[dict]:
    summaries = []
    for act in manifest["acts"]:
        pdf = staging / f"{act['id']}.pdf"
        if not pdf.exists():
            raise FileNotFoundError(f"{act['id']}: fetch first")
        result = extract_pdf(pdf.read_bytes(), ocr=lambda pages, pdf=pdf: ocr_pages(pdf, pages))
        (staging / f"{act['id']}.txt").write_text(result.pop("text"))
        (staging / f"{act['id']}.extraction.json").write_text(json.dumps(result, indent=2) + "\n")
        keys = ("pages", "needs_ocr", "characters")
        summaries.append(
            {"id": act["id"], **{k: result[k] for k in keys}, "ocr_pages": len(result["ocr_pages"])}
        )
    return summaries


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
            f"<tr><td><a href='#{act['id']}'>{html.escape(act['title'])}</a></td><td>{extraction['pages']}</td>"
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
    parser.add_argument("action", choices=["fetch", "extract", "review"])
    args = parser.parse_args(argv)
    manifest = load_manifest()
    if args.action == "fetch":
        for record in fetch(manifest):
            print(f"{record['id']}: {record['bytes']} bytes sha256 {record['sha256'][:16]}…")
    elif args.action == "extract":
        for summary in extract(manifest):
            print(summary)
    else:
        print(review(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
