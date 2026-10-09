"""Batch reading pipeline for a collection (operator only).

    python -m ingestion.batch plan   [--act ID] [--readers docai,mineru,azure]
    python -m ingestion.batch run    [--act ID] [--readers ...] [--max-billed-pages N]
    python -m ingestion.batch status

Each Act's PDF is read by Document AI (primary) and by independent check readers (MinerU locally,
Azure in the cloud). Readings are cached under private-staging/<collection>/readings/<act>/ keyed
by the PDF's SHA-256 and the reader's exact version, so a rerun resumes where it stopped and never
pays twice for the same page. Billed pages are counted before any call and refused above the
budget. One Act failing does not stop the others. Every run writes a log under runs/.

Nothing here publishes or approves text: the reconciled reading goes to the private review pack.
"""

import argparse
import hashlib
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ingestion import first_collection, reconcile, settings
from ingestion.readers import READERS, page_count

DEFAULT_READERS = ("docai", "mineru", "azure")
DEFAULT_BUDGET = 500  # billed pages per run unless --max-billed-pages or ALVARY_BATCH_MAX_BILLED_PAGES


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Batch:
    def __init__(self, staging: Path, readers: list[str], factories: dict | None = None):
        self.staging, self.names = staging, readers
        self.factories = factories or READERS
        self._readers: dict = {}

    def reader(self, name: str):
        if name not in self._readers:
            self._readers[name] = self.factories[name]()
        return self._readers[name]

    def path(self, act: str, reader: str) -> Path:
        return self.staging / "readings" / act / f"{reader}.json"

    def cached(self, act: str, name: str, digest: str) -> dict | None:
        path = self.path(act, name)
        if not path.exists():
            return None
        stored = json.loads(path.read_text())
        if stored["input_sha256"] != digest or stored["version"] != self.reader(name).version():
            return None
        return stored

    def plan(self, acts: list[dict]) -> list[dict]:
        rows = []
        for act in acts:
            pdf = self.staging / f"{act['id']}.pdf"
            digest, pages = sha256(pdf), page_count(pdf)
            for name in self.names:
                fresh = self.cached(act["id"], name, digest) is not None
                billed = 0 if fresh or not self.reader(name).billed else pages
                rows.append(
                    {"act": act["id"], "reader": name, "pages": pages, "cached": fresh, "billed": billed}
                )
        return rows

    def read(self, act: str, name: str, pdf: Path, digest: str) -> dict:
        started, clock = now(), time.monotonic()
        reading = self.reader(name).read(pdf)
        stored = {
            **asdict(reading),
            "act": act,
            "input_sha256": digest,
            "started_at": started,
            "seconds": round(time.monotonic() - clock, 1),
        }
        path = self.path(act, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        temporary.write_text(json.dumps(stored, ensure_ascii=False))
        temporary.replace(path)  # a reading is either complete or absent
        return stored

    def run_act(self, act: dict) -> dict:
        pdf = self.staging / f"{act['id']}.pdf"
        digest = sha256(pdf)
        readings, outcome = {}, {"act": act["id"], "readers": {}}
        todo = []
        for name in self.names:
            stored = self.cached(act["id"], name, digest)
            if stored:
                readings[name] = stored
                outcome["readers"][name] = {"status": "cached"}
            else:
                todo.append(name)
        with ThreadPoolExecutor(max_workers=max(len(todo), 1)) as pool:
            futures = {name: pool.submit(self.read, act["id"], name, pdf, digest) for name in todo}
            for name, future in futures.items():
                try:
                    readings[name] = future.result()
                    outcome["readers"][name] = {
                        "status": "read",
                        "seconds": readings[name]["seconds"],
                        "billed_pages": readings[name]["billed_pages"],
                    }
                except Exception as error:  # isolate one reader's failure; the run log keeps it
                    outcome["readers"][name] = {
                        "status": "failed",
                        "error": f"{type(error).__name__}: {error}"[:2000],
                        "trace": traceback.format_exc()[-4000:],
                    }
        if "docai" in readings:
            checks = {name: reading for name, reading in readings.items() if name != "docai"}
            reconciled = reconcile.reconcile(readings["docai"], checks)
            target = self.staging / f"{act['id']}.reading.json"
            target.write_text(json.dumps(reconciled, ensure_ascii=False))
            outcome["summary"] = reconciled["summary"]
            outcome["complete"] = all(r["status"] != "failed" for r in outcome["readers"].values())
        else:
            outcome["complete"] = False
        return outcome


def budget(limit: int | None) -> int:
    settings.load()
    return limit or int(os.environ.get("ALVARY_BATCH_MAX_BILLED_PAGES") or DEFAULT_BUDGET)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["plan", "run", "status"])
    parser.add_argument("--act", help="only this manifest id")
    parser.add_argument("--readers", default=",".join(DEFAULT_READERS))
    parser.add_argument("--max-billed-pages", type=int)
    args = parser.parse_args(argv)
    names = [name.strip() for name in args.readers.split(",") if name.strip()]
    unknown = set(names) - set(READERS)
    if unknown or "docai" not in names:
        parser.error(f"readers must include docai and be among {sorted(READERS)}")
    manifest = first_collection.load_manifest()
    acts = [act for act in manifest["acts"] if args.act in (None, act["id"])]
    if not acts:
        parser.error(f"{args.act}: not in the manifest")
    staging = first_collection.STAGING
    batch = Batch(staging, names)
    if args.action == "status":
        for act in acts:
            path = staging / f"{act['id']}.reading.json"
            summary = json.loads(path.read_text())["summary"] if path.exists() else None
            print(f"{act['id']:52} {json.dumps(summary) if summary else 'not reconciled'}")
        return 0
    rows = batch.plan(acts)
    billed = sum(row["billed"] for row in rows)
    for row in rows:
        state = (
            "cached"
            if row["cached"]
            else f"read {row['pages']} pages" + (f", {row['billed']} billed" if row["billed"] else "")
        )
        print(f"{row['act']:52} {row['reader']:7} {state}")
    limit = budget(args.max_billed_pages)
    print(f"Billed pages this run: {billed} (budget {limit})")
    if args.action == "plan":
        return 0
    if billed > limit:
        print("Refused: over budget. Narrow with --act or raise --max-billed-pages.")
        return 2
    run = {"started_at": now(), "readers": names, "billed_pages_planned": billed, "acts": []}
    for act in acts:
        outcome = batch.run_act(act)
        run["acts"].append(outcome)
        states = ", ".join(f"{n} {r['status']}" for n, r in outcome["readers"].items())
        print(f"{act['id']}: {states}; {json.dumps(outcome.get('summary'))}", flush=True)
    run["finished_at"] = now()
    logs = staging / "runs"
    logs.mkdir(exist_ok=True)
    (logs / f"{run['started_at'].replace(':', '')}.json").write_text(json.dumps(run, indent=1))
    first_collection.review(manifest, staging)
    return 0 if all(outcome["complete"] for outcome in run["acts"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
