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
import os
import tempfile
from io import BytesIO
from pathlib import Path

from ingestion import corpus_store, gazette_layout, ruled_tables
from ingestion.sync import now, stamp

FOLDERS = {"layout": "docai", "ocr": "ocr"}
PRICE_PER_1000 = {"layout": 10.0, "ocr": 1.5}  # Document AI list prices, USD per 1,000 pages
# Pages a minute a run sends, all tasks together, under the project's allowance (SCAN_PAGES_PER_MINUTE
# overrides): the Layout Parser ran at about 33 a minute, refused for its rate when pushed harder.
PACE = {"layout": 30}
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


CHECKPOINT = 50  # pages read and saved together: a failure loses at most these


def read(
    store, country: str, kind: str, files: list[dict], max_pages: int, log=print, reader=None, retry=True
) -> dict:
    """Each file's listed pages not yet read by this reader, read CHECKPOINT pages at a time, each group
    cut into one PDF inside this job's container, read and merged by page into the stored reading
    before the next is read, so a file that fails keeps what was read and a run again resumes it.
    `max_pages` caps the pages billed (the service bills every page it reads, also in a file that then
    fails); a refusal (billing, permission, sign-in) or five failures in a row stop the run; files that
    failed are tried once more before it ends."""
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
    counted = hasattr(reader, "billed")  # a reader that counts the pages billed; else the pages read
    start = reader.billed if counted else 0
    totals = {"pages": 0, "files": 0}
    state = {"in_a_row": 0, "stop": None}

    def billed() -> int:
        return (reader.billed - start) if counted else totals["pages"]

    def read_file(item: dict) -> None:
        sha = item["sha256"]
        name = f"readings/{country}/{folder}/{sha}.json"
        earlier = json.loads(store.get(name)) if store.exists(name) else {"pages": []}
        done = {page["page"] for page in earlier["pages"]}
        left = [n for n in item["pages"] if n not in done]
        if not left:
            return
        source = PdfReader(BytesIO(store.get(item["source_object"])))
        left = [n for n in left if 1 <= n <= len(source.pages)]
        pages = {page["page"]: page for page in earlier["pages"]}
        runs = list(earlier.get("runs", []))
        read_now = 0
        for at in range(0, len(left), CHECKPOINT):
            group = left[at : at + CHECKPOINT]
            if billed() + len(group) > max_pages:
                state["stop"] = "budget"
                break
            with tempfile.TemporaryDirectory() as scratch:
                writer = PdfWriter()
                for number in group:
                    writer.add_page(source.pages[number - 1])
                buffer = BytesIO()
                writer.write(buffer)
                part = Path(scratch) / "pages.pdf"
                part.write_bytes(buffer.getvalue())
                reading = reader.read(part)
            if len(reading.pages) != len(group):
                raise ValueError(f"{len(reading.pages)} pages read of {len(group)}")
            pages.update(
                {number: {**page, "page": number} for number, page in zip(group, reading.pages, strict=True)}
            )
            runs.append({"pages": group, "read_at": stamp(now())})
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
            read_now += len(group)
            totals["pages"] += len(group)
        if read_now:
            totals["files"] += 1
            log(f"{sha[:12]}: {read_now} pages read by {kind} ({totals['pages']} so far, {billed()} billed)")

    def go(items: list[dict]) -> list[dict]:
        failed = []
        for item in items:
            if state["stop"]:
                break
            try:
                read_file(item)
                state["in_a_row"] = 0
            except stopping as error:
                state["stop"] = f"refused: {type(error).__name__}"
            except Exception as error:  # noqa: BLE001 - one file's failure is noted; the run goes on
                failed.append(
                    {"sha256": item["sha256"], "error": f"{type(error).__name__}: {str(error)[:200]}"}
                )
                log(f"{item['sha256'][:12]}: failed ({type(error).__name__}); what it read is kept")
                state["in_a_row"] += 1
                if state["in_a_row"] >= 5:
                    state["stop"] = "five failures in a row"
        return failed

    failed = go(files)
    retried = []
    if retry and failed and not state["stop"]:
        retried = [f["sha256"] for f in failed]
        by_sha = {item["sha256"]: item for item in files}
        log(f"trying the {len(retried)} files that failed once more")
        state["in_a_row"] = 0
        failed = go([by_sha[sha] for sha in retried])
    return {
        "files": totals["files"],
        "pages": totals["pages"],
        "billed_pages": billed(),
        "failed": failed,
        "retried": retried,
        "stopped": state["stop"],
    }


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
    # A run approved apart from the work list (a re-run of failed files): the pages it may bill, all tasks.
    if os.environ.get("SCAN_MAX_PAGES"):
        budget = int(os.environ["SCAN_MAX_PAGES"]) // tasks
    per_minute = float(os.environ.get("SCAN_PAGES_PER_MINUTE") or PACE.get(kind) or 0) / tasks
    reader = None
    if per_minute:
        from ingestion import layout_parser, readers

        reader = readers.DocAIReader() if kind == "layout" else readers.DocOCRReader()
        reader.preflight()
        reader.pace = layout_parser.Pace(per_minute)
    log(
        f"task {task + 1} of {tasks}: {len(mine)} files, up to {budget} pages billed, reader {kind}, "
        f"{per_minute or 'unpaced'} pages a minute"
    )
    started = stamp(now())
    result = read(store, country, kind, mine, budget, log=log, reader=reader)
    log(json.dumps(result, ensure_ascii=False))
    report = {
        **result,
        "reader": kind,
        "task": task + 1,
        "tasks": tasks,
        "started_at": started,
        "finished_at": stamp(now()),
        "budget_pages": budget,
        "pages_per_minute": per_minute or None,
        "approval": os.environ.get("SCAN_APPROVAL") or work["approval"].get("basis"),
        "cost_usd": round(result["billed_pages"] * PRICE_PER_1000[kind] / 1000, 2),
    }
    store.put(
        f"work/{country}/runs/{kind}/{started.replace(':', '')}-task{task + 1}-of-{tasks}.json",
        json.dumps(report, ensure_ascii=False).encode(),
        "application/json",
        replace=True,
    )
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
