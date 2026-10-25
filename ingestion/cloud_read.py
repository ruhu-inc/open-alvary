"""Reading a collection's stored files in the cloud, for every collection but the Rwandan Gazette
(which reads its own way: rwanda.read_scans and rwanda.check_layers). Run by the scan-reader job
with SCAN_COUNTRY set:

    SCAN_READER=survey   count each stored PDF's pages, its pictures of pages (scans) and its pages
                         with a text layer, free of charge, before any reading is billed; kept at
                         work/<country>/survey/<sha256>.json and summed by `summary`
    SCAN_READER=layout   the Layout Parser on each file's listed pages: the main reading
    SCAN_READER=ocr      Google's OCR on the same pages: the independent reading that checks it

The readers take the reading work list (work/<country>/reading.json), which the operator writes
from the survey with the approval of its budget (`worklist`): each file's pages, largest file
first. A reading is kept at readings/<country>/<docai|ocr>/<sha256>.json, its pages under their own
numbers; a file read in part before keeps those pages and gains the rest, and a stored reading that
cannot be read fails its file rather than be replaced. A stopped run is run again: what is read is
skipped. Web pages and Word files are not read here (they carry their own text).
"""

import json
import tempfile
from io import BytesIO
from pathlib import Path

from ingestion import corpus_store, gazette_layout, ruled_tables
from ingestion.sync import now, stamp

FOLDERS = {"layout": "docai", "ocr": "ocr"}
PRICE_PER_1000 = {"layout": 10.0, "ocr": 1.5}  # Document AI list prices, USD per 1,000 pages
TEXT_PAGE_WORDS = 20  # a page with fewer words in its layer has no text layer to speak of


def survey_name(country: str, sha: str) -> str:
    return f"work/{country}/survey/{sha}.json"


def worklist_name(country: str) -> str:
    return f"work/{country}/reading.json"


def stored_files(store, country: str) -> list[str]:
    """The PDFs stored for the collection: sources/<country>/<source>/<sha256>.pdf."""
    return sorted(name for name in store.list(f"sources/{country}/") if name.endswith(".pdf"))


def survey_file(pdf: Path) -> dict:
    """One PDF's pages, its pictures of pages and its pages with a text layer (free: no service)."""
    from pypdf import PdfReader

    pages = len(PdfReader(str(pdf)).pages)
    found = {"pages": pages, "scanned_pages": sorted(ruled_tables.image_pages(pdf))}
    try:
        layer = gazette_layout.text_layer(pdf)
    except Exception as error:  # noqa: BLE001 - its pages still count, and are read; only its layer is not
        return {**found, "text_pages": [], "text_layer_error": f"{type(error).__name__}: {str(error)[:200]}"}
    return {
        **found,
        "text_pages": [n for n, page in enumerate(layer, 1) if len(page["words"]) >= TEXT_PAGE_WORDS],
    }


def survey(store, country: str, names: list[str], log=print) -> dict:
    """Measure each listed file not yet measured; a file that cannot be read is noted, not fatal."""
    measured = failed = 0
    for name in names:
        sha = name.rsplit("/", 1)[-1][: -len(".pdf")]
        if store.exists(survey_name(country, sha)) and "pages" in json.loads(
            store.get(survey_name(country, sha))
        ):
            continue  # measured; a file that failed before is measured again
        try:
            with tempfile.TemporaryDirectory() as scratch:
                pdf = Path(scratch) / "file.pdf"
                pdf.write_bytes(store.get(name))
                found = survey_file(pdf)
        except Exception as error:  # noqa: BLE001 - a damaged file is noted; the survey goes on
            found = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
            failed += 1
        record = {"source_object": name, "sha256": sha, "measured_at": stamp(now()), **found}
        store.put(survey_name(country, sha), json.dumps(record).encode(), "application/json", replace=True)
        measured += 1
        log(f"{sha[:12]}: {found.get('pages', '?')} pages, {len(found.get('scanned_pages', []))} scanned")
    return {"measured": measured, "failed": failed}


