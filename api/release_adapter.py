"""Filesystem release compatibility adapter; global pilot verifier remains explicit.

Its existing whole-corpus integrity contract is retained until the M4 release slice.
Regular catalogue repositories never use this adapter or load a whole Corpus.
"""

import json
import re
from pathlib import Path

from api.store import Store
from exports.identity import RELEASE_ID, validate_release_id
from exports.verify import release_available


class ReleaseMissing(LookupError):
    pass


class ReleaseWithdrawn(ValueError):
    pass


JURISDICTION = re.compile(r"[a-z0-9-]{2,30}")
MODES = {"corpus", "metadata"}


def _published(root: Path):
    """Completed release directories only, newest revision first; never writer staging names."""
    found = []
    for path in root.glob("*/*/*/manifest.json"):
        release, jurisdiction, mode = path.parent.relative_to(root).parts
        match = RELEASE_ID.fullmatch(release)
        if match is None or not JURISDICTION.fullmatch(jurisdiction) or mode not in MODES:
            continue
        try:
            validate_release_id(release)
        except ValueError:
            continue
        found.append(((match["day"], int(match["revision"] or 0), jurisdiction, mode), path))
    return [path for _, path in sorted(found, reverse=True)]


def holds_text(manifest: dict) -> bool:
    """Whether a release carries any full text (its versions file has records)."""
    versions = (manifest.get("files") or {}).get("versions.jsonl") or {}
    return bool(versions.get("records") or (manifest.get("coverage") or {}).get("published_versions"))


class LegacyReleases:
    """Release listing and files. A release holding full text is neither listed nor served unless
    bulk_text is set: a full-text download cannot be taken back, and who may take bulk text is the
    owner's decision when the first texts are cleared (D25(5)). Metadata releases stay public."""

    def __init__(self, store: Store, root: Path, bulk_text: bool = False):
        self.store = store
        self.root = root
        self.bulk_text = bulk_text

    def listing(self):
        current = self.store.load()
        result = []
        for path in _published(self.root):
            manifest = json.loads(path.read_text())
            if holds_text(manifest) and not self.bulk_text:
                continue
            if release_available(path.parent, current):
                manifest["download_base"] = "/releases/" + path.parent.relative_to(self.root).as_posix()
                result.append(manifest)
        return result

    def file(self, release_id, jurisdiction, mode, filename) -> Path:
        try:
            validate_release_id(release_id)
        except ValueError:
            raise ReleaseMissing() from None
        if not JURISDICTION.fullmatch(jurisdiction):
            raise ReleaseMissing()
        if mode not in MODES:
            raise ReleaseMissing()
        directory = self.root / release_id / jurisdiction / mode
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            raise ReleaseMissing()
        if holds_text(json.loads(manifest.read_text())) and not self.bulk_text:
            raise ReleaseMissing()  # as if absent: bulk text is not offered (D25(5))
        if not release_available(directory, self.store.load()):
            raise ReleaseWithdrawn("Release withdrawn or invalid; consult the latest release")
        allowed = set(json.loads(manifest.read_text())["files"]) | {"manifest.json"}
        path = directory / filename
        if filename not in allowed or Path(filename).name != filename:
            raise ReleaseMissing()
        if not path.is_file() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ReleaseMissing()
        return path
