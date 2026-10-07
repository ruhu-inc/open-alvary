"""Immutable, hashed release artifacts. Public projection is mandatory."""

import argparse
import json
import re
import tempfile
from collections import Counter
from hashlib import sha256
from pathlib import Path

from api.store import ROOT, Store, engine_for
from exports.identity import validate_release_id
from rights.policy import public_corpus
from schema.models import Corpus


def coverage(corpus: Corpus) -> dict:
    versioned = {v.source_id for v in corpus.versions}
    return {
        "catalogued_sources": len(corpus.sources),
        "open_full_text_sources": len(versioned),
        "metadata_only_sources": len(corpus.sources) - len(versioned),
        "published_versions": len(corpus.versions),
        "jurisdictions": dict(Counter(s.jurisdiction_id for s in corpus.sources)),
        "notice": "Catalogue coverage only; not a claim of complete or current law.",
    }


def write_release(
    corpus: Corpus, root: Path, release_id: str, jurisdiction: str, metadata_only: bool = False
) -> Path:
    validate_release_id(release_id)
    if not re.fullmatch(r"[a-z0-9-]{2,30}", jurisdiction):
        raise ValueError("Invalid jurisdiction path")
    public = public_corpus(corpus, metadata_only=metadata_only)
    if jurisdiction not in {j.id for j in public.jurisdictions}:
        raise ValueError("Unknown jurisdiction")
    ids = {s.id for s in public.sources if s.jurisdiction_id == jurisdiction}
    data = public.model_dump(mode="json")
    for key in ["sources", "rights", "versions", "structure", "citations", "amendments"]:
        data[key] = [
            r
            for r in data[key]
            if (r.get("source_id", r["id"]) in ids) and (key != "amendments" or r["target_source_id"] in ids)
        ]
    versions = {v["id"] for v in data["versions"]}
    data["translations"] = [
        r
        for r in data["translations"]
        if r["source_version_id"] in versions and r["translated_version_id"] in versions
    ]
    # Keep jurisdiction/authority/language reference tables for standalone validation.
    public = Corpus.model_validate(data)
    destination = root / release_id / jurisdiction / ("metadata" if metadata_only else "corpus")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("Releases are immutable; choose a new release ID or output directory")
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        stage = Path(tmp)
        files = {}
        for name in Corpus.model_fields:
            rows = sorted(getattr(public, name), key=lambda r: r.id)
            path = stage / f"{name}.jsonl"
            path.write_text(
                "".join(
                    json.dumps(r.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n"
                    for r in rows
                )
            )
            files[path.name] = {"sha256": sha256(path.read_bytes()).hexdigest(), "records": len(rows)}
        extras = {
            "coverage.json": coverage(public),
            "rights-summary.json": dict(Counter(r.status.value for r in public.rights)),
        }
        for name, payload in extras.items():
            path = stage / name
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            files[name] = {"sha256": sha256(path.read_bytes()).hexdigest()}
        readme = stage / "README.md"
        readme.write_text(
            f"# Open Alvary {release_id} / {jurisdiction}\n\n"
            f"Mode: {'metadata-only' if metadata_only else 'rights-filtered corpus'}.\n\n"
            "Only versions passing the rights and content gates appear. Empty text files are intentional.\n"
            "Catalogue metadata does not grant rights to linked documents. See each rights record.\n"
            "No claim of comprehensive coverage or current consolidated law.\n"
            "The manifest hashes every payload file; it is not a signature or a legal clearance.\n"
        )
        files[readme.name] = {"sha256": sha256(readme.read_bytes()).hexdigest()}
        manifest = {
            "schema_version": "0.1.0",
            "release_id": release_id,
            "jurisdiction": jurisdiction,
            "mode": "metadata" if metadata_only else "corpus",
            "files": files,
            "coverage": coverage(public),
            "data_licence": "Per-record; no blanket legal-text licence",
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        # Same-filesystem atomic publication; never overwrite an existing release.
        stage.rename(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--jurisdiction", default="ng")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "releases")
    args = parser.parse_args()
    print(
        write_release(
            Store(engine_for()).load(), args.output, args.date, args.jurisdiction, args.metadata_only
        )
    )
