"""One corpus store copied to another, object by object, and the copy checked (deployment/azure/
README.md, phase 5: Google Cloud Storage to Azure Blob Storage).

    python -m operations.transfer copy SOURCE TARGET [--workers 8]
    python -m operations.transfer verify SOURCE TARGET

SOURCE and TARGET are places in a store, "gs://<bucket>[/<prefix>]" or
"az://<account>/<container>[/<prefix>]": names under the source's prefix land under the target's.

- copy: each object the target lacks is read, checked against the source's MD5 and written with its
  content type and metadata, create-only, so a container with an immutability policy keeps what it
  holds. Objects under state/ (ledgers and catalogues, rewritten by design) are written again when
  their bytes differ. A stopped run resumes where it stopped. Nothing is ever deleted.
- verify: the target holds every source object with the same MD5; objects only in the target are
  listed, never removed. Exit status 1 on any difference.

Only counts and names are printed, never contents.
"""

import argparse
import base64
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

REPLACED = ("state/",)  # rewritten by design: the latest bytes win


def _md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


def _open(place: str):
    from ingestion.corpus_store import open_store, split

    store, prefix = split(place)
    return open_store(store), prefix


def plan(source: dict, target: dict, source_prefix: str, target_prefix: str) -> list[tuple[str, str]]:
    """(source name, target name) for every object to write: missing from the target, or under a
    replaced prefix with other bytes."""
    work = []
    for name, held in sorted(source.items()):
        relative = name[len(source_prefix) :]
        there = target.get(target_prefix + relative)
        if there is None or (relative.startswith(REPLACED) and held["md5"] and there["md5"] != held["md5"]):
            work.append((name, target_prefix + relative))
    return work


def copy(source_place: str, target_place: str, workers: int = 8) -> dict:
    source, source_prefix = _open(source_place)
    target, target_prefix = _open(target_place)
    listed = source.describe(source_prefix)
    work = plan(listed, target.describe(target_prefix), source_prefix, target_prefix)

    def one(names: tuple[str, str]) -> str:
        name, destination = names
        held = listed[name]
        data = source.get(name)
        if held["md5"] and _md5(data) != held["md5"]:
            raise OSError(f"{name} arrived damaged: its bytes do not match the source's MD5")
        replace = destination[len(target_prefix) :].startswith(REPLACED)
        target.put(destination, data, held["content_type"], held["metadata"], replace=replace)
        return destination

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(one, names) for names in work]):
            future.result()
            done += 1
            if done % 500 == 0:
                print(f"{done} of {len(work)} copied", file=sys.stderr, flush=True)
    return {"source_objects": len(listed), "copied": done, "already_there": len(listed) - len(work)}


def verify(source_place: str, target_place: str) -> dict:
    source, source_prefix = _open(source_place)
    target, target_prefix = _open(target_place)
    listed, held = source.describe(source_prefix), target.describe(target_prefix)
    missing, different = [], []
    for name, item in sorted(listed.items()):
        there = held.get(target_prefix + name[len(source_prefix) :])
        if there is None:
            missing.append(name)
        elif item["md5"] and there["md5"] != item["md5"]:
            different.append(name)
    wanted = {target_prefix + name[len(source_prefix) :] for name in listed}
    extra = sorted(name for name in held if name not in wanted)
    return {
        "source_objects": len(listed),
        "missing": missing,
        "different": different,
        "only_in_target": extra,
        "ok": not missing and not different,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    copying = commands.add_parser("copy")
    copying.add_argument("source")
    copying.add_argument("target")
    copying.add_argument("--workers", type=int, default=8)
    checking = commands.add_parser("verify")
    checking.add_argument("source")
    checking.add_argument("target")
    args = parser.parse_args(argv)
    if args.command == "copy":
        print(json.dumps(copy(args.source, args.target, args.workers)))
        return 0
    report = verify(args.source, args.target)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
