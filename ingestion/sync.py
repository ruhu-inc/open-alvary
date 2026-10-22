"""The sync ledger: when each source was last checked, what changed, and what is due.

One ledger per collection (private staging), updated by every check, beside a run log per check.
Per source it keeps the last check and its outcome, the last change, the last fetch, the cursor
(the newest item seen), counts and when the next check is due. Per item it keeps when it was first
and last seen, what the listing showed, the fingerprint of what we fetched, and a history of
changes (new, changed, moved, no longer listed), each dated and tied to the run that saw it.

A check that fails or stops early is recorded as a failure, never as "nothing new": items it did
not reach are not marked as no longer listed. A file disappearing from a listing is recorded as
such, never as the repeal of a law.
"""

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

# An item's provenance beside what its listing shows: its own record or detail page, the listing
# request (with its query or page) it came from, the source's record verbatim, and the addresses of
# other renditions of the same document (not fetched).
PROVENANCE = ("record_url", "listing_url", "listed_raw", "alternate_urls")

CADENCE = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"sources": {}, "items": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def save_items(self, item_ids) -> None:
        """Write back only these items, merged into the ledger as it now stands on disk, under a lock:
        two fetches (one per site) can then share the ledger without undoing each other's records."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = (
                    json.loads(self.path.read_text()) if self.path.exists() else {"sources": {}, "items": {}}
                )
                for item_id in item_ids:
                    if item_id in self.data["items"]:
                        current["items"][item_id] = self.data["items"][item_id]
                for item_id, record in current["items"].items():  # and see what the other wrote
                    if item_id not in item_ids:
                        self.data["items"][item_id] = record
                tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                tmp.replace(self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save_source(self, source_id: str) -> None:
        """Write back one source's state and items, merged into the ledger as it now stands on
        disk, under the same lock: catalogues of two sources run at once keep each other's records."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = (
                    json.loads(self.path.read_text()) if self.path.exists() else {"sources": {}, "items": {}}
                )
                current["items"] = {
                    k: v for k, v in current["items"].items() if v.get("source") != source_id
                } | {k: v for k, v in self.data["items"].items() if v.get("source") == source_id}
                if source_id in self.data["sources"]:
                    current["sources"][source_id] = self.data["sources"][source_id]
                tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                tmp.replace(self.path)
                self.data = current
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def source(self, source_id: str) -> dict:
        return self.data["sources"].get(source_id, {})

    def due(self, source_id: str, cadence: str, at: datetime | None = None) -> bool:
        state = self.source(source_id)
        if not state.get("next_due"):
            return True
        return (at or now()) >= datetime.fromisoformat(state["next_due"].replace("Z", "+00:00"))

    def observe_listing(
        self,
        source_id: str,
        url: str,
        cadence: str,
        crawl: dict,
        run_id: str,
        key=lambda item: item["url"],
        cursor: dict | None = None,
        at: datetime | None = None,
        shown=None,
    ) -> dict:
        """Record one check of a listing; returns what changed. `shown` gives what the listing shows
        of an item (by default a file browser's name, size, date and folder). Beside it are kept, where
        the listing gives them, the item's `type` and its provenance (PROVENANCE): its own record
        page, the listing request it came from, the source's record verbatim, other renditions."""
        moment = at or now()
        when = stamp(moment)
        items = self.data["items"]
        seen = {}
        added, changed, moved = [], [], []
        for item in crawl["files"]:
            item_id = f"{source_id}:{key(item)}"
            seen[item_id] = item
            listed = (
                shown(item) if shown else {k: item.get(k) for k in ("name", "size", "modified", "folder")}
            )
            kind = {k: item[k] for k in ("type", *PROVENANCE) if item.get(k) not in (None, "", [])}
            record = items.get(item_id)
            if record is None:
                items[item_id] = {
                    "source": source_id,
                    "url": item["url"],
                    **kind,
                    "first_seen": when,
                    "last_seen": when,
                    "listed": listed,
                    "history": [{"at": when, "run": run_id, "change": "new"}],
                }
                added.append(item_id)
                continue
            record["last_seen"] = when
            before = {k: record.get(k) for k in ("type", "alternate_urls")}
            record.update(kind)
            if record.get("gone"):
                record.pop("gone")
                record["history"].append({"at": when, "run": run_id, "change": "listed again"})
            if shown:  # a listing with details of its own: any change to them is recorded
                for k in PROVENANCE:
                    if k not in kind:
                        record.pop(k, None)
                now_ = {k: record.get(k) for k in ("type", "alternate_urls")}
                if record["listed"] != listed or before != now_:
                    record["history"].append(
                        {
                            "at": when,
                            "run": run_id,
                            "change": "listing changed",
                            "before": record["listed"],
                            **({"before_also": before} if before != now_ else {}),
                        }
                    )
                    changed.append(item_id)
                record["listed"] = listed
                continue
            if record["listed"].get("folder") != listed["folder"]:
                record["history"].append(
                    {
                        "at": when,
                        "run": run_id,
                        "change": "moved",
                        "from": record["listed"].get("folder"),
                        "to": listed["folder"],
                    }
                )
                moved.append(item_id)
            elif any(record["listed"].get(k) != listed[k] for k in ("name", "size", "modified")):
                record["history"].append(
                    {"at": when, "run": run_id, "change": "listing changed", "before": record["listed"]}
                )
                changed.append(item_id)
            record["listed"] = listed
        gone = []
        if crawl["complete"]:  # only a complete check can say an item is no longer listed
            for item_id, record in items.items():
                if record["source"] == source_id and item_id not in seen and not record.get("gone"):
                    record["gone"] = when
                    record["history"].append({"at": when, "run": run_id, "change": "no longer listed"})
                    gone.append(item_id)
        state = self.data["sources"].setdefault(source_id, {"source": source_id})
        state.update(
            {
                "url": url,
                "cadence": cadence,
                "last_checked_at": when,
                "last_check_outcome": crawl["outcome"],
                "last_check_complete": crawl["complete"],
                "next_due": stamp(moment + CADENCE[cadence]),
            }
        )
        if added or changed or moved or gone:
            state["last_changed_at"] = when
        if cursor:
            state["cursor"] = cursor
        state["counts"] = {
            "listed": len(seen),
            "added_last_run": len(added),
            "changed_last_run": len(changed),
            "moved_last_run": len(moved),
            "no_longer_listed_last_run": len(gone),
            "pages_read_last_run": crawl["pages_read"],
        }
        return {"added": added, "changed": changed, "moved": moved, "gone": gone}

    def fetched(self, item_id: str, sha256: str, size: int, at: datetime | None = None) -> None:
        """Record that an item's file was fetched, with the fingerprint of what we got."""
        when = stamp(at or now())
        record = self.data["items"][item_id]
        if record.get("sha256") and record["sha256"] != sha256:
            record["history"].append({"at": when, "change": "content changed", "before": record["sha256"]})
        record.update({"sha256": sha256, "bytes": size, "last_fetched": when})
        state = self.data["sources"][record["source"]]
        state["last_fetched_at"] = when
