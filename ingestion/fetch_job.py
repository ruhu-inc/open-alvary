"""Fetch a country's approved work list of catalogued documents in the cloud: the Cloud Run job
corpus-fetcher (deployment/gcp/ingestion_jobs.sh fetch).

The work list is in the corpus bucket (work/<country>/fetch.json, written with the operator's
approval by `python -m ingestion.nigeria fetch-worklist`). It is shared out by host: each host's
items go to one task only (CLOUD_RUN_TASK_INDEX of CLOUD_RUN_TASK_COUNT), so the wait between
requests to a host holds across the job, and the busiest hosts are spread first. Each task:

- fetches its items politely (ingestion.polite: who we are, robots.txt, at least the work list's
  delay between requests to a host, its stated rate limit, redirects only onto the source's hosts),
  with a stored item's validators (If-None-Match, If-Modified-Since), so an unchanged file is not
  taken again;
- sniffs each answer's type (a PDF or Word document, a web page, the court's JSON record) and
  enforces its size limit; it parses nothing else: reading the files is a later step;
- stores a file once, by its fingerprint: sources/<country>/<source>/<sha256>.<ext> with a sidecar
  <sha256>.json (the address, the final address after redirects, when, the status, content type,
  length, ETag and Last-Modified, the fingerprint, the listing's details and the item's id). A file
  already stored from another listing (another source, or a later run) is not stored again: the
  listing is added to it as another occurrence (<sha256>.occurrences/<item key>.json, and the
  sidecar's other_sources);
- records each item done (work/<country>/fetched/<key>.json, and a marker under fetch-done/<work
  list id>/, so a stopped run is run again and skips what it did), each failure (fetch-failed/), and
  each host it stops on: a refusal, a bot check or repeated failures stop that host, and the task
  goes on to its next host (fetch-stopped/<work list id>/).

Nothing here holds a key: the job runs as its own service account.
"""

import hashlib
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from ingestion import corpus_store, polite, rwanda

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # an old Word document (and other Office files)


class NotTaken(Exception):
    """An answer that is not what the item should be (an HTML page for a PDF, a damaged file)."""


def stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def share_by_host(items: list[dict], task: int, tasks: int) -> list[dict]:
    """This task's items: every item of the hosts given to it. Hosts are dealt out busiest first,
    each to the task with the fewest items so far, so no host is ever fetched by two tasks."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item["host"]] = counts.get(item["host"], 0) + 1
    load, owner = [0] * tasks, {}
    for host in sorted(counts, key=lambda h: (-counts[h], h)):
        chosen = min(range(tasks), key=lambda t: (load[t], t))
        owner[host] = chosen
        load[chosen] += counts[host]
    return [item for item in items if owner[item["host"]] == task]


def sniff(data: bytes, expect: str) -> tuple[str, str]:
    """What a fetched answer is, by its bytes, judged against what the item should be: a document
    (PDF or Word, by rwanda.file_kind; an old .doc), a web page, or the court's JSON record."""
    kind = rwanda.file_kind(data)
    start = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    page = start.startswith((b"<!doctype html", b"<html")) or b"<html" in start or b"<body" in start
    if expect == "json":
        try:
            if isinstance(json.loads(data), dict):
                return ".api.json", "application/json"
        except ValueError:
            pass
        raise NotTaken("not the court's JSON record")
    if expect == "html":
        if page:
            return ".html", "text/html"
        if kind:
            return kind
        raise NotTaken("not a web page")
    if kind:
        return kind
    if data.startswith(OLE):
        return ".doc", "application/msword"
    if page:
        raise NotTaken("a web page instead of the document (an error, a notice or a viewer page)")
    raise NotTaken("not a PDF or a Word document")


