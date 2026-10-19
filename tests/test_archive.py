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


def test_an_azure_container_under_an_immutability_policy_is_only_ever_added_to(tmp_path, monkeypatch, capsys):
    """In a store every write creates an object: the originals once each, and each copy's manifest
    lines as a manifest of its own."""
    from blob_support import BlobService

    from ingestion import corpus_store

    service = BlobService()
    service.immutable.add("archive")
    opened = []

    def open_store(address):
        opened.append(address)
        return service.store("archive")

    monkeypatch.setattr(corpus_store, "open_store", open_store)
    sources = tmp_path / "sources"
    sources.mkdir()
    first = original(sources, "act-1.pdf", b"%PDF-1.7 one")
    second = original(sources, "act-2.pdf", b"%PDF-1.7 two")
    place = "az://oatest/archive/originals"
    assert archive.copy(sources, place, dry_run=True) == {
        "added": 2,
        "already_archived": 0,
        "tier": "locked",
        "dry_run": True,
    }
    assert not service.blobs and opened == ["az://oatest/archive"]
    assert archive.main(["copy", str(sources), place]) == 0  # an address, not a folder
    assert json.loads(capsys.readouterr().out)["added"] == 2
    puts = [r for r in service.requests if r.method == "PUT"]
    assert len(puts) == 3 and all(r.headers["If-None-Match"] == "*" for r in puts)
    assert service.blobs[("archive", "originals/" + archive.address(first))]["data"] == b"%PDF-1.7 one"
    assert service.blobs[("archive", "originals/" + archive.address(first))]["metadata"] == {"sha256": first}
    [manifest] = [name for _, name in service.blobs if name.startswith("originals/manifests/")]
    lines = [json.loads(line) for line in service.blobs[("archive", manifest)]["data"].splitlines()]
    assert {line["sha256"] for line in lines} == {first, second} and lines[0][
        "media_type"
    ] == "application/pdf"
    assert archive.copy(sources, place) == {
        "added": 0,
        "already_archived": 2,
        "tier": "locked",
        "dry_run": False,
    }
    assert len([r for r in service.requests if r.method == "PUT"]) == 3  # nothing more written
    assert archive.verify(place) == {"checked": 2, "tampered": [], "missing": [], "ok": True}
    service.rewrite("archive", "originals/" + archive.address(first), b"changed")
    del service.blobs[("archive", "originals/" + archive.address(second))]
    assert archive.main(["verify", place]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["tampered"] == [first] and report["missing"] == [second]