def summary(store, country: str) -> dict:
    """The survey summed, with the reading's cost: the Layout Parser and the OCR on every page."""
    records = [json.loads(store.get(name)) for name in store.list(f"work/{country}/survey/")]
    good = [r for r in records if "pages" in r]
    pages = sum(r["pages"] for r in good)
    scanned = sum(len(r["scanned_pages"]) for r in good)
    return {
        "files": len(good),
        "unreadable": [r["source_object"] for r in records if "pages" not in r],
        "pages": pages,
        "scanned_pages": scanned,
        "text_pages": sum(len(r["text_pages"]) for r in good),
        "cost_usd": {kind: round(pages * price / 1000, 2) for kind, price in PRICE_PER_1000.items()},
    }


def worklist(store, country: str, approval: dict, skip: dict[str, dict] | None = None) -> dict:
    """Every surveyed file's pages, largest first, for the readers, with the budget's approval. A file
    in `skip` (by sha256: another copy of a document read instead, with the copy read and the reason)
    is left out and listed as not read, so one text is read once."""
    files, not_read = [], []
    for name in store.list(f"work/{country}/survey/"):
        record = json.loads(store.get(name))
        if record.get("pages") and record["sha256"] in (skip or {}):
            not_read.append({"sha256": record["sha256"], "pages": record["pages"], **skip[record["sha256"]]})
        elif record.get("pages"):
            files.append(
                {
                    "sha256": record["sha256"],
                    "source_object": record["source_object"],
                    "pages": list(range(1, record["pages"] + 1)),
                }
            )
    files.sort(key=lambda f: (-len(f["pages"]), f["sha256"]))
    return {
        "made_at": stamp(now()),
        "approval": approval,
        "pages": sum(len(f["pages"]) for f in files),
        "files": files,
        "not_read": sorted(not_read, key=lambda f: f["sha256"]),
    }


def read(store, country: str, kind: str, files: list[dict], max_pages: int, log=print, reader=None) -> dict:
    """Each file's listed pages not yet read by this reader, cut into one PDF inside this job's
    container, read, and merged by page into the stored reading. No file is started that would take
    the pages past `max_pages`; a refusal (billing, permission, sign-in) or five failures in a row
    stop the run."""
    import google.api_core.exceptions
    from pypdf import PdfReader, PdfWriter

    from ingestion import readers

    if reader is None:
        reader = readers.DocAIReader() if kind == "layout" else readers.DocOCRReader()
        reader.preflight()
    stopping = (
        google.api_core.exceptions.PermissionDenied,
        google.api_core.exceptions.Unauthenticated,
        google.api_core.exceptions.Forbidden,
    )
    folder = FOLDERS[kind]
    pages_read = files_read = in_a_row = 0
    failed, stop = [], None
    for item in files:
        sha = item["sha256"]
        name = f"readings/{country}/{folder}/{sha}.json"
        try:
            earlier = json.loads(store.get(name)) if store.exists(name) else {"pages": []}
            done = {page["page"] for page in earlier["pages"]}
            left = [n for n in item["pages"] if n not in done]
            if not left:
                continue
            if pages_read + len(left) > max_pages:
                stop = "budget"
                break
            with tempfile.TemporaryDirectory() as scratch:
                source = PdfReader(BytesIO(store.get(item["source_object"])))
                left = [n for n in left if 1 <= n <= len(source.pages)]
                writer = PdfWriter()
                for number in left:
                    writer.add_page(source.pages[number - 1])
                buffer = BytesIO()
                writer.write(buffer)
                part = Path(scratch) / "pages.pdf"
                part.write_bytes(buffer.getvalue())
                reading = reader.read(part)
            if len(reading.pages) != len(left):
                raise ValueError(f"{len(reading.pages)} pages read of {len(left)}")
            pages = {page["page"]: page for page in earlier["pages"]}
            pages.update(
                {number: {**page, "page": number} for number, page in zip(left, reading.pages, strict=True)}
            )
            runs = [*earlier.get("runs", []), {"pages": left, "read_at": stamp(now())}]
            raw = getattr(reading, "raw", {}) or {}
            if raw.get("layout"):  # the service's own layout, so a converter fix needs no new reading
                runs[-1]["layout"] = raw["layout"]
            kept = {
                "sha256": sha,
                "reader": reader.name,
                "version": reader.version(),
                "pages": [pages[number] for number in sorted(pages)],
                "runs": runs,
            }
            store.put(name, json.dumps(kept, ensure_ascii=False).encode(), "application/json", replace=True)
            pages_read += len(left)
            files_read += 1
            in_a_row = 0
            log(f"{sha[:12]}: {len(left)} pages read by {kind} ({pages_read} so far)")
        except stopping as error:
            stop = f"refused: {type(error).__name__}"
            break
        except Exception as error:  # noqa: BLE001 - one file's failure is noted; the run goes on
            failed.append({"sha256": sha, "error": f"{type(error).__name__}: {str(error)[:200]}"})
            log(f"{sha[:12]}: failed ({type(error).__name__})")
            in_a_row += 1
            if in_a_row >= 5:
                stop = "five failures in a row"
                break
    return {"files": files_read, "pages": pages_read, "failed": failed, "stopped": stop}


