"""Reference file journal: conformance drills plus crash, tamper and concurrency cases."""

import json
import multiprocessing
from uuid import uuid4

import pytest
from journal_conformance import CHECKS, check_events_reject_non_opaque_or_invalid_fields, deny

from operations.journal import FileJournal, JournalCorrupt


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_file_journal_conformance(tmp_path, check):
    check(FileJournal(tmp_path))


def test_event_validation():
    check_events_reject_non_opaque_or_invalid_fields()


def test_history_survives_reopen_and_tampering_is_detected(tmp_path):
    journal = FileJournal(tmp_path)
    receipts = [journal.append(deny(revision=r)) for r in range(1, 4)]
    assert FileJournal(tmp_path).high_watermark() == (3, receipts[-1].chain_sha256)
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    record = json.loads(lines[1])
    record["event"]["revision"] = 99
    lines[1] = json.dumps(record)
    (tmp_path / "journal.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(JournalCorrupt):
        journal.high_watermark()
    with pytest.raises(JournalCorrupt):
        journal.append(deny())


def test_removed_record_breaks_the_chain(tmp_path):
    journal = FileJournal(tmp_path)
    for revision in range(1, 4):
        journal.append(deny(revision=revision))
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    (tmp_path / "journal.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(JournalCorrupt):
        journal.read()


def test_torn_tail_fails_closed_instead_of_appending(tmp_path):
    journal = FileJournal(tmp_path)
    journal.append(deny())
    with (tmp_path / "journal.jsonl").open("ab") as writer:
        writer.write(b'{"sequence":2,"ev')
    with pytest.raises(JournalCorrupt):
        journal.append(deny())


def _append_many(root, count):
    journal = FileJournal(root)
    return [journal.append(deny()).sequence for _ in range(count)]


def test_concurrent_writers_receive_unique_contiguous_sequences(tmp_path):
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        results = pool.starmap(_append_many, [(tmp_path, 10)] * 4)
    sequences = sorted(s for batch in results for s in batch)
    assert sequences == list(range(1, 41))
    assert FileJournal(tmp_path).high_watermark()[0] == 40


def test_receipt_lookup_for_unknown_event_is_none(tmp_path):
    assert FileJournal(tmp_path).receipt(uuid4()) is None
