"""D3 fixture load test: 25 browsers behind one address, 8 requests each, ≤4 in parallel.

Read-only. Reports status counts, 429s and latency percentiles so admission values can be
checked on a real host before any public capacity claim. Example:
    python -m scripts.load_fixture http://127.0.0.1:8018 --duration 60 --forwarded-for 203.0.113.9
"""

import argparse
import asyncio
import random
import statistics
import time
from collections import Counter

import httpx

PATHS = ["/search?q=act", "/sources", "/sources/ng-foi-act-2011", "/coverage", "/jurisdictions"]
TEXT_PATHS = ["/sources/ng-foi-act-2011/versions"]


async def browser(client, duration, parallel, results):
    plan = ["/search?q=act", *random.choices(PATHS[1:], k=4), *TEXT_PATHS * 3]
    random.shuffle(plan)
    semaphore = asyncio.Semaphore(parallel)

    async def fetch(path, delay):
        await asyncio.sleep(delay)
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await client.get(path)
                results.append((response.status_code, time.perf_counter() - started))
            except httpx.HTTPError:
                results.append(("error", time.perf_counter() - started))

    await asyncio.gather(*(fetch(path, random.uniform(0, duration)) for path in plan))


async def run(base_url, browsers, duration, parallel, forwarded_for):
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    results = []
    limits = httpx.Limits(max_connections=browsers * parallel)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30, limits=limits) as client:
        await asyncio.gather(*(browser(client, duration, parallel, results) for _ in range(browsers)))
    statuses = Counter(status for status, _ in results)
    latencies = sorted(seconds for _, seconds in results)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 0
    return {
        "requests": len(results),
        "statuses": dict(statuses),
        "rate_limited": statuses.get(429, 0),
        "p50_ms": round(statistics.median(latencies) * 1000, 1) if latencies else None,
        "p95_ms": round(p95 * 1000, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("base_url")
    parser.add_argument("--browsers", type=int, default=25)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--forwarded-for", help="Single shared client address, when behind one trusted proxy")
    args = parser.parse_args()
    report = asyncio.run(run(args.base_url, args.browsers, args.duration, args.parallel, args.forwarded_for))
    print(report)
    raise SystemExit(1 if report["rate_limited"] or "error" in report["statuses"] else 0)


if __name__ == "__main__":
    main()