def share(items: list, task: int, tasks: int) -> list:
    return [item for k, item in enumerate(items) if k % tasks == task]


def run(kind: str, country: str, task: int, tasks: int, log=print) -> int:
    """One task of the scan-reader job for a collection read here (scan_job, SCAN_COUNTRY)."""
    store = corpus_store.open_store()
    if kind == "survey":
        mine = share(stored_files(store, country), task, tasks)
        log(f"task {task + 1} of {tasks}: {len(mine)} files to measure")
        result = survey(store, country, mine, log=log)
        log(json.dumps(result))
        return 0
    if kind not in FOLDERS:
        raise SystemExit("SCAN_READER is survey, layout or ocr for this collection")
    work = json.loads(store.get(worklist_name(country)))
    if work["pages"] > work["approval"]["max_pages"]:
        raise SystemExit("the work list exceeds its approved budget")
    mine = share(work["files"], task, tasks)
    budget = sum(len(f["pages"]) for f in mine)
    log(f"task {task + 1} of {tasks}: {len(mine)} files, {budget} pages, reader {kind}")
    result = read(store, country, kind, mine, budget, log=log)
    log(json.dumps(result, ensure_ascii=False))
    return 1 if result["stopped"] and "budget" not in result["stopped"] else 0


def main(argv=None) -> int:
    """python -m ingestion.cloud_read summary --country ng
    python -m ingestion.cloud_read worklist --country ng --approved-by NAME --max-pages N --basis TEXT"""
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["summary", "worklist"])
    parser.add_argument("--country", required=True)
    parser.add_argument("--approved-by")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--basis", help="where and when the budget was approved")
    parser.add_argument(
        "--skip", type=Path, help="JSON: copies not to read, by sha256, each with the copy read"
    )
    args = parser.parse_args(argv)
    store = corpus_store.open_store()
    if args.command == "summary":
        print(json.dumps(summary(store, args.country), indent=1))
        return 0
    if not (args.approved_by and args.max_pages and args.basis):
        parser.error("a work list needs --approved-by, --max-pages and --basis")
    approval = {"approved_by": args.approved_by, "max_pages": args.max_pages, "basis": args.basis}
    work = worklist(store, args.country, approval, json.loads(args.skip.read_text()) if args.skip else None)
    if work["pages"] > args.max_pages:
        parser.error(f"{work['pages']} pages exceed the approved {args.max_pages}")
    store.put(worklist_name(args.country), json.dumps(work).encode(), "application/json", replace=True)
    print(
        json.dumps(
            {
                "files": len(work["files"]),
                "pages": work["pages"],
                "not_read": len(work["not_read"]),
                "not_read_pages": sum(f["pages"] for f in work["not_read"]),
                "object": worklist_name(args.country),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
