"""Load-test harness for concurrent multi-agent workbench claims and saves.

Spawns configurable concurrent agent workers against a workbench instance,
exercising:
  1. POST /api/queue/claim-next
  2. GET  /api/documents/{act_id}/pages/{page}
  3. GET  /api/documents/{act_id}/pages/{page}/image (when --include-images)
  4. POST /api/documents/{act_id}/pages/{page} (with lost-save readback check)
  5. POST /api/claims/{work_id}/renew
  6. POST /api/claims/{work_id}/release

Passes when:
  - 0 double-claims (no work held by two workers at once)
  - 0 lost saves (every committed revision is visible on readback)
  - 0 unexpected HTTP/runtime errors
  - p90 request latency < --p90-limit (default 1.0s)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class LoadConfig:
    url: str = "http://127.0.0.1:8000"
    agents: int = 10
    pages: int = 2
    include_images: bool = True
    email: str = "load-agent@example.org"
    label_prefix: str = "load-agent"
    task: str = "review"
    jurisdiction: str = ""
    p90_limit_s: float = 1.0
    timeout_s: float = 30.0
    json_output: bool = False


@dataclass
class LoadReport:
    agents: int
    claims_acquired: int = 0
    nothing_to_claim: int = 0
    pages_loaded: int = 0
    images_fetched: int = 0
    pages_saved: int = 0
    renewals: int = 0
    releases: int = 0
    double_claims: int = 0
    lost_saves: int = 0
    errors: list[str] = field(default_factory=list)
    latencies_s: list[float] = field(default_factory=list)
    p90_limit_s: float = 1.0

    @property
    def p50_s(self) -> float:
        return percentile(self.latencies_s, 0.50)

    @property
    def p90_s(self) -> float:
        return percentile(self.latencies_s, 0.90)

    @property
    def p99_s(self) -> float:
        return percentile(self.latencies_s, 0.99)

    @property
    def max_s(self) -> float:
        return max(self.latencies_s) if self.latencies_s else 0.0

    def failures(self) -> list[str]:
        issues: list[str] = []
        if self.double_claims > 0:
            issues.append(f"double_claims={self.double_claims} (expected 0)")
        if self.lost_saves > 0:
            issues.append(f"lost_saves={self.lost_saves} (expected 0)")
        if self.errors:
            issues.append(f"errors={len(self.errors)} (expected 0)")
        if self.latencies_s and self.p90_s >= self.p90_limit_s:
            issues.append(f"p90={self.p90_s:.3f}s >= {self.p90_limit_s:.3f}s")
        return issues

    @property
    def passed(self) -> bool:
        return len(self.failures()) == 0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "agents": self.agents,
            "claims_acquired": self.claims_acquired,
            "nothing_to_claim": self.nothing_to_claim,
            "pages_loaded": self.pages_loaded,
            "images_fetched": self.images_fetched,
            "pages_saved": self.pages_saved,
            "renewals": self.renewals,
            "releases": self.releases,
            "double_claims": self.double_claims,
            "lost_saves": self.lost_saves,
            "requests": len(self.latencies_s),
            "p50_s": round(self.p50_s, 4),
            "p90_s": round(self.p90_s, 4),
            "p99_s": round(self.p99_s, 4),
            "max_s": round(self.max_s, 4),
            "p90_limit_s": self.p90_limit_s,
            "errors": list(self.errors),
            "failures": self.failures(),
        }


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile for q in [0, 1]. Returns 0.0 for empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if q <= 0.0:
        return ordered[0]
    if q >= 1.0:
        return ordered[-1]
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


class ActiveClaimTracker:
    """Tracks currently active claims across concurrent workers to detect double-claims."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}
        self.collisions: list[tuple[str, str, str]] = []

    def acquire(self, work_id: str, label: str) -> bool:
        """Record work_id as held by label. Returns False if another worker holds it."""
        with self._lock:
            existing = self._active.get(work_id)
            if existing is not None and existing != label:
                self.collisions.append((work_id, existing, label))
                return False
            self._active[work_id] = label
            return True

    def release(self, work_id: str, label: str) -> None:
        with self._lock:
            if self._active.get(work_id) == label:
                del self._active[work_id]