class Fetcher:
    """One task's share of a work list, fetched host by host."""

    def __init__(self, store, worklist: dict, client=None, log: Callable[[str], None] = print, task: int = 0):
        self.store, self.worklist, self.log, self.task = store, worklist, log, task
        self.country = worklist["country"]
        self.prefix = f"work/{self.country}/"
        self.wid = worklist["id"]
        hosts, windows = set(), {}
        for meta in worklist["sources"].values():
            hosts |= set(meta["file_hosts"])
            windows |= {h: tuple(w) for h, w in (meta.get("windows") or {}).items()}
        self.client = client or polite.PoliteClient(hosts, delay=worklist["delay"], windows=windows, log=log)
        self.done = {n.rsplit("/", 1)[-1] for n in store.list(f"{self.prefix}fetch-done/{self.wid}/")}
        self.held: dict[str, str] = {}  # sha256 -> the stored file's name
        for name in store.list(f"sources/{self.country}/"):
            leaf = name.rsplit("/", 1)[-1]
            sidecar = leaf.endswith(".json") and not leaf.endswith(".api.json")
            if ".occurrences/" not in name and not sidecar:
                self.held.setdefault(leaf.split(".", 1)[0], name)

    def run(self, items: list[dict]) -> dict:
        totals = {
            "items": len(items),
            "skipped": 0,
            "stored": 0,
            "already_stored": 0,
            "unchanged": 0,
            "failed": 0,
        }
        stopped = []
        by_host: dict[str, list[dict]] = {}
        for item in items:
            by_host.setdefault(item["host"], []).append(item)
        for host, queue in by_host.items():
            self.log(f"task {self.task + 1}: {host}: {len(queue)} items")
            failures = misses = 0
            for n, item in enumerate(queue):
                if item["key"] in self.done:
                    totals["skipped"] += 1
                    continue
                try:
                    outcome = self.fetch(item)
                    totals[outcome] += 1
                    failures = misses = 0
                except polite.Refused as error:  # a refusal or a bot check: this host is left alone
                    self.failed(item, f"refused: {error}")
                    totals["failed"] += 1
                    stopped.append(self.stop(host, f"refused or bot check: {error}", queue[n:]))
                    break
                except polite.Unreachable as error:
                    self.failed(item, f"no answer: {error}")
                    totals["failed"] += 1
                    failures += 1
                    if failures >= 3:
                        stopped.append(
                            self.stop(host, f"no answer {failures} times in a row: {error}", queue[n:])
                        )
                        break
                except (polite.Disallowed, polite.OffHost, polite.TooLarge, NotTaken, HttpFailure) as error:
                    self.failed(item, str(error))
                    totals["failed"] += 1
                    misses += 1
                    if misses >= 10:
                        stopped.append(self.stop(host, f"10 failures in a row; the last: {error}", queue[n:]))
                        break
                if (n + 1) % 50 == 0:
                    self.log(f"task {self.task + 1}: {host}: {n + 1} of {len(queue)} ({json.dumps(totals)})")
        return totals | {"hosts_stopped": stopped}

    def fetch(self, item: dict) -> str:
        known = item.get("known") or {}
        headers = {}
        if known.get("etag"):
            headers["If-None-Match"] = known["etag"]
        if known.get("last_modified"):
            headers["If-Modified-Since"] = known["last_modified"]
        hosts = self.worklist["sources"][item["source"]]["file_hosts"]
        answer = self.client.get(item["url"], limit=item["size_limit"], hosts=hosts, headers=headers or None)
        validators = {
            "etag": answer.headers.get("etag"),
            "last_modified": answer.headers.get("last-modified"),
            "content_length": answer.headers.get("content-length"),
        }
        validators = {k: v for k, v in validators.items() if v}
        base = {
            "item": item["item"],
            "key": item["key"],
            "source": item["source"],
            "url": item["url"],
            "final_url": answer.final_url,
            "redirects": answer.redirects,
            "retrieved_at": stamp(),
            "status": answer.status,
            "content_type": answer.headers.get("content-type"),
            "validators": validators,
            "worklist": self.wid,
        }
        if answer.status == 304 and known.get("sha256"):
            kept = {k: v for k, v in known.items() if k in ("etag", "last_modified")}
            return self.record(
                base
                | {
                    "outcome": "unchanged",
                    "how": "304 Not Modified",
                    "sha256": known["sha256"],
                    "bytes": None,
                    "stored": known["stored"],
                    "suffix": "." + known["stored"].rsplit("/", 1)[-1].split(".", 1)[1],
                    "validators": kept | validators,
                }
            )
        if answer.status != 200:
            raise HttpFailure(f"HTTP {answer.status}")
        suffix, media_type = sniff(answer.content, item["expect"])
        sha = hashlib.sha256(answer.content).hexdigest()
        base |= {"sha256": sha, "bytes": len(answer.content), "suffix": suffix}
        if known.get("sha256") == sha:
            return self.record(
                base | {"outcome": "unchanged", "how": "same fingerprint", "stored": known["stored"]}
            )
        sidecar = {
            **{
                k: base[k]
                for k in ("item", "source", "url", "final_url", "redirects", "retrieved_at", "status")
            },
            "content_type": base["content_type"],
            "content_length": validators.get("content_length"),
            "etag": validators.get("etag"),
            "last_modified": validators.get("last_modified"),
            "bytes": len(answer.content),
            "sha256": sha,
            "type": item.get("type"),
            "listed": item.get("listed"),
            "record_url": item.get("record_url"),
            "listing_url": item.get("listing_url"),
            "alternate_urls": item.get("alternate_urls"),
            "approval": self.worklist.get("approval"),
            "worklist": self.wid,
            "fetched_by": {
                "job": "corpus-fetcher",
                "execution": os.environ.get("CLOUD_RUN_EXECUTION"),
                "task": self.task,
            },
            "other_sources": [],
        }
        held = self.held.get(sha)
        if held:  # the same bytes already stored from another listing: one file, another occurrence
            return self.occurrence(held, sidecar, base)
        name = corpus_store.source_name(self.country, item["source"], sha, suffix)
        self.store.put(name, answer.content, media_type, {"sha256": sha, "url": item["url"]})
        self.store.put(
            name[: -len(suffix)] + ".json",
            (json.dumps(sidecar, ensure_ascii=False, indent=1) + "\n").encode(),
            "application/json",
        )
        self.held[sha] = name
        self.log(f"stored {item['item'][:90]} ({len(answer.content) // 1024} KB)")
        return self.record(base | {"outcome": "stored", "stored": self.store.uri(name)})

    def occurrence(self, held: str, sidecar: dict, base: dict) -> str:
        stem = held.rsplit(".", 2)[0] if held.endswith(".api.json") else held.rsplit(".", 1)[0]
        occurrence = {k: v for k, v in sidecar.items() if k != "other_sources"}
        self.store.put(
            f"{stem}.occurrences/{base['key']}.json",
            (json.dumps(occurrence, ensure_ascii=False, indent=1) + "\n").encode(),
            "application/json",
            replace=True,
        )
        same_as = None
        try:  # the file's own sidecar lists its other occurrences too (the occurrence objects decide)
            first = json.loads(self.store.get(f"{stem}.json"))
            same_as = first.get("item")
            others = first.setdefault("other_sources", [])
            if all(o.get("item") != base["item"] for o in others):
                others.append({k: sidecar[k] for k in ("item", "source", "url", "retrieved_at")})
                self.store.put(
                    f"{stem}.json",
                    (json.dumps(first, ensure_ascii=False, indent=1) + "\n").encode(),
                    "application/json",
                    replace=True,
                )
        except Exception as error:  # noqa: BLE001 - the occurrence object above is the record
            self.log(f"sidecar of {held} not updated: {type(error).__name__}: {error}")
        self.log(f"already stored as {held}: {base['item'][:80]}")
        return self.record(
            base | {"outcome": "already_stored", "stored": self.store.uri(held), "same_file_as": same_as}
        )

    def record(self, fetched: dict) -> str:
        body = (json.dumps(fetched, ensure_ascii=False, indent=1) + "\n").encode()
        self.store.put(f"{self.prefix}fetched/{fetched['key']}.json", body, "application/json", replace=True)
        self.store.put(
            f"{self.prefix}fetch-done/{self.wid}/{fetched['key']}", b"", "text/plain", replace=True
        )
        self.done.add(fetched["key"])
        return fetched["outcome"]

    def failed(self, item: dict, reason: str) -> None:
        self.log(f"failed {item['item'][:90]}: {reason[:200]}")
        failure = {
            "item": item["item"],
            "url": item["url"],
            "at": stamp(),
            "reason": reason[:500],
            "worklist": self.wid,
        }
        self.store.put(
            f"{self.prefix}fetch-failed/{item['key']}.json",
            json.dumps(failure, ensure_ascii=False).encode(),
            "application/json",
            replace=True,
        )

    def stop(self, host: str, reason: str, left: list[dict]) -> dict:
        stop = {
            "host": host,
            "at": stamp(),
            "reason": reason[:500],
            "sources": sorted({i["source"] for i in left}),
            "items_left": len(left),
            "task": self.task,
            "worklist": self.wid,
        }
        self.log(f"stopped {host}: {reason[:200]} ({len(left)} items left)")
        self.store.put(
            f"{self.prefix}fetch-stopped/{self.wid}/{host}.json",
            json.dumps(stop, ensure_ascii=False).encode(),
            "application/json",
            replace=True,
        )
        return stop


class HttpFailure(Exception):
    """The site answered, but not with the file (a 404, a 500 after a second try)."""


def main() -> int:
    country = os.environ.get("FETCH_COUNTRY", "ng")
    task = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    tasks = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    store = corpus_store.open_store()
    worklist = json.loads(store.get(f"work/{country}/fetch.json"))
    approval = worklist.get("approval") or {}
    if not (approval.get("approved_by") and approval.get("basis")):
        raise SystemExit("the work list has no recorded approval")
    if float(worklist.get("delay") or 0) < polite.LEAST_DELAY:
        raise SystemExit(f"the work list's delay is under {polite.LEAST_DELAY} seconds")
    mine = share_by_host(worklist["items"], task, tasks)
    hosts = sorted({i["host"] for i in mine})
    print(
        f"task {task + 1} of {tasks}: {len(mine)} of {worklist['items_total']} items, hosts {hosts}",
        flush=True,
    )
    fetcher = Fetcher(store, worklist, log=lambda m: print(m, flush=True), task=task)
    try:
        result = fetcher.run(mine)
    finally:
        fetcher.client.close()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
