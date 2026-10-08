"""Backend-independent SYS-07 journal drills; every journal implementation must pass these."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from operations.journal import Event, JournalConflict, JournalError


def deny(revision=1, affected=("scp_synthetic",), event_id=None, kind="publication_deny"):
    return Event(
        event_id=event_id or uuid4(),
        kind=kind,
        affected=affected,
        revision=revision,
        revision_domain="publication",
        occurred_at=datetime(2026, 10, 7, tzinfo=UTC),
    )


def check_sequence_is_assigned_gapless_and_ordered(journal):
    events = [deny(revision=r) for r in range(1, 6)]
    receipts = [journal.append(e) for e in events]
    assert [r.sequence for r in receipts] == [1, 2, 3, 4, 5]
    assert [e for _, e in journal.read()] == events
    assert journal.high_watermark() == (5, receipts[-1].chain_sha256)
    assert [r.sequence for r, _ in journal.read(after=3, limit=10)] == [4, 5]


def check_retry_is_idempotent_and_mismatch_rejected(journal):
    event = deny()
    first = journal.append(event)
    assert journal.append(event) == first
    assert journal.receipt(event.event_id) == first
    with pytest.raises(JournalConflict):
        journal.append(deny(revision=2, event_id=event.event_id))
    assert journal.high_watermark()[0] == 1


def check_events_reject_non_opaque_or_invalid_fields():
    for bad in [("contains spaces",), ("",), tuple(f"id{i}" for i in range(1001))]:
        with pytest.raises(JournalError):
            deny(affected=bad)
    with pytest.raises(JournalError):
        deny(kind="publish")
    with pytest.raises(JournalError):
        deny(revision=-1)


CHECKS = [
    check_sequence_is_assigned_gapless_and_ordered,
    check_retry_is_idempotent_and_mismatch_rejected,
]