def parse_args(argv: Sequence[str] | None = None) -> LoadConfig:
    parser = argparse.ArgumentParser(
        description="Run concurrent multi-agent claim/load/save/renew/release load tests."
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Workbench base URL")
    parser.add_argument("--agents", type=int, default=10, help="Number of concurrent agent workers")
    parser.add_argument("--pages", type=int, default=2, help="Pages to load/save per claimed work")
    parser.add_argument(
        "--include-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fetch page images (GET /api/documents/{act_id}/pages/{page}/image)",
    )
    parser.add_argument("--email", default="load-agent@example.org", help="Reviewer email for /auth/dev")
    parser.add_argument("--label-prefix", default="load-agent", help="Prefix for worker labels")
    parser.add_argument(
        "--task",
        default="review",
        choices=["review", "status", "audit"],
        help="Claim task type",
    )
    parser.add_argument("--jurisdiction", default="", help="Optional jurisdiction filter")
    parser.add_argument("--p90-limit", type=float, default=1.0, help="Max p90 latency in seconds")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request HTTP timeout in seconds")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print JSON report")
    ns = parser.parse_args(argv)
    if ns.agents < 1:
        parser.error("--agents must be >= 1")
    if ns.pages < 1:
        parser.error("--pages must be >= 1")
    if ns.p90_limit <= 0:
        parser.error("--p90-limit must be > 0")
    return LoadConfig(
        url=ns.url.rstrip("/"),
        agents=ns.agents,
        pages=ns.pages,
        include_images=ns.include_images,
        email=ns.email,
        label_prefix=ns.label_prefix,
        task=ns.task,
        jurisdiction=ns.jurisdiction,
        p90_limit_s=ns.p90_limit,
        timeout_s=ns.timeout,
        json_output=ns.json_output,
    )


def _timed(client: httpx.Client, method: str, path: str, latencies: list[float], **kwargs) -> httpx.Response:
    started = time.perf_counter()
    response = client.request(method, path, **kwargs)
    latencies.append(time.perf_counter() - started)
    return response


def _worker_run(
    index: int,
    config: LoadConfig,
    tracker: ActiveClaimTracker,
    barrier: threading.Barrier,
    client_factory: Callable[[], httpx.Client],
) -> LoadReport:
    label = f"{config.label_prefix}-{index:03d}"
    local = LoadReport(agents=1, p90_limit_s=config.p90_limit_s)
    work_id: str | None = None

    with client_factory() as client:
        try:
            login = _timed(client, "POST", "/auth/dev", local.latencies_s, json={"email": config.email})
            if login.status_code != 200:
                local.errors.append(f"{label}: /auth/dev returned {login.status_code}")
                barrier.wait()
                return local

            me = _timed(client, "GET", "/api/me", local.latencies_s)
            if me.status_code != 200:
                local.errors.append(f"{label}: /api/me returned {me.status_code}")
                barrier.wait()
                return local
            csrf = str(me.json().get("csrf", ""))
            write_headers = {"X-CSRF-Token": csrf}

            barrier.wait()

            claim_resp = _timed(
                client,
                "POST",
                "/api/queue/claim-next",
                local.latencies_s,
                headers=write_headers,
                json={
                    "label": label,
                    "task": config.task,
                    "jurisdiction": config.jurisdiction,
                    "kind": "",
                },
            )
            if claim_resp.status_code == 404:
                local.nothing_to_claim += 1
                return local
            if claim_resp.status_code != 200:
                local.errors.append(f"{label}: claim-next returned {claim_resp.status_code}")
                return local

            claim = claim_resp.json()
            work_id = str(claim["work_id"])
            token = str(claim["token"])
            documents = list(claim.get("documents") or [])
            local.claims_acquired += 1

            if not tracker.acquire(work_id, label):
                local.double_claims += 1

            claim_headers = {**write_headers, "X-Claim-Token": token}
            page_targets: list[tuple[str, int]] = []
            for doc in documents:
                act_id = str(doc["id"])
                # A document's pages keep the numbers of the file it came from (a law printed on pages
                # 9 to 12 of a gazette has no page 1), so ask the workbench which pages it has.
                numbers: list[int] = []
                listing = _timed(client, "GET", f"/api/documents/{act_id}", local.latencies_s)
                if listing.status_code == 200:
                    numbers = [int(p["page"]) for p in (listing.json().get("pages") or []) if "page" in p]
                if not numbers:  # a workbench that does not list them: count from 1
                    numbers = list(range(1, int(doc.get("pages") or 1) + 1))
                for page_num in numbers:
                    page_targets.append((act_id, page_num))
                    if len(page_targets) >= config.pages:
                        break
                if len(page_targets) >= config.pages:
                    break

            for act_id, page_num in page_targets:
                page_resp = _timed(
                    client,
                    "GET",
                    f"/api/documents/{act_id}/pages/{page_num}",
                    local.latencies_s,
                    headers={"X-Claim-Token": token},
                )
                if page_resp.status_code != 200:
                    local.errors.append(f"{label}: GET {act_id} p{page_num} returned {page_resp.status_code}")
                    continue
                local.pages_loaded += 1
                page_data = page_resp.json()

                if config.include_images:
                    img_resp = _timed(
                        client,
                        "GET",
                        f"/api/documents/{act_id}/pages/{page_num}/image",
                        local.latencies_s,
                        headers={"X-Claim-Token": token},
                    )
                    if img_resp.status_code != 200 or not img_resp.content:
                        local.errors.append(
                            f"{label}: GET image {act_id} p{page_num} returned {img_resp.status_code}"
                        )
                    else:
                        local.images_fetched += 1

                latest = page_data.get("latest")
                expected_rev = int(latest["revision"]) if latest else 0
                units = latest["units"] if latest else page_data.get("machine_units", [])
                resolutions = latest["resolutions"] if latest else {}

                save_resp = _timed(
                    client,
                    "POST",
                    f"/api/documents/{act_id}/pages/{page_num}",
                    local.latencies_s,
                    headers=claim_headers,
                    json={
                        "expected_revision": expected_rev,
                        "units": units,
                        "status": "checked",
                        "resolutions": resolutions,
                        "comment": f"claim-load {label}",
                    },
                )
                if save_resp.status_code != 200:
                    local.errors.append(
                        f"{label}: POST {act_id} p{page_num} returned {save_resp.status_code}"
                    )
                    continue
                new_rev = int(save_resp.json().get("revision", -1))
                local.pages_saved += 1

                verify_resp = _timed(
                    client,
                    "GET",
                    f"/api/documents/{act_id}/pages/{page_num}",
                    local.latencies_s,
                    headers={"X-Claim-Token": token},
                )
                if verify_resp.status_code != 200:
                    local.errors.append(
                        f"{label}: verify GET {act_id} p{page_num} returned {verify_resp.status_code}"
                    )
                    continue
                verified_latest = verify_resp.json().get("latest")
                if not verified_latest or int(verified_latest.get("revision", -1)) < new_rev:
                    local.lost_saves += 1

            renew_resp = _timed(
                client,
                "POST",
                f"/api/claims/{work_id}/renew",
                local.latencies_s,
                headers=claim_headers,
            )
            if renew_resp.status_code != 200:
                local.errors.append(f"{label}: renew {work_id} returned {renew_resp.status_code}")
            else:
                local.renewals += 1

            # Drop from local active set right before server release so a rapid subsequent claim
            # by another worker is not miscounted as an overlap.
            tracker.release(work_id, label)
            release_resp = _timed(
                client,
                "POST",
                f"/api/claims/{work_id}/release",
                local.latencies_s,
                headers=claim_headers,
                json={"outcome": "done", "reason": ""},
            )
            if release_resp.status_code != 200:
                local.errors.append(f"{label}: release {work_id} returned {release_resp.status_code}")
            else:
                local.releases += 1
        except Exception as exc:  # noqa: BLE001
            local.errors.append(f"{label}: {type(exc).__name__}: {exc}")
            if work_id is not None:
                tracker.release(work_id, label)
            try:
                barrier.abort()
            except Exception:  # noqa: BLE001
                pass

    return local


