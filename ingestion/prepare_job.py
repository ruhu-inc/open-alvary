"""Prepare the whole corpus for review in the cloud: one Cloud Run job, many tasks.

Each task takes every n-th stored file (CLOUD_RUN_TASK_INDEX of CLOUD_RUN_TASK_COUNT), reads it from
the corpus bucket with its readings (the text layer here, Google's OCR and the Layout Parser for
scanned pages), splits it into its instruments and their language versions, and writes the review
documents to collections/<country>/ in the bucket, with an index per file. A file already prepared
by this version of the pipeline is skipped, so an execution that stops is simply run again; each
task assembles the manifest from the indexes when it ends, the last to end seeing them all.

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


def fetch_state(store, staging: Path) -> None:
    """The ledger and catalogues the pipeline reads, from the bucket into this task's folder."""
    prefix = f"state/{rwanda.COUNTRY}/"
    for name in [f"{prefix}sync-ledger.json", *store.list(f"{prefix}catalogue/")]:
        if name.endswith(".json"):
            target = staging / name[len(prefix) :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(store.get(name))


def main() -> int:
    os.environ.setdefault("ALVARY_TEXT_LAYER", "direct")
    task = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    tasks = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    store = corpus_store.CorpusStore()
    with tempfile.TemporaryDirectory() as scratch:
        rwanda.STAGING = Path(scratch)
        fetch_state(store, rwanda.STAGING)
        ledger = json.loads((rwanda.STAGING / "sync-ledger.json").read_text())["items"]
        items = sorted(i for i, record in ledger.items() if record.get("stored"))
        mine = share(items, task, tasks)
        if os.environ.get("PREPARE_LIMIT"):  # a trial: the first few of this task's share
            mine = mine[: int(os.environ["PREPARE_LIMIT"])]
        print(f"task {task + 1} of {tasks}: {len(mine)} of {len(items)} stored files", flush=True)
        result = rwanda.prepare_selection(
            {"items": mine},
            log=lambda message: print(message, flush=True),
            collection=rwanda.BucketCollection(store),
            store=store,
            skip_done=True,
        )
    print(
        json.dumps({"documents": result["documents"], "instruments": len(result["instruments"])}), flush=True
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
