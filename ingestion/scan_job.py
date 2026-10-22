"""Read the corpus's scanned pages in the cloud: one Cloud Run job, run once per reader.

    SCAN_READER=ocr     Google's Enterprise Document OCR: every word with its place
    SCAN_READER=layout  the Layout Parser: the second reading that checks the OCR's words
    SCAN_READER=check   Google's OCR of born-digital pages, the check on their text layer (the work
                        list rwanda check-worklist writes; readings/<country>/ocr-check/)

The files to read are the work list in the corpus bucket (rwanda scan-worklist, written with the
operator's approval of the budget). Each of the job's tasks takes every n-th file of the list
(CLOUD_RUN_TASK_INDEX of CLOUD_RUN_TASK_COUNT), reads each file from the bucket and writes its
reading back to readings/<country>/<ocr|docai>/<sha256>.json. A file already read is skipped, so
an execution that stops (a quota, a timeout, a lost task) is simply run again. No task reads more
pages than its share of the approved budget. Nothing here holds a key: the job runs as its own
service account.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from ingestion import corpus_store, rwanda


def share(files: list[dict], task: int, tasks: int) -> list[dict]:
    """This task's files: every `tasks`-th of the list (largest first), starting at `task`."""
    return [f for k, f in enumerate(files) if k % tasks == task]


def main() -> int:
    kind = os.environ["SCAN_READER"]
    if kind not in ("ocr", "layout", "check"):
        raise SystemExit("SCAN_READER is ocr, layout or check")
    task = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    tasks = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    files_at_once = int(os.environ.get("SCAN_FILES_AT_ONCE", "2"))
    store = corpus_store.open_store()
    if kind == "check":
        return check(store, task, tasks)
    worklist = json.loads(store.get(rwanda.WORKLIST))
    mine = share(worklist["files"], task, tasks)
    budget = sum(f["scanned_pages"] for f in mine)
    if worklist["pages"] > worklist["approval"]["max_pages"]:
        raise SystemExit("the work list exceeds its approved budget")
    print(
        f"task {task + 1} of {tasks}: {len(mine)} files, {budget} scanned pages, reader {kind}",
        flush=True,
    )
    with tempfile.TemporaryDirectory() as scratch:
        rwanda.STAGING = Path(scratch)
        result = rwanda.read_scans(
            log=lambda message: print(message, flush=True),
            store=store,
            max_pages=budget,
            files_at_once=files_at_once,
            kind=kind,
            worklist=mine,
        )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    # A stop for the budget is the end of the share; any other stop is a failure to look at.
    return 1 if result["stopped"] and "budget" not in result["stopped"] else 0


def check(store, task: int, tasks: int) -> int:
    """This task's share of the born-digital pages Google's OCR checks (rwanda.check_layers)."""
    worklist = json.loads(store.get(rwanda.CHECK_WORKLIST))
    if worklist["pages"] > worklist["approval"]["max_pages"]:
        raise SystemExit("the work list exceeds its approved budget")
    mine = share(worklist["files"], task, tasks)
    budget = sum(len(f["pages"]) for f in mine)
    print(f"task {task + 1} of {tasks}: {len(mine)} files, {budget} pages to check", flush=True)
    with tempfile.TemporaryDirectory() as scratch:
        rwanda.STAGING = Path(scratch)
        result = rwanda.check_layers(store, mine, budget, log=lambda message: print(message, flush=True))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result["stopped"] and "budget" not in result["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
