"""Validate release integrity and current eligibility before serving any artifact.

Asked on every response, so it holds neither the corpus nor a release in memory to answer: a release's
files are hashed and read a row at a time, and only the records a release names are looked up, as they
stand now. Nothing about a record is kept between answers, so a withdrawal takes effect on the next one.
"""

import hashlib
import json
import threading
from collections.abc import Iterator
from itertools import batched
from pathlib import Path

from api.repositories import LegacyRepository
from api.store import Store, check_legacy_authority
from rights.policy import decide, told_rights
from schema.models import Corpus, RightsRecord

BATCH = 500
# Whether a release's bytes form a valid Corpus, by the hash of its manifest, which pins every file that
# was hashed before asking. It says only that: what the rows ask of current records is asked each time.
_validated: dict[str, bool] = {}
_validating = threading.Lock()


def _rows(directory: Path, name: str) -> Iterator[dict]:
    """A release file's rows, one at a time. Only a line feed ends a row; a value may hold other breaks."""
    with (directory / f"{name}.jsonl").open(encoding="utf-8", newline="\n") as lines:
        for line in lines:
            yield json.loads(line)


def _intact(directory: Path, manifest: dict) -> bool:
    required = {f"{key}.jsonl" for key in Corpus.model_fields} | {
        "README.md",
        "coverage.json",
        "rights-summary.json",
    }
    if set(manifest["files"]) != required:
        return False
    for name, details in manifest["files"].items():
        if Path(name).name != name:
            return False
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()):
            return False
        with path.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != details["sha256"]:
                return False
    return True


def _valid(directory: Path, manifest: bytes) -> bool:
    """Whether an intact release's rows validate together. This reads the release whole, so it is done once
    for the same bytes and one release at a time, however many ask at once."""
    key = hashlib.sha256(manifest).hexdigest()
    if key in _validated:
        return _validated[key]
    with _validating:
        if key not in _validated:
            try:
                Corpus.model_validate({name: list(_rows(directory, name)) for name in Corpus.model_fields})
                _validated[key] = True
            except (ValueError, KeyError, TypeError):
                _validated[key] = False
        return _validated[key]


def _still_public(directory: Path, repository: LegacyRepository) -> bool:
    for rows in batched(_rows(directory, "sources"), BATCH):
        public = repository.public_among([row["id"] for row in rows])
        if any(row["id"] not in public for row in rows):
            return False
    # Review corrections invalidate bundles containing stale rights assertions: what a release tells of each
    # rights record, against what would be told of it now. (A release made before privacy reviews stopped
    # being released still has that field; it is not what is compared.)
    for rows in batched(_rows(directory, "rights"), BATCH):
        rights = repository.public_rights([row["source_id"] for row in rows])
        for row in rows:
            current = rights.get(row["source_id"])
            if current is None:
                return False
            released = RightsRecord.model_validate(row).model_dump(mode="json", exclude={"privacy_status"})
            if released != told_rights(RightsRecord.model_validate(current)):
                return False
    claims = ((row["id"], row["content_hash"]) for row in _rows(directory, "versions"))
    for batch in batched(claims, BATCH):
        gates = repository.gates_of_versions([version_id for version_id, _ in batch])
        rights = repository.public_rights({gate.source_id for gate in gates.values()})
        for version_id, content_hash in batch:
            gate = gates.get(version_id)
            if gate is None or gate.content_hash != content_hash or gate.source_id not in rights:
                return False
            if not decide(RightsRecord.model_validate(rights[gate.source_id]), gate).full_text:
                return False
    return True


def release_available(directory: Path, current: Store) -> bool:
    """Whether a release may be served now. A database that cannot answer raises: that is no withdrawal."""
    try:
        written = (directory / "manifest.json").read_bytes()
        if not _intact(directory, json.loads(written)) or not _valid(directory, written):
            return False
        with current.engine.begin() as connection:
            check_legacy_authority(connection, write=False)
            return _still_public(directory, LegacyRepository(connection))
    except (OSError, ValueError, KeyError, TypeError):
        return False
