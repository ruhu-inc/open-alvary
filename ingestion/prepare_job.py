"""Prepare the whole corpus for review in the cloud: one Cloud Run job, many tasks.

Each task takes every n-th stored file (CLOUD_RUN_TASK_INDEX of CLOUD_RUN_TASK_COUNT), reads it from
the corpus bucket with its readings (the text layer here, Google's OCR and the Layout Parser for
scanned pages), splits it into its instruments and their language versions, and writes the review
documents to collections/<country>/ in the bucket, with an index per file. A file already prepared
by this version of the pipeline is skipped, so an execution that stops is simply run again; each
task assembles the manifest from the indexes when it ends, the last to end seeing them all.

PREPARE_COUNTRY chooses the collection: rw (the default) prepares Rwanda's Gazette and RLRC files;
ng prepares Nigeria's stored files of the sources named in PREPARE_SOURCES into collections/ng/: by
default the National Industrial Court's judgment pages (nicn-judgments, read by ingestion.nicn: web
pages, no OCR); a PDF source's files (nass-acts, nln-repository, supreme-court …) once the scan-reader
job has read them with the Layout Parser, checked by the OCR (ingestion.ng_documents), the rest
waiting.

PREPARE_MODE=details writes no document either: each work's first draft of details is measured
(workbench.details_audit) into work/<country>/audit/details/<execution>/.

PREPARE_MODE=audit writes no document: each file is prepared as usual into a collection that keeps,
instead, where every printed line went (ingestion.accounting), and each task writes its files'
accounts to work/<country>/audit/lines/<execution>/task-NNN-of-NNN.json; `python -m
ingestion.accounting summary <folder>` sums them once downloaded (owner, 2026-10-29: measure before
fixing).

The sync ledger and the catalogues come from the bucket (state/<country>/, written by mirror_state
on the operator's machine). The PDF's text layer is read by pdftotext inside this container, which
is the sandbox (ALVARY_TEXT_LAYER=direct); nothing here holds a key.
"""

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ingestion import corpus_store, rwanda


def share(items: list[str], task: int, tasks: int) -> list[str]:
    return [item for k, item in enumerate(items) if k % tasks == task]


