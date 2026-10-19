"""Where a client keeps Open Alvary evidence for its own users (RSV-09).

A cache holds evidence units keyed by (anchor_id, content_hash), evidence packs keyed by (normalised
request, dataset revision, profiles), the application's own answers with the units they cite, the
change feed position and the versions whose withdrawal the client has seen. It holds only what one
client fetched under its own key, for its own users: it is never a mirror index of the corpus.

A store implements four operations on JSON records grouped by kind (get, put, delete, scan). The
rules are written once on top of them, so another store (a database, Redis) needs only those four.
A newer pack for the same request replaces the older one, and a unit nothing cites any more goes.
"""

import json
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

KINDS = ("unit", "pack", "answer", "state", "withdrawn")


def unit_key(anchor_id: str, content_hash: str) -> str:
    """A unit's key: its anchor and the hash of the text it is in (IDs never contain "|")."""
    return f"{anchor_id}|{content_hash}"


def _cites(units: list[dict]) -> list[list[str]]:
    """What an entry cites, as [unit key, version, work] for each unit, so a purge finds it."""
    return [[unit_key(u["anchor_id"], u["content_hash"]), u["version_id"], u["source_id"]] for u in units]


class Cache(ABC):
    @abstractmethod
    def get(self, kind: str, key: str) -> dict | None:
        """The record stored under (kind, key), or None."""

    @abstractmethod
    def put(self, kind: str, key: str, record: dict) -> None:
        """Store a record, replacing any under the same (kind, key)."""

    @abstractmethod
    def delete(self, kind: str, key: str) -> None:
        """Remove a record, if there is one."""

    @abstractmethod
    def scan(self, kind: str) -> list[tuple[str, dict]]:
        """Every (key, record) of a kind."""

    @contextmanager
    def transaction(self):
        """Writes inside happen together or not at all, where the store can."""
        yield

    # --- the change feed ---------------------------------------------------------------------------

    def feed(self) -> dict:
        """The saved feed position: ``resume_token``, ``epoch`` and ``read_at`` (the last successful read)."""
        return self.get("state", "feed") or {}

    def save_feed(self, **values) -> None:
        self.put("state", "feed", {**self.feed(), **values})

    def profiles(self) -> str | None:
        """The profiles of the latest pack Open Alvary sent: a pack of other profiles is not reused."""
        return (self.get("state", "profiles") or {}).get("value")

    def withdrawn(self, version_id: str) -> bool:
        return self.get("withdrawn", version_id) is not None

    # --- packs and units ---------------------------------------------------------------------------

    def store_pack(self, request: str, pack: dict, etag: str | None, now: datetime) -> None:
        """A pack fresh from Open Alvary and its units, which replace what was cached for them. Its
        revisions say which dataset and profiles it belongs to."""
        revisions = {k: v for k, v in pack["revisions"].items() if v is not None}
        dataset = revisions.pop("dataset")
        profiles = ";".join(f"{name}={value}" for name, value in sorted(revisions.items()))
        at = now.isoformat()
        with self.transaction():
            before = self.get("pack", request)
            for unit in pack["evidence"]:
                record = {
                    "anchor_id": unit["anchor_id"],
                    "content_hash": unit["content_hash"],
                    "version_id": unit["version_id"],
                    "source_id": unit["source_id"],
                    "expression_id": unit["expression_id"],
                    "unit": unit,
                    "currentness": unit["currentness"],
                    "flags": [],
                    "cached_at": at,
                    "checked_at": at,
                }
                self.put("unit", unit_key(unit["anchor_id"], unit["content_hash"]), record)
                self.delete("withdrawn", unit["version_id"])  # Open Alvary has just served it: public again
            entry = {"request": request, "dataset": dataset, "profiles": profiles, "etag": etag}
            self.put(
                "pack", request, entry | {"pack": pack, "cites": _cites(pack["evidence"]), "cached_at": at}
            )
            self.put("state", "profiles", {"value": profiles})
            if before is not None:
                self._drop_orphans([c[0] for c in before["cites"]])

    def pack(self, request: str) -> dict | None:
        return self.get("pack", request)

    def units_of(self, entry: dict) -> list[dict | None]:
        """The cached unit records an entry cites, in its order; None for any no longer cached."""
        return [self.get("unit", c[0]) for c in entry["cites"]]

    def units_with_anchor(self, anchor_ids: set[str]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for key, record in self.scan("unit"):
            if record["anchor_id"] in anchor_ids:
                found.setdefault(record["anchor_id"], []).append(key)
        return found

    def mark(self, flag: str, *, expression: str | None = None, source: str | None = None) -> int:
        """Flag cached units as needing a check before reuse: those of one expression, of one work, or
        every one. Returns how many were not flagged so before."""
        count = 0
        with self.transaction():
            for key, record in self.scan("unit"):
                if expression is not None and record["expression_id"] != expression:
                    continue
                if source is not None and record["source_id"] != source:
                    continue
                if flag not in record["flags"]:
                    self.put("unit", key, record | {"flags": [*record["flags"], flag]})
                    count += 1
        return count

    def confirm(self, key: str, currentness: dict | None, now: datetime) -> None:
        """Open Alvary has just confirmed a unit: its flags go and its currentness is the one it gave."""
        record = self.get("unit", key)
        if record is not None:
            fresh = currentness or record["currentness"]
            self.put("unit", key, record | {"flags": [], "currentness": fresh, "checked_at": now.isoformat()})

    def purge(
        self, *, version: str | None = None, source: str | None = None, unit: str | None = None
    ) -> None:
        """Remove at once every cached unit, pack and answer citing a version, a work or one unit."""

        def hit(cited: list[str]) -> bool:
            return cited[0] == unit or cited[1] == version or cited[2] == source

        with self.transaction():
            dropped: list[str] = []
            for kind in ("pack", "answer"):
                for key, record in self.scan(kind):
                    if any(hit(c) for c in record["cites"]):
                        self.delete(kind, key)
                        dropped += [c[0] for c in record["cites"]]
            for key, record in self.scan("unit"):
                if hit([key, record["version_id"], record["source_id"]]):
                    self.delete("unit", key)
            self._drop_orphans(dropped)

    def withdraw(self, version_id: str, now: datetime) -> None:
        """A withdrawal seen in the feed: everything citing the version goes, and the version is
        remembered so nothing citing it is ever served from the cache again."""
        with self.transaction():
            self.purge(version=version_id)
            self.put("withdrawn", version_id, {"at": now.isoformat()})

    def _drop_orphans(self, keys: list[str]) -> None:
        used = {c[0] for kind in ("pack", "answer") for _, record in self.scan(kind) for c in record["cites"]}
        for key in set(keys) - used:
            self.delete("unit", key)

    # --- the application's own answers -------------------------------------------------------------

    def put_answer(self, key: str, body, units: list[dict], now: datetime) -> None:
        record = {"body": body, "units": units, "cites": _cites(units), "saved_at": now.isoformat()}
        self.put("answer", key, record)

    def answer(self, key: str) -> dict | None:
        return self.get("answer", key)


class MemoryCache(Cache):
    """For one process. The feed position goes with it, so a new process starts from the feed's
    present with an empty cache."""

    def __init__(self):
        self._records: dict[str, dict[str, str]] = {kind: {} for kind in KINDS}

    def get(self, kind, key):
        stored = self._records[kind].get(key)
        return None if stored is None else json.loads(stored)

    def put(self, kind, key, record):
        self._records[kind][key] = json.dumps(record)

    def delete(self, kind, key):
        self._records[kind].pop(key, None)

    def scan(self, kind):
        return [(key, json.loads(stored)) for key, stored in sorted(self._records[kind].items())]


class SQLiteCache(Cache):
    """A cache in one SQLite file, kept across restarts with the feed position, so a restarted client
    continues the feed from where it stopped."""

    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS entries (kind TEXT NOT NULL, key TEXT NOT NULL, record TEXT NOT NULL,"
            " PRIMARY KEY (kind, key))"
        )
        self._db.commit()
        self._depth = 0

    def get(self, kind, key):
        row = self._db.execute(
            "SELECT record FROM entries WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, kind, key, record):
        self._db.execute(
            "INSERT INTO entries (kind, key, record) VALUES (?, ?, ?)"
            " ON CONFLICT (kind, key) DO UPDATE SET record = excluded.record",
            (kind, key, json.dumps(record)),
        )
        self._commit()

    def delete(self, kind, key):
        self._db.execute("DELETE FROM entries WHERE kind = ? AND key = ?", (kind, key))
        self._commit()

    def scan(self, kind):
        rows = self._db.execute("SELECT key, record FROM entries WHERE kind = ? ORDER BY key", (kind,))
        return [(key, json.loads(record)) for key, record in rows.fetchall()]

    @contextmanager
    def transaction(self):
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if not self._depth:
                self._db.rollback()
            raise
        self._depth -= 1
        self._commit()

    def _commit(self):
        if not self._depth:
            self._db.commit()

    def close(self) -> None:
        self._db.close()
