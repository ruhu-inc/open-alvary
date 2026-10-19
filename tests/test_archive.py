"""CPR-05: originals are archived by their SHA-256 with a manifest line each, never rewritten; a copy
whose bytes changed is refused; verification reports a tampered object and a missing one."""

import hashlib
import json

import pytest

from operations import archive


def original(folder, name, data, url="https://www.minijust.gov.rw/official-gazette"):
    (folder / name).write_bytes(data)
    record = {
        "url": url,
        "retrieved_at": "2026-10-09T06:41:17+00:00",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (folder / (name.rsplit(".", 1)[0] + ".provenance.json")).write_text(json.dumps(record))
    return record["sha256"]


def test_originals_are_archived_once_by_hash_and_verified(tmp_path):
    sources, root = tmp_path / "sources", tmp_path / "archive"
    sources.mkdir()
    first = original(sources, "act-1.pdf", b"%PDF-1.7 one")
    original(sources, "act-2.pdf", b"%PDF-1.7 two")
    assert archive.copy(sources, root, dry_run=True) == {
        "added": 2,
        "already_archived": 0,
        "tier": "locked",
        "dry_run": True,
    }
    assert not root.exists()
    assert archive.copy(sources, root)["added"] == 2
    assert (root / archive.address(first)).read_bytes() == b"%PDF-1.7 one"
    lines = [json.loads(line) for line in (root / archive.MANIFEST).read_text().splitlines()]
    assert {line["sha256"] for line in lines} >= {first} and lines[0]["media_type"] == "application/pdf"
    assert archive.copy(sources, root) == {
        "added": 0,
        "already_archived": 2,
        "tier": "locked",
        "dry_run": False,
    }
    assert archive.verify(root)["ok"]
    (root / archive.address(first)).write_bytes(b"changed")
    report = archive.verify(root)
    assert report["tampered"] == [first] and not report["ok"]


def test_an_original_that_no_longer_matches_its_record_is_refused(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    original(sources, "act-1.pdf", b"%PDF-1.7 one")
    (sources / "act-1.pdf").write_bytes(b"%PDF-1.7 altered")
    with pytest.raises(ValueError, match="no longer matches"):
        archive.copy(sources, tmp_path / "archive")