def fetch_state(store, staging: Path, country: str = rwanda.COUNTRY) -> None:
    """The ledger and catalogues the pipeline reads, from the bucket into this task's folder."""
    prefix = f"state/{country}/"
    for name in [f"{prefix}sync-ledger.json", *store.list(f"{prefix}catalogue/")]:
        if name.endswith(".json"):
            target = staging / name[len(prefix) :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(store.get(name))


def prepare_nigeria(store, task: int, tasks: int) -> dict:
    """Nigeria's stored files of the chosen sources, each task its share, into collections/ng/: the
    National Industrial Court's pages by their reader (ingestion.nicn); the PDF sources' files whose
    Layout Parser reading has arrived (ingestion.ng_documents), the rest waiting. A source with no
    reader here is refused."""
    from ingestion import ng_documents, nicn

    sources = [s for s in os.environ.get("PREPARE_SOURCES", nicn.SOURCE).split(",") if s]
    unready = sorted(set(sources) - {nicn.SOURCE} - ng_documents.PDF_SOURCES)
    if unready:
        raise SystemExit(f"no review-document reader yet for {', '.join(unready)}")
    items = json.loads(store.get("state/ng/sync-ledger.json"))["items"]
    stored = sorted(i for i, r in items.items() if r.get("stored") and r.get("source") in sources)
    mine = share(stored, task, tasks)
    if os.environ.get("PREPARE_LIMIT"):  # a trial: the first few of this task's share
        mine = mine[: int(os.environ["PREPARE_LIMIT"])]
    print(
        f"task {task + 1} of {tasks}: {len(mine)} of {len(stored)} stored files ({', '.join(sources)})",
        flush=True,
    )

    def log(message: str) -> None:
        print(message, flush=True)

    pages = [i for i in mine if items[i]["source"] == nicn.SOURCE]
    pdfs = [i for i in mine if items[i]["source"] != nicn.SOURCE]
    counts: dict = {"files": len(mine)}
    if pages:
        collection = nicn.BucketCollection(store)
        counts[nicn.SOURCE] = nicn.prepare_items(items, pages, nicn.bucket_fetch(store), collection, log=log)
    if pdfs:
        collection = nicn.BucketCollection(store, pipeline=ng_documents.PIPELINE)
        counts["pdf"] = ng_documents.prepare_pdfs(store, items, pdfs, collection, log=log)
    nicn.BucketCollection(store).save_manifest()
    return counts


def write_audit(store, country: str, task: int, tasks: int, payload: dict, kind: str = "lines") -> str:
    """One task's accounts (kind "lines", or "details": workbench.details_audit), in the bucket beside
    the country's other work."""
    run = os.environ.get("CLOUD_RUN_EXECUTION") or datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    name = f"work/{country}/audit/{kind}/{run}/task-{task + 1:03d}-of-{tasks:03d}.json"
    store.put(name, json.dumps(payload, ensure_ascii=False).encode(), "application/json", replace=True)
    return name


def audit_nigeria(store, task: int, tasks: int) -> dict:
    """Nigeria's stored files of the chosen sources, each task its share, accounted for and nothing
    written: a court page by its words (ingestion.nicn), a PDF read by the Layout Parser by its pages
    and lines (ingestion.ng_documents); a PDF not yet read is listed as waiting."""
    from ingestion import accounting, ng_documents, nicn

    sources = [s for s in os.environ.get("PREPARE_SOURCES", nicn.SOURCE).split(",") if s]
    items = json.loads(store.get("state/ng/sync-ledger.json"))["items"]
    stored = sorted(i for i, r in items.items() if r.get("stored") and r.get("source") in sources)
    mine = share(stored, task, tasks)
    if os.environ.get("PREPARE_LIMIT"):
        mine = mine[: int(os.environ["PREPARE_LIMIT"])]
    reports, failed, waiting = [], [], []
    for item_id in mine:
        record = items[item_id]
        sha = record.get("sha256")
        try:
            if record["source"] == nicn.SOURCE:
                raw = store.get(f"sources/ng/{nicn.SOURCE}/{sha}.html")
                report = accounting.nicn_page(raw, nicn.read(raw))
            else:
                layout, check = f"readings/ng/docai/{sha}.json", f"readings/ng/ocr/{sha}.json"
                if not str(record.get("stored", "")).endswith(".pdf") or not store.exists(layout):
                    waiting.append(item_id)
                    continue
                docai = json.loads(store.get(layout))
                ocr = json.loads(store.get(check)) if store.exists(check) else None
                error = None
                try:
                    reading = ng_documents.cloud_reading(docai, ocr)
                except Exception as problem:  # noqa: BLE001 - the check's failure is itself measured
                    error = f"{type(problem).__name__}: {str(problem)[:200]}"
                    reading = ng_documents.cloud_reading(docai, None)
                parts = [part for _, part in ng_documents.documents(reading, record)]
                report = accounting.ng_pdf(reading, parts, ocr_check_error=error)
            reports.append(
                {
                    "sha": sha,
                    "item": item_id,
                    "source": record["source"],
                    "name": (record.get("listed") or {}).get("title") or item_id,
                    "url": record.get("url"),
                    **report,
                }
            )
        except Exception as error:  # noqa: BLE001 - noted, the rest go on
            failed.append({"item": item_id, "error": f"{type(error).__name__}: {str(error)[:300]}"})
    return {"country": "ng", "sources": sources, "reports": reports, "failed": failed, "waiting": waiting}


def audit_rwanda(store, ledger: dict, mine: list[str]) -> dict:
    """Rwanda's files, each prepared into an AuditCollection: nothing written, every line accounted
    for. A file prepared without an account (a Word document, a file not in the bucket) is listed."""
    collection = rwanda.AuditCollection()
    failed = []
    for item in mine:
        try:
            rwanda.prepare_selection(
                {"items": [item]},
                log=lambda message: None,
                collection=collection,
                store=store,
                skip_done=False,
                save=False,
            )
        except Exception as error:  # noqa: BLE001 - noted, the rest go on
            failed.append({"item": item, "error": f"{type(error).__name__}: {str(error)[:300]}"})
    reported = {report["sha"] for report in collection.reports}
    unreported = [
        {"item": item, "source": ledger[item].get("source"), "format": ledger[item].get("format", "pdf")}
        for item in mine
        if ledger[item].get("sha256") not in reported and item not in {f["item"] for f in failed}
    ]
    return {"country": "rw", "reports": collection.reports, "failed": failed, "not_reported": unreported}


def main() -> int:
    os.environ.setdefault("ALVARY_TEXT_LAYER", "direct")
    task = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    tasks = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    store = corpus_store.open_store()
    if os.environ.get("PREPARE_MODE") == "details":  # each work's first draft of details, measured
        from workbench import details_audit

        country = os.environ.get("PREPARE_COUNTRY", rwanda.COUNTRY)
        limit = int(os.environ["PREPARE_LIMIT"]) if os.environ.get("PREPARE_LIMIT") else None
        payload = details_audit.audit_collection(store, country, task, tasks, limit)
        name = write_audit(store, country, task, tasks, payload, kind="details")
        print(json.dumps({"audit": name, "works": len(payload["reports"]), "failed": len(payload["failed"])}))
        return 0
    audit = os.environ.get("PREPARE_MODE") == "audit"
    if os.environ.get("PREPARE_COUNTRY", rwanda.COUNTRY) == "ng":
        if audit:
            payload = audit_nigeria(store, task, tasks)
            name = write_audit(store, "ng", task, tasks, payload)
            print(
                json.dumps(
                    {"audit": name, "files": len(payload["reports"]), "failed": len(payload["failed"])}
                )
            )
            return 0
        print(json.dumps(prepare_nigeria(store, task, tasks)), flush=True)
        return 0
    with tempfile.TemporaryDirectory() as scratch:
        rwanda.STAGING = Path(scratch)
        fetch_state(store, rwanda.STAGING)
        ledger = json.loads((rwanda.STAGING / "sync-ledger.json").read_text())["items"]
        # Each stored file once, before the files are shared out: a file listed twice must not be
        # prepared by two tasks at once (rwanda.one_per_file).
        items = rwanda.one_per_file(ledger, sorted(i for i, record in ledger.items() if record.get("stored")))
        mine = share(items, task, tasks)
        if os.environ.get("PREPARE_LIMIT"):  # a trial: the first few of this task's share
            mine = mine[: int(os.environ["PREPARE_LIMIT"])]
        print(f"task {task + 1} of {tasks}: {len(mine)} of {len(items)} stored files", flush=True)
        if audit:
            payload = audit_rwanda(store, ledger, mine)
            name = write_audit(store, "rw", task, tasks, payload)
            print(
                json.dumps(
                    {"audit": name, "files": len(payload["reports"]), "failed": len(payload["failed"])}
                )
            )
            return 0
        collection = rwanda.BucketCollection(store)
        failed, instruments = [], 0
        # File by file: a file that cannot be prepared is noted and the task goes on.
        for item in mine:
            try:
                result = rwanda.prepare_selection(
                    {"items": [item]},
                    log=lambda message: print(message, flush=True),
                    collection=collection,
                    store=store,
                    skip_done=True,
                    save=False,
                )
                instruments += len(result["instruments"])
            except Exception as error:  # noqa: BLE001 - noted and left for a later run
                failed.append(item)
                print(f"failed {item}: {type(error).__name__}: {str(error)[:300]}", flush=True)
        collection.save_manifest(rwanda.new_manifest())
    print(json.dumps({"files": len(mine), "instruments": instruments, "failed": len(failed)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
