"""The open evaluation runner (EVAL-05, RSV-12): runs a benchmark's cases through the retrieval service,
scores them and records an immutable run; compares two runs under a gate profile.

    python -m evaluations run --benchmark benchmarks/retrieval/ng-legislation-dev-1 --database URL
    python -m evaluations run --benchmark ... --base https://api.open.alvary.ai/api/v1   (OPEN_ALVARY_KEY)
    python -m evaluations compare --benchmark ... BASELINE.json CANDIDATE.json

A run records the benchmark's digest, the code commit (and a hash of any uncommitted changes), the
retrieval revisions the service reported, and every case's delivered units and scores. An error or
timeout on a case that expects an answer scores as a miss, never as an abstention.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from evaluations import records, scoring

MAX_UNITS = 20  # so recall@20 is measurable


def git_state(root: Path) -> dict:
    def run(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False).stdout

    commit = run("rev-parse", "HEAD").strip() or None
    patch = run("diff", "HEAD")
    return {
        "commit": commit,
        "dirty": bool(patch),
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch else None,
    }


def in_process(database, **retriever) -> Callable[[records.Case], dict]:
    """Retrieve through the service's own code against a database (a URL or an engine: a scratch copy
    or a replica); ``retriever`` passes options to the Retriever (search_engine, vector_profile,
    embedder, reranker), so one corpus can be run under several configurations."""
    from sqlalchemy import create_engine

    from api import text_index
    from api.catalogue import LegacyCatalogue
    from api.repositories import LegacyRepository
    from retrieval.pack import Request, Retriever

    engine = create_engine(database) if isinstance(database, str) else database

    def ask(case: records.Case) -> dict:
        with engine.begin() as conn:
            service = LegacyCatalogue(LegacyRepository(conn))
            filters = text_index.Filters(
                jurisdictions=case.jurisdictions, language=case.language, as_of=case.as_of
            )
            pack = Retriever(service, **retriever).retrieve(
                Request(query=case.query, filters=filters, max_units=MAX_UNITS)
            )
            return pack.model_dump(mode="json")

    return ask


def over_http(base: str, key: str) -> Callable[[records.Case], dict]:
    import httpx

    client = httpx.Client(base_url=base.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, timeout=30)

    def ask(case: records.Case) -> dict:
        params: list[tuple[str, str]] = [("q", case.query), ("max_units", str(MAX_UNITS))]
        params += [("jurisdiction", j) for j in case.jurisdictions or ()]
        if case.language:
            params.append(("language", case.language))
        if case.as_of:
            params.append(("as_of", case.as_of.isoformat()))
        answer = client.get("/retrieve", params=params)
        answer.raise_for_status()
        return answer.json()

    return ask


def run(
    benchmark: records.Benchmark, ask: Callable[[records.Case], dict], *, split: str, system: dict
) -> dict:
    per_case, outcomes, failures, revisions = {}, {}, {}, None
    for case in benchmark.cases:
        if case.split != split:
            continue
        try:
            pack = ask(case)
        except Exception as error:  # every failure is recorded and scored as a miss
            failures[case.case_id] = f"{type(error).__name__}: {str(error)[:300]}"
            pack = None
        delivered = (
            [scoring.Delivered(u["source_id"], u["locator"], u["unit"], u["text"]) for u in pack["evidence"]]
            if pack
            else []
        )
        if pack and revisions is None:
            revisions = pack["revisions"]
        scores = scoring.score_case(case, benchmark.judged(case.case_id), delivered)
        if pack is None and case.expected == "abstain":
            scores = {"abstain": 0.0}  # a failure is never a correct abstention
        per_case[case.case_id] = scores
        outcomes[case.case_id] = {
            "status": pack["status"] if pack else "error",
            "routes": pack["query"]["routes"] if pack else [],
            "delivered": [[d.source_id, d.locator, d.unit] for d in delivered],
        }
    return {
        "run_id": f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now(UTC).isoformat(),
        "benchmark": {
            "id": benchmark.manifest.id,
            "version": benchmark.manifest.version,
            "digest": benchmark.digest,
            "review_state": benchmark.manifest.review_state,
            "synthetic": benchmark.manifest.synthetic,
        },
        "split": split,
        "system": system | {"revisions": revisions},
        "metrics": scoring.aggregate(per_case),
        "cases": per_case,
        "outcomes": outcomes,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluations")
    commands = parser.add_subparsers(dest="command", required=True)
    go = commands.add_parser("run", help="Run a benchmark and write its run record")
    go.add_argument("--benchmark", type=Path, required=True)
    go.add_argument("--split", default="dev")
    where = go.add_mutually_exclusive_group(required=True)
    where.add_argument("--database", help="A database URL: the service's code runs in process")
    where.add_argument("--base", help="An API base URL; the key is read from OPEN_ALVARY_KEY")
    go.add_argument("--out", type=Path, default=Path("reports/evaluations"))
    compare = commands.add_parser("compare", help="Gate a candidate run against a baseline run")
    compare.add_argument("--benchmark", type=Path, required=True)
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    benchmark = records.load(args.benchmark)
    if args.command == "run":
        root = Path(__file__).resolve().parent.parent
        if args.database:
            ask, system = in_process(args.database), {"adapter": "in_process"}
        else:
            key = os.environ.get("OPEN_ALVARY_KEY")
            if not key:
                print("Set OPEN_ALVARY_KEY to an API key first.", file=sys.stderr)
                return 2
            ask, system = over_http(args.base, key), {"adapter": "http", "base": args.base}
        record = run(benchmark, ask, split=args.split, system=system | git_state(root))
        target = args.out / record["run_id"]
        target.mkdir(parents=True, exist_ok=True)
        (target / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                {
                    "run": str(target / "run.json"),
                    "metrics": record["metrics"],
                    "failures": len(record["failures"]),
                },
                indent=2,
            )
        )
        return 0
    profile = records.gate_profile(args.benchmark)
    if profile is None:
        print("The benchmark has no gate.json.", file=sys.stderr)
        return 2
    baseline, candidate = (json.loads(p.read_text()) for p in (args.baseline, args.candidate))
    for name, record in (("baseline", baseline), ("candidate", candidate)):
        if record["benchmark"]["digest"] != benchmark.digest:
            print(f"The {name} run is of another benchmark version.", file=sys.stderr)
            return 2
    decision = scoring.gate(baseline, candidate, list(benchmark.cases), profile)
    print(json.dumps(decision, indent=2))
    return 0 if decision["state"] == "pass" else 1
