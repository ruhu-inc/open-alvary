"""Validate release integrity and current eligibility before serving any artifact."""

import json
from hashlib import sha256
from pathlib import Path

from rights.policy import public_corpus
from schema.models import Corpus


def release_available(directory: Path, current: Corpus) -> bool:
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
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
            if sha256(path.read_bytes()).hexdigest() != details["sha256"]:
                return False
        released = Corpus.model_validate(
            {
                key: [json.loads(line) for line in (directory / f"{key}.jsonl").read_text().splitlines()]
                for key in Corpus.model_fields
            }
        )
        public = public_corpus(current)
        ids = {s.id for s in public.sources}
        versions = {(v.id, v.content_hash) for v in public.versions}
        if any(s.id not in ids for s in released.sources):
            return False
        if any((v.id, v.content_hash) not in versions for v in released.versions):
            return False
        # Review corrections invalidate bundles containing stale rights assertions.
        rights = {r.source_id: r for r in public.rights}
        if any(r != rights.get(r.source_id) for r in released.rights):
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
