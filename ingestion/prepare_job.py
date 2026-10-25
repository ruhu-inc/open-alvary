"""Prepare the whole corpus for review in the cloud: one Cloud Run job, many tasks.

Each task takes every n-th stored file (CLOUD_RUN_TASK_INDEX of CLOUD_RUN_TASK_COUNT), reads it from
the corpus bucket with its readings (the text layer here, Google's OCR and the Layout Parser for
scanned pages), splits it into its instruments and their language versions, and writes the review
documents to collections/<country>/ in the bucket, with an index per file. A file already prepared
by this version of the pipeline is skipped, so an execution that stops is simply run again; each
task assembles the manifest from the indexes when it ends, the last to end seeing them all.

PREPARE_COUNTRY chooses the collection: rw (the default) prepares Rwanda's Gazette and RLRC files;
ng prepares Nigeria's stored files of the sources named in PREPARE_SOURCES (by default the National
Industrial Court's judgment pages, nicn-judgments, read by ingestion.nicn: web pages, no OCR), into
collections/ng/.

The sync ledger and the catalogues come from the bucket (state/<country>/, written by mirror_state
on the operator's machine). The PDF's text layer is read by pdftotext inside this container, which
is the sandbox (ALVARY_TEXT_LAYER=direct); nothing here holds a key.
"""

import json
import os
import sys
import tempfile
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
    """Nigeria's stored files of the chosen sources, each task its share, into collections/ng/. Only the
    National Industrial Court's pages have a reader yet; another source named is refused."""
    from ingestion import nicn

    sources = [s for s in os.environ.get("PREPARE_SOURCES", nicn.SOURCE).split(",") if s]
    unready = sorted(set(sources) - {nicn.SOURCE})
    if unready:
        raise SystemExit(f"no review-document reader yet for {', '.join(unready)} (only {nicn.SOURCE})")
    items = json.loads(store.get("state/ng/sync-ledger.json"))["items"]
    stored = sorted(i for i, r in items.items() if r.get("stored") and r.get("source") in sources)
    mine = share(stored, task, tasks)
    if os.environ.get("PREPARE_LIMIT"):  # a trial: the first few of this task's share
        mine = mine[: int(os.environ["PREPARE_LIMIT"])]
    print(
        f"task {task + 1} of {tasks}: {len(mine)} of {len(stored)} stored files ({', '.join(sources)})",
        flush=True,
    )
    collection = nicn.BucketCollection(store)
    counts = nicn.prepare_items(
        items, mine, nicn.bucket_fetch(store), collection, log=lambda message: print(message, flush=True)
    )
    collection.save_manifest()
    return {"files": len(mine), **counts}


def main() -> int:
    os.environ.setdefault("ALVARY_TEXT_LAYER", "direct")
    task = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    tasks = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    store = corpus_store.open_store()
    if os.environ.get("PREPARE_COUNTRY", rwanda.COUNTRY) == "ng":
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
