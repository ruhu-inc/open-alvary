"""The S1 catalogue benchmark (OPS-04, interface budgets): 10,000 synthetic metadata records, 10
concurrent clients, and p95 under two seconds for warm search and detail requests, with the cold
start reported apart. The service runs as deployed (one uvicorn process; the database pool, admission
classes and API credential gate of one of three replicas) over a scratch PostgreSQL database that
the script creates afresh, migrates and fills: the Nigerian catalogue and synthetic records, labelled
as such, in a synthetic jurisdiction. The website's data path and the keyed REST API are measured
apart; the API key is a local test key made for the run.

    python -m scripts.benchmark_catalogue postgresql+psycopg://postgres@127.0.0.1:55450/oa_bench_scratch

The database name must contain "bench" and "scratch": it is dropped and created again. Prints a JSON
report and exits 1 when warm search or detail p95 reaches two seconds, or any request fails.
"""

import argparse
import asyncio
import base64
import json
import os
import random
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
P95_TARGET_MS = 2000
WORDS = (
    "land water mining forestry fisheries tax customs labour health education transport aviation "
    "energy electricity telecommunications banking insurance securities companies cooperatives "
    "procurement elections police defence immigration nationality family succession property "
    "environment agriculture trade investment tourism culture sport media archives statistics"
).split()
TYPES = ("act", "act", "act", "regulation", "regulation", "regulation", "gazette", "judgment", "other")
STATUSES = ("current", "current", "unknown", "unknown", "repealed", "superseded", "historical")


def synthetic(records: int, seed: int = 7):
    """The Nigerian catalogue and `records` synthetic metadata records with their rights records,
    citations and checked links (made under a few parent laws, repeals, amendments), so that every
    public route has realistic work: facets, ranking, history pages and a law enabling hundreds."""
    from schema.models import (
        Amendment,
        Authority,
        Citation,
        Corpus,
        Jurisdiction,
        Language,
        RightsRecord,
        Source,
    )

    rng = random.Random(seed)
    corpus = Corpus.model_validate_json((ROOT / "ingestion/catalogue/ng.json").read_text())
    have = {language.id for language in corpus.languages}
    corpus.languages += [
        Language(id=code, name=name, tag=code)
        for code, name in (("fr", "French"), ("rw", "Kinyarwanda"))
        if code not in have
    ]
    corpus.jurisdictions.append(
        Jurisdiction(
            id="zz", name="Synthetic benchmark jurisdiction", country_code="ZZ", languages=["en", "fr", "rw"]
        )
    )
    corpus.authorities.append(
        Authority(
            id="zz-synthetic",
            jurisdiction_id="zz",
            name="Synthetic benchmark authority",
            kind="legislature",
            official_url="https://example.org/synthetic/",
        )
    )
    ids = [f"synthetic-{i:05d}" for i in range(records)]
    for i, source_id in enumerate(ids):
        subject = " and ".join(rng.sample(WORDS, 2))
        year = rng.randint(1962, 2026)
        corpus.sources.append(
            Source(
                id=source_id,
                jurisdiction_id="zz",
                authority_id="zz-synthetic",
                title=f"Synthetic benchmark record {i}: {subject.capitalize()} Act {year}",
                document_type=rng.choice(TYPES),
                canonical_url=f"https://example.org/synthetic/{i}",
                url_kind="document",
                citation=f"SYN {i}/{year}",
                language=rng.choice(("en", "en", "fr", "rw")),
                legal_status=rng.choice(STATUSES),
                summary=f"A synthetic record for load testing, about {subject}; not law.",
                year=year,
            )
        )
        corpus.rights.append(
            RightsRecord(
                id=f"rights-{source_id}",
                source_id=source_id,
                status=rng.choice(("LINK_ONLY", "RIGHTS_UNDER_REVIEW")),
                metadata_public=True,
                metadata_basis="Synthetic benchmark metadata; not law",
            )
        )
        corpus.citations.append(Citation(id=f"citation-{source_id}", source_id=source_id, value=f"SYN {i}"))
    # Links: three parent laws enabling many instruments (the largest a few hundred, as Rwanda's
    # public service statute does), repeal lines and amendments.
    parents = ids[:3]
    links = []
    for i, source_id in enumerate(ids[3:], 3):
        if i % 12 == 0:
            links.append(("made_under", source_id, parents[i % 3]))
        if i % 25 == 0:
            links.append(("repeals", source_id, ids[i - 1]))
        if i % 40 == 0:
            links.append(("amends", source_id, ids[i - 7]))
    corpus.amendments += [
        Amendment(
            id=f"synthetic-link-{n:05d}",
            source_id=newer,
            target_source_id=older,
            kind=kind,
            evidence_url=f"https://example.org/synthetic/{newer}",
            verified=True,
        )
        for n, (kind, newer, older) in enumerate(links)
    ]
    return corpus