def run_load_test(
    config: LoadConfig,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> LoadReport:
    """Spawn config.agents concurrent workers and aggregate their metrics."""
    factory = client_factory or (
        lambda: httpx.Client(base_url=config.url, timeout=config.timeout_s, follow_redirects=False)
    )
    tracker = ActiveClaimTracker()
    barrier = threading.Barrier(config.agents)
    report = LoadReport(agents=config.agents, p90_limit_s=config.p90_limit_s)

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.agents) as pool:
        futures = [
            pool.submit(_worker_run, idx, config, tracker, barrier, factory)
            for idx in range(1, config.agents + 1)
        ]
        for future in concurrent.futures.as_completed(futures):
            part = future.result()
            report.claims_acquired += part.claims_acquired
            report.nothing_to_claim += part.nothing_to_claim
            report.pages_loaded += part.pages_loaded
            report.images_fetched += part.images_fetched
            report.pages_saved += part.pages_saved
            report.renewals += part.renewals
            report.releases += part.releases
            report.double_claims += part.double_claims
            report.lost_saves += part.lost_saves
            report.errors.extend(part.errors)
            report.latencies_s.extend(part.latencies_s)

    return report


def format_report(report: LoadReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    lines = [
        f"claim_load: {status}",
        f"  agents={report.agents} claims={report.claims_acquired} empty={report.nothing_to_claim}",
        f"  pages_loaded={report.pages_loaded} images={report.images_fetched} saved={report.pages_saved}",
        f"  renewals={report.renewals} releases={report.releases}",
        f"  double_claims={report.double_claims} lost_saves={report.lost_saves} errors={len(report.errors)}",
        (
            f"  requests={len(report.latencies_s)} "
            f"p50={report.p50_s:.3f}s p90={report.p90_s:.3f}s "
            f"p99={report.p99_s:.3f}s max={report.max_s:.3f}s (limit p90<{report.p90_limit_s:.3f}s)"
        ),
    ]
    for issue in report.failures():
        lines.append(f"  FAILURE: {issue}")
    for err in report.errors[:10]:
        lines.append(f"  ERROR: {err}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    report = run_load_test(config)
    if config.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
