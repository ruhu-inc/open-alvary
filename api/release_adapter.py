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


class LegacyReleases:
    def __init__(self, store: Store, root: Path):
        self.store = store
        self.root = root

    def listing(self):
        current = self.store.load()
        result = []
        for path in _published(self.root):
            if release_available(path.parent, current):
                manifest = json.loads(path.read_text())
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
        if not release_available(directory, self.store.load()):
            raise ReleaseWithdrawn("Release withdrawn or invalid; consult the latest release")
        allowed = set(json.loads(manifest.read_text())["files"]) | {"manifest.json"}
        path = directory / filename
        if filename not in allowed or Path(filename).name != filename:
            raise ReleaseMissing()
        if not path.is_file() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ReleaseMissing()
        return path