def fresh_database(url: str) -> None:
    parsed = make_url(url)
    name = parsed.database or ""
    if "bench" not in name or "scratch" not in name:
        raise SystemExit(
            "The database name must contain 'bench' and 'scratch': it is dropped and made again."
        )
    admin = create_engine(parsed.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()


def prepare(url: str, records: int, pepper: bytes) -> dict:
    """A migrated database holding the catalogue, and an organisation with a local test API key."""
    from alembic import command
    from alembic.config import Config

    from accounts import policies
    from accounts.store import Accounts
    from api.store import Store
    from rights.database import PolicyNamespace

    fresh_database(url)
    engine = create_engine(url, poolclass=NullPool)
    config = Config(str(ROOT / "alembic.ini"))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    started = time.perf_counter()
    corpus = synthetic(records)
    Store(engine).save(corpus)
    loaded = time.perf_counter() - started
    with engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    accounts = Accounts(engine, namespace, key_pepper=pepper)
    # A plan whose allowance the run cannot reach: the benchmark measures capacity, not quotas.
    accounts.set_tiers(
        [
            {
                "id": "benchmark",
                "name": "Benchmark",
                "description": "Local benchmark only",
                "includes": ["rest"],
                "allowance": {"requests_per_minute": 100000, "burst": 10000, "concurrency": 50},
                "published": True,
            }
        ]
    )
    person = accounts.sign_in(
        issuer="https://login.example.test/",
        subject="benchmark|1",
        method="email",
        email="benchmark@example.test",
        label="benchmark@example.test",
    )
    accounts.accept_policies(person["id"], dict(policies.CURRENT))
    organisation = accounts.welcome(person["id"], "Benchmark", "Benchmark")
    accounts.grant_access(
        "benchmark", organisation, tier="benchmark", scope={"all": True}, source="manual", ends_at=None,
        reason="Local S1 benchmark",
    )  # fmt: skip
    key = accounts.create_key(person["id"], organisation, kind="personal", name="benchmark")["secret"]
    with engine.connect() as conn:
        size = conn.scalar(text("SELECT pg_database_size(current_database())"))
    engine.dispose()
    return {
        "key": key,
        "sources": len(corpus.sources),
        "links": len(corpus.amendments),
        "load_seconds": round(loaded, 1),
        "database_mb": round(size / 1024**2, 1),
    }


def admission_file(example: str, folder: Path) -> Path:
    """An admission class as deployed (deployment/gcp/public_service.sh): one of three replicas, a
    pool of ten connections each, one trusted proxy in front (the run's client addresses)."""
    config = json.loads((ROOT / "deployment" / example).read_text())
    config.update(
        hmac_secret=secrets.token_hex(32),
        previous_hmac_secret=None,
        trusted_proxies=1,
        api_replicas=3,
        db_connections=50,
        operator_reserve=5,
        other_workers=5,
        public_pool_size=30,
    )
    path = folder / example.replace(".example", "")
    path.write_text(json.dumps(config))
    return path


def start_service(url: str, port: int, pepper: bytes, folder: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "ALVARY_RUNTIME_MODE": "persistent",
        "ALVARY_ACCOUNTS": "1",
        "ALVARY_KEY_PEPPER": base64.b64encode(pepper).decode(),
        "ALVARY_ADMISSION_CONFIG": str(admission_file("admission.example.json", folder)),
        "ALVARY_SITE_ADMISSION_CONFIG": str(admission_file("site-admission.example.json", folder)),
        "ALVARY_API_ORIGIN": f"http://127.0.0.1:{port}",
    }
    for name in ("ALVARY_CONSOLE_CLIENT_ID", "RESEND_API_KEY", "ALVARY_FRONTEND"):
        env.pop(name, None)
    command = [sys.executable, "-m", "uvicorn", "api.service:app", "--host", "127.0.0.1", "--port", str(port)]
    command += ["--no-access-log", "--timeout-keep-alive", "5"]  # as the container runs it
    return subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return round(int(out or 0) / 1024, 1)


def requests_for(rng: random.Random, records: int) -> list[tuple[str, str]]:
    """One visitor's turn on the website and one API call: (kind, path)."""
    source = f"synthetic-{rng.randrange(records):05d}"
    word = rng.choice(WORDS)
    site = rng.choice(
        [
            ("search", f"/site/v1/search?q={word}"),
            ("search", f"/site/v1/search?q={word}&type=act&sort=newest"),
            ("search", "/site/v1/search?q=nothing-matches-this"),
            ("browse", f"/site/v1/sources?jurisdiction=zz&page={rng.randint(1, 25)}"),
            ("browse", f"/site/v1/sources?type=regulation&year={rng.randint(1962, 2026)}"),
            ("detail", f"/site/v1/sources/{source}"),
            ("detail", f"/site/v1/sources/{source}/versions"),
            ("detail", f"/site/v1/rights/{source}"),
            ("history", f"/site/v1/sources/{source}/relationships"),
            ("history", "/site/v1/sources/synthetic-00000/relationships"),  # a law enabling hundreds
            ("coverage", "/site/v1/coverage"),
        ]
    )
    api = rng.choice(
        [
            ("api search", f"/api/v1/search?q={word}&limit=25"),
            ("api detail", f"/api/v1/sources/{source}"),
            ("api history", f"/api/v1/sources/{source}/relationships"),
            ("api browse", f"/api/v1/sources?limit=50&offset={rng.randrange(0, records, 50)}"),
        ]
    )
    return [site, api]


async def load(base: str, key: str, records: int, clients: int, seconds: float, warmup: float) -> dict:
    """`clients` concurrent clients, each asking as soon as its last answer came, from addresses of
    its own (as many visitors would), for `warmup` seconds unmeasured, then `seconds` measured."""
    results: dict[str, list[float]] = defaultdict(list)
    statuses: Counter = Counter()
    limits = httpx.Limits(max_connections=clients * 2, max_keepalive_connections=clients * 2)
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as http:

        async def client(number: int):
            rng = random.Random(number)
            measure_from, stop = time.perf_counter() + warmup, time.perf_counter() + warmup + seconds
            while time.perf_counter() < stop:
                for kind, path in requests_for(rng, records):
                    headers = {"X-Forwarded-For": f"198.51.100.{number * 20 + rng.randrange(20)}"}
                    if kind.startswith("api"):
                        headers["Authorization"] = f"Bearer {key}"
                    started = time.perf_counter()
                    try:
                        status = (await http.get(path, headers=headers)).status_code
                    except httpx.HTTPError:
                        status = "error"
                    if started >= measure_from:
                        results[kind].append(time.perf_counter() - started)
                        statuses[(kind, status)] += 1

        await asyncio.gather(*(client(n) for n in range(clients)))
    return {"latencies": results, "statuses": statuses}


def percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(len(ordered) * q))] * 1000, 1)  # noqa: E731
    return {
        "count": len(ordered),
        "p50_ms": round(statistics.median(ordered) * 1000, 1),
        "p95_ms": pick(0.95),
        "p99_ms": pick(0.99),
        "max_ms": round(ordered[-1] * 1000, 1),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("database_url")
    parser.add_argument("--records", type=int, default=10000)
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--warmup", type=float, default=15)
    parser.add_argument("--port", type=int, default=8077)
    args = parser.parse_args(argv)
    pepper = secrets.token_bytes(32)
    setup = prepare(args.database_url, args.records, pepper)
    base = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory() as folder:
        started = time.perf_counter()
        server = start_service(args.database_url, args.port, pepper, Path(folder))
        try:
            ready = None
            while ready is None and time.perf_counter() - started < 60:
                try:
                    if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                        ready = time.perf_counter() - started
                except httpx.HTTPError:
                    time.sleep(0.1)
            if ready is None:
                raise SystemExit(
                    "The service did not start: " + server.stderr.read1(4000).decode(errors="replace")
                )
            # Cold: the first request of each kind after start, one at a time.
            cold = {}
            first = {"X-Forwarded-For": "198.51.100.250"}
            for kind, path in (
                ("search", "/site/v1/search?q=land"),
                ("detail", "/site/v1/sources/synthetic-00042"),
                ("history", "/site/v1/sources/synthetic-00000/relationships"),
                ("api search", "/api/v1/search?q=land"),
            ):
                headers = {
                    **first,
                    **({"Authorization": f"Bearer {setup['key']}"} if kind.startswith("api") else {}),
                }
                at = time.perf_counter()
                status = httpx.get(base + path, headers=headers, timeout=30).status_code
                cold[kind] = {"ms": round((time.perf_counter() - at) * 1000, 1), "status": status}
            memory_before = rss_mb(server.pid)
            run = asyncio.run(load(base, setup["key"], args.records, args.clients, args.seconds, args.warmup))
            memory_after = rss_mb(server.pid)
        finally:
            server.terminate()
            server.wait(timeout=10)
    latencies, statuses = run["latencies"], run["statuses"]
    measured = sum(len(v) for v in latencies.values())
    warm = {kind: percentiles(values) for kind, values in sorted(latencies.items())}
    failures = {f"{kind} {status}": n for (kind, status), n in statuses.items() if status != 200}
    gate = [
        kind
        for kind in ("search", "detail", "api search", "api detail")
        if warm.get(kind, {}).get("p95_ms", 0) >= P95_TARGET_MS
    ]
    report = {
        "target": f"{args.records} records, {args.clients} clients, warm search/detail p95 < {P95_TARGET_MS} ms",
        "setup": {k: v for k, v in setup.items() if k != "key"},
        "cold_start": {"ready_seconds": round(ready, 2), "first_requests": cold},
        "warm": warm,
        "throughput_per_second": round(measured / args.seconds, 1),
        "non_200": failures,
        "memory_mb": {"before_load": memory_before, "after_load": memory_after},
        "passed": not gate and not failures,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
