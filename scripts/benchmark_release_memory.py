"""How much memory checking a release's rows takes, all at once and a row at a time (exports.integrity), on a
synthetic release of invented records written to a scratch folder and checked in a fresh process each way.

    python -m scripts.benchmark_release_memory                  # 120,000 records
    python -m scripts.benchmark_release_memory --records 5000 --text-bytes 8000 --nodes 6

Each record is a source of a synthetic jurisdiction, with its rights record, a citation, one version whose text
has --text-bytes bytes, and --nodes structure nodes: the whole text, and sections nested in it. "at_once" is
how exports.verify checked a release before: every row parsed, held and validated as one Corpus. "row_at_a_time"
is how it checks one now. Prints JSON: for each, whether the release was valid, its time, the process's peak
resident memory (getrusage, a run of its own) and the peak of Python's own allocations (tracemalloc, another);
and for the second, the scratch database's largest size, which is memory where the temporary folder is.

Not part of the test run; tests/test_benchmark_release_memory.py runs it on a few records.
"""

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-01-01T00:00:00Z"
WORDS = b"synthetic benchmark text of no legal force, made up for measuring alone; "


def text_of(record: int, size: int) -> str:
    head = f"Synthetic record {record}: not law. Café ✓\n".encode()
    body = (WORDS * (size // len(WORDS) + 1))[: max(size - len(head), 0)]
    return (head + body).decode()


def between(raw: bytes, offset: int) -> int:
    """The first offset from this one that falls between characters."""
    while offset < len(raw) and raw[offset] & 0xC0 == 0x80:
        offset += 1
    return offset


def write(folder: Path, records: int, text_bytes: int, nodes: int) -> dict:
    """The release's row files, as exports.release writes them: one JSON object a line."""
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        name: (folder / f"{name}.jsonl").open("w", encoding="utf-8")
        for name in (
            "languages",
            "jurisdictions",
            "authorities",
            "sources",
            "rights",
            "versions",
            "structure",
            "citations",
            "amendments",
            "translations",
        )
    }

    def put(name, row):
        files[name].write(json.dumps(row) + "\n")

    put("languages", {"id": "en", "name": "English", "tag": "en"})
    put("jurisdictions", {"id": "zz", "name": "Synthetica", "country_code": "ZZ", "languages": ["en"]})
    put(
        "authorities",
        {
            "id": "zz-assembly",
            "jurisdiction_id": "zz",
            "name": "Synthetic Assembly",
            "kind": "legislature",
            "official_url": "https://example.org/assembly",
        },
    )
    for n in range(records):
        source = f"synthetic-{n:07d}"
        put(
            "sources",
            {
                "id": source,
                "jurisdiction_id": "zz",
                "authority_id": "zz-assembly",
                "title": f"Synthetic Act {n} (benchmark record, not law)",
                "document_type": "act",
                "canonical_url": f"https://example.org/{source}",
                "citation": f"Synthetic Act {n}",
                "summary": "Synthetic benchmark record; not law.",
                "year": 1990 + n % 30,
            },
        )
        put(
            "rights",
            {
                "id": f"rights-{source}",
                "source_id": source,
                "status": "OPEN",
                "rights_holder": "Synthetic test author",
                "licence": "TEST-ONLY-NOT-A-LEGAL-LICENCE",
                "redistribution_allowed": "yes",
                "commercial_reuse_allowed": "yes",
                "derivatives_allowed": "yes",
                "privacy_status": "cleared",
                "reviewer": "synthetic-reviewer",
                "reviewed_at": NOW,
                "evidence": ["https://example.org/permission"],
                "metadata_public": True,
                "metadata_basis": "Synthetic benchmark metadata; not law",
            },
        )
        put("citations", {"id": f"citation-{source}", "source_id": source, "value": f"Synthetic Act {n}"})
        if not text_bytes:
            continue
        text = text_of(n, text_bytes)
        raw = text.encode()
        content = sha256(raw).hexdigest()
        version = f"{source}-v1"
        put(
            "versions",
            {
                "id": version,
                "source_id": source,
                "canonical_url": f"https://example.org/{source}/text",
                "retrieved_at": NOW,
                "raw_hash": content,
                "content_hash": content,
                "canonical_text": text,
                "parser": "synthetic",
            },
        )
        # The whole text, and the sections it is cut into, each cut between characters.
        sections = max(nodes - 1, 0)
        cuts = [between(raw, len(raw) * k // max(sections, 1)) for k in range(sections + 1)]
        for k, (start, end) in enumerate([(0, len(raw)), *zip(cuts, cuts[1:], strict=False)]):
            put(
                "structure",
                {
                    "id": f"{version}-n{k}",
                    "source_id": source,
                    "version_id": version,
                    "parent_id": f"{version}-n0" if k else None,
                    "kind": "section" if k else "document",
                    "locator": f"section/{k}" if k else "document",
                    "order": k,
                    "start_byte": start,
                    "end_byte": end,
                    "source_hash": content,
                    "span_hash": sha256(raw[start:end]).hexdigest(),
                },
            )
    for file in files.values():
        file.close()
    return {name: (folder / f"{name}.jsonl").stat().st_size for name in files}


def child(mode: str, folder: Path, traced: bool) -> dict:
    """One way of checking, in this process, which has done nothing else."""
    from exports import integrity
    from exports.verify import _rows
    from schema.models import Corpus

    scratch = folder / "scratch"
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir = str(scratch)
    largest = {"bytes": 0}
    done = threading.Event()

    def watch():
        while not done.wait(0.02):
            size = sum(path.stat().st_size for path in scratch.rglob("*") if path.is_file())
            largest["bytes"] = max(largest["bytes"], size)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    if traced:
        tracemalloc.start()
    started = time.perf_counter()
    if mode == "at_once":
        try:
            Corpus.model_validate({name: list(_rows(folder, name)) for name in Corpus.model_fields})
            valid = True
        except (ValueError, KeyError, TypeError):
            valid = False
    else:
        valid = integrity.valid(lambda name: _rows(folder, name))
    seconds = time.perf_counter() - started
    done.set()
    watcher.join()
    report = {"valid": valid, "seconds": round(seconds, 1)}
    if traced:
        report["python_peak_mb"] = round(tracemalloc.get_traced_memory()[1] / 2**20, 1)
    else:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["peak_rss_mb"] = round(peak / (2**20 if sys.platform == "darwin" else 2**10), 1)
        if mode == "row_at_a_time":
            report["scratch_largest_mb"] = round(largest["bytes"] / 2**20, 1)
    return report


def measure(mode: str, folder: Path, traced: bool) -> dict:
    command = [sys.executable, "-m", "scripts.benchmark_release_memory", "--child", mode, str(folder)]
    done = subprocess.run(
        command + (["--traced"] if traced else []), cwd=ROOT, capture_output=True, text=True
    )
    if done.returncode:
        raise SystemExit(f"{mode} failed:\n{done.stderr}")
    return json.loads(done.stdout)


def run(records: int, text_bytes: int, nodes: int, folder: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="release-benchmark-") as made:
        release = Path(folder or made) / "release"
        sizes = write(release, records, text_bytes, nodes)
        report = {
            "records": records,
            "text_bytes": text_bytes,
            "nodes": nodes,
            "rows": sum(1 for name in sizes for _ in (release / f"{name}.jsonl").open()),
            "release_mb": round(sum(sizes.values()) / 2**20, 1),
        }
        for mode in ("at_once", "row_at_a_time"):
            plain, traced = measure(mode, release, False), measure(mode, release, True)
            report[mode] = {**plain, "python_peak_mb": traced["python_peak_mb"]}
        return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--records", type=int, default=120_000)
    parser.add_argument("--text-bytes", type=int, default=2_000)
    parser.add_argument("--nodes", type=int, default=3)
    parser.add_argument("--child", nargs=2, metavar=("MODE", "FOLDER"), help=argparse.SUPPRESS)
    parser.add_argument("--traced", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.child:
        mode, folder = options.child
        print(json.dumps(child(mode, Path(folder), options.traced)))
        return 0
    report = run(options.records, options.text_bytes, options.nodes)
    print(json.dumps(report, indent=2))
    return 0 if report["at_once"]["valid"] and report["row_at_a_time"]["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
