"""Independent, ordered denial journal contract (SYS-07) and a reference file backend.

The journal assigns the global sequence; clients supply a stable ``event_id`` and can never
choose order. Appending the same event again returns the original receipt; the same
``event_id`` with a different payload is rejected. Every record carries a hash chain so
gaps, edits and torn writes are detected rather than silently replayed.

``FileJournal`` is a reference backend for tests and drills. It must live in a failure domain
independent of the database (D7 chooses the real service, its access control and encryption);
any replacement must pass ``tests/journal_conformance.py``. Events carry opaque IDs and
revisions only: never source text, emails or secrets.
"""

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

FORMAT = "open-alvary-journal-1"
KINDS = {"publication_deny", "erasure", "epoch_open", "epoch_close"}
DOMAINS = {"publication", "account"}
OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
GENESIS = "0" * 64


class JournalError(RuntimeError):
    """Base for safe journal failures; messages never contain event payloads."""


class JournalConflict(JournalError):
    pass


class JournalCorrupt(JournalError):
    pass


@dataclass(frozen=True)
class Event:
    event_id: UUID
    kind: str
    affected: tuple[str, ...]
    revision: int
    revision_domain: str
    occurred_at: datetime
    erasure: bool = False

    def __post_init__(self):
        if (
            self.kind not in KINDS
            or self.revision_domain not in DOMAINS
            or not 1 <= len(self.affected) <= 1000
            or not all(OPAQUE_ID.fullmatch(a) for a in self.affected)
            or self.revision < 0
            or self.occurred_at.tzinfo is None
        ):
            raise JournalError("Invalid journal event")

    def canonical(self) -> bytes:
        return json.dumps(self.as_json(), sort_keys=True, separators=(",", ":")).encode()

    def as_json(self) -> dict:
        return {
            "event_id": str(self.event_id),
            "kind": self.kind,
            "affected": list(self.affected),
            "revision": self.revision,
            "revision_domain": self.revision_domain,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "erasure": self.erasure,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Event":
        return cls(
            event_id=UUID(data["event_id"]),
            kind=data["kind"],
            affected=tuple(data["affected"]),
            revision=data["revision"],
            revision_domain=data["revision_domain"],
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            erasure=data["erasure"],
        )


@dataclass(frozen=True)
class Receipt:
    sequence: int
    event_id: UUID
    payload_sha256: str
    chain_sha256: str


def chain(previous: str, sequence: int, payload_sha256: str) -> str:
    return hashlib.sha256(f"{FORMAT}\0{previous}\0{sequence}\0{payload_sha256}".encode()).hexdigest()


class FileJournal:
    """Append-only JSON lines with an exclusive lock, fsync and a verified hash chain."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "journal.jsonl"
        self.lock_path = root / "journal.lock"
        if not root.is_dir():
            raise JournalError("Journal location unavailable")

    def _locked(self, mode):
        handle = self.lock_path.open("a")
        fcntl.flock(handle, mode)
        return handle

    def _records(self):
        """Verify the whole chain; any torn, reordered or edited record fails closed."""
        records, previous = [], GENESIS
        if not self.path.exists():
            return records, previous
        with self.path.open("rb") as reader:
            data = reader.read()
        if data and not data.endswith(b"\n"):
            raise JournalCorrupt("Journal tail is incomplete")
        for number, line in enumerate(data.splitlines(), start=1):
            try:
                record = json.loads(line)
                event = Event.from_json(record["event"])
            except (ValueError, KeyError, TypeError, JournalError):
                raise JournalCorrupt("Journal record unreadable") from None
            payload = hashlib.sha256(event.canonical()).hexdigest()
            if (
                record.get("sequence") != number
                or record.get("payload_sha256") != payload
                or record.get("chain_sha256") != chain(previous, number, payload)
            ):
                raise JournalCorrupt("Journal chain broken")
            previous = record["chain_sha256"]
            records.append((Receipt(number, event.event_id, payload, previous), event))
        return records, previous

    def append(self, event: Event) -> Receipt:
        payload = hashlib.sha256(event.canonical()).hexdigest()
        with self._locked(fcntl.LOCK_EX):
            records, previous = self._records()
            for receipt, _ in records:
                if receipt.event_id == event.event_id:
                    if receipt.payload_sha256 != payload:
                        raise JournalConflict("Event ID reused with a different payload")
                    return receipt
            sequence = len(records) + 1
            receipt = Receipt(sequence, event.event_id, payload, chain(previous, sequence, payload))
            line = json.dumps(
                {
                    "sequence": sequence,
                    "event": event.as_json(),
                    "payload_sha256": payload,
                    "chain_sha256": receipt.chain_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, line.encode() + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return receipt

    def read(self, after: int = 0, limit: int = 1000) -> list[tuple[Receipt, Event]]:
        if after < 0 or not 1 <= limit <= 10000:
            raise JournalError("Invalid journal range")
        with self._locked(fcntl.LOCK_SH):
            records, _ = self._records()
        return records[after : after + limit]

    def receipt(self, event_id: UUID) -> Receipt | None:
        with self._locked(fcntl.LOCK_SH):
            records, _ = self._records()
        return next((r for r, _ in records if r.event_id == event_id), None)

    def high_watermark(self) -> tuple[int, str]:
        """Verified (sequence, chain hash); the chain hash pins history up to that point."""
        with self._locked(fcntl.LOCK_SH):
            records, previous = self._records()
        return len(records), previous
