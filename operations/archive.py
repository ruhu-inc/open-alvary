"""The write-once archive of fetched originals (CPR-05).

Every original that its acquisition assessment allows to be kept long-term is copied, by its SHA-256,
to ``sha256/ab/cd/<hash>`` under the archive root, with one manifest line recording what it is and
where it came from. Nothing in the archive is ever overwritten or removed by this code; the bucket's
locked retention policy (deployment/gcp/archive.sh) makes that true for every role. Originals with
personal data or an uncertain privacy status (most judgments) go to the unlocked tier instead, so
lawful erasure stays possible (SYS staged-artifact lifecycle).

  python -m operations.archive copy SOURCES ARCHIVE [--tier locked|unlocked] [--dry-run]
  python -m operations.archive verify ARCHIVE

SOURCES holds originals beside their ``<name>.provenance.json`` (url, retrieved_at, sha256, bytes);
ARCHIVE is the archive root (a mounted bucket in the cloud). ``verify`` rehashes every object and
reports any whose bytes no longer match its name, and any manifest line without its object.
"""

import argparse
import hashlib
import json
import mimetypes
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

MANIFEST = "manifest.jsonl"


def address(digest: str) -> str:
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def originals(sources: Path) -> list[tuple[Path, dict]]:
    """Each original with its provenance record; an original whose bytes do not match its recorded
    hash is refused before anything is copied."""
    found = []
    for record in sorted(sources.glob("*.provenance.json")):
        provenance = json.loads(record.read_text())
        stem = record.name.removesuffix(".provenance.json")
        candidates = [p for p in sources.glob(f"{stem}.*") if p.suffix not in (".json", ".txt", ".html")]
        if not candidates:
            continue
        original = sorted(candidates)[0]
        if provenance.get("sha256") and _hash(original) != provenance["sha256"]:
            raise ValueError(f"{original.name} no longer matches its recorded hash")
        found.append((original, provenance))
    return found


def copy(sources: Path, archive: Path, *, tier: str = "locked", dry_run: bool = False) -> dict:
    """Copy every original not yet archived; returns counts. Existing objects are never rewritten."""
    if tier not in ("locked", "unlocked"):
        raise ValueError("The tier is locked or unlocked")
    added = present = 0
    lines = []
    for original, provenance in originals(sources):
        digest = provenance.get("sha256") or _hash(original)
        target = archive / address(digest)
        if target.exists():
            present += 1
            continue
        added += 1
        lines.append(
            {
                "sha256": digest,
                "bytes": original.stat().st_size,
                "media_type": mimetypes.guess_type(original.name)[0] or "application/octet-stream",
                "name": original.name,
                "retrieval_url": provenance.get("url"),
                "retrieved_at": provenance.get("retrieved_at"),
                "tier": tier,
                "archived_at": datetime.now(UTC).isoformat(),
            }
        )
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(target.name + ".partial")
            shutil.copyfile(original, staging)
            if _hash(staging) != digest:
                staging.unlink()
                raise OSError(f"{original.name} changed while it was copied")
            staging.rename(target)
    if lines and not dry_run:
        with (archive / MANIFEST).open("a") as manifest:
            manifest.writelines(json.dumps(line, sort_keys=True) + "\n" for line in lines)
    return {"added": added, "already_archived": present, "tier": tier, "dry_run": dry_run}


def verify(archive: Path) -> dict:
    """Rehash every archived object against its address and the manifest."""
    tampered, missing, checked = [], [], 0
    for path in sorted((archive / "sha256").rglob("*")):
        if path.is_file() and not path.name.endswith(".partial"):
            checked += 1
            if _hash(path) != path.name:
                tampered.append(path.name)
    manifest = archive / MANIFEST
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            digest = json.loads(line)["sha256"]
            if not (archive / address(digest)).is_file():
                missing.append(digest)
    return {"checked": checked, "tampered": tampered, "missing": missing, "ok": not tampered and not missing}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    copying = commands.add_parser("copy")
    copying.add_argument("sources", type=Path)
    copying.add_argument("archive", type=Path)
    copying.add_argument("--tier", choices=("locked", "unlocked"), default="locked")
    copying.add_argument("--dry-run", action="store_true")
    checking = commands.add_parser("verify")
    checking.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    if args.command == "copy":
        print(json.dumps(copy(args.sources, args.archive, tier=args.tier, dry_run=args.dry_run)))
        return 0
    report = verify(args.archive)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
