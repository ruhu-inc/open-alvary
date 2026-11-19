"""Load-test harness for concurrent multi-agent workbench claims and saves.

Run it only against a scratch copy of the workbench: it saves pages (as "edited", their units
unchanged) and releases every work it claims as "abandoned". It refuses a target that is not on
this machine (a loopback address) unless --target-is-scratch says the target is a scratch copy,
and a target whose claims are not enforced (GET /api/me "claims") unless --allow-mode is given.

Spawns configurable concurrent agent workers against a workbench instance, exercising:
  1. POST /api/queue/claim-next
  2. GET  /api/documents/{act_id}/pages/{page}
  3. GET  /api/documents/{act_id}/pages/{page}/image (when --include-images)
  4. POST /api/documents/{act_id}/pages/{page} (status "edited"), then a readback of the page's latest
     revision: the save is there, by the signed-in account, under the worker's label
  5. once per worker, a save with a wrong claim token and a save with none, both of which must be
     refused as claim refusals (403 or 409 with a "code")
  6. POST /api/claims/{work_id}/renew
  7. POST /api/claims/{work_id}/release (outcome "abandoned")
While every worker holds its claim, GET /api/claims (admin only; skipped on 403) is read once and
any work listed twice is reported.

Passes when:
  - every agent claimed a work and no claim-next answered 404 (unless --allow-empty)
  - 0 double-claims (no work held by two workers at once, seen by the workers or by the server)
  - 0 lost saves (every committed revision is visible on readback)
  - 0 saves attributed to another account or label
  - 0 unclaimed saves accepted (wrong token or no token)
  - 0 unexpected HTTP/runtime errors
  - p90 request latency < --p90-limit (default 1.0s)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import math
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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
    allow_empty: bool = False
    allow_mode: bool = False
    target_is_scratch: bool = False


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
    misattributed_saves: int = 0
    labels_unverified: int = 0
    refusals_checked: int = 0
    unclaimed_saves_accepted: int = 0
    errors: list[str] = field(default_factory=list)
    latencies_s: list[float] = field(default_factory=list)
    p90_limit_s: float = 1.0
    allow_empty: bool = False
    claims_mode: str = ""
    mode_overridden: bool = False
    refused: str = ""
    server_claims: str = "not read"
    server_double_claims: list[str] = field(default_factory=list)

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

    @property
    def agents_without_work(self) -> int:
        return max(0, self.agents - self.claims_acquired)

    def failures(self) -> list[str]:
        issues: list[str] = []
        if self.refused:
            issues.append(f"refused to run: {self.refused}")
            return issues
        if not self.allow_empty:
            if self.agents_without_work > 0:
                issues.append(
                    f"agents_without_work={self.agents_without_work} "
                    f"(claimed {self.claims_acquired} of {self.agents}; expected every agent to get a work)"
                )
            if self.nothing_to_claim > 0:
                issues.append(f"nothing_to_claim={self.nothing_to_claim} (claim-next answered 404)")
        if self.double_claims > 0:
            issues.append(f"double_claims={self.double_claims} (expected 0)")
        if self.server_double_claims:
            issues.append(
                f"server_double_claims={len(self.server_double_claims)} "
                f"(works listed twice by /api/claims: {', '.join(self.server_double_claims[:5])})"
            )
        if self.lost_saves > 0:
            issues.append(f"lost_saves={self.lost_saves} (expected 0)")
        if self.misattributed_saves > 0:
            issues.append(f"misattributed_saves={self.misattributed_saves} (expected 0)")
        if self.unclaimed_saves_accepted > 0:
            issues.append(
                f"unclaimed_saves_accepted={self.unclaimed_saves_accepted} "
                "(saves with a wrong or no claim token must be refused)"
            )
        if self.errors:
            issues.append(f"errors={len(self.errors)} (expected 0)")
        if self.latencies_s and self.p90_s >= self.p90_limit_s:
            issues.append(f"p90={self.p90_s:.3f}s >= {self.p90_limit_s:.3f}s")
        return issues

    def warnings(self) -> list[str]:
        notes: list[str] = []
        if self.mode_overridden:
            notes.append(
                f"CLAIMS ARE NOT ENFORCED ON THE TARGET (mode {self.claims_mode or 'not reported'!r}), "
                "run with --allow-mode: the claim checks in this result prove nothing"
            )
        if self.labels_unverified:
            notes.append(
                f"labels_unverified={self.labels_unverified}: the page's latest revision did not say "
                "its label, so only its author was checked"
            )
        return notes

    @property
    def passed(self) -> bool:
        return len(self.failures()) == 0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "refused": self.refused,
            "claims_mode": self.claims_mode,
            "mode_overridden": self.mode_overridden,
            "agents": self.agents,
            "claims_acquired": self.claims_acquired,
            "agents_without_work": self.agents_without_work,
            "nothing_to_claim": self.nothing_to_claim,
            "allow_empty": self.allow_empty,
            "pages_loaded": self.pages_loaded,
            "images_fetched": self.images_fetched,
            "pages_saved": self.pages_saved,
            "renewals": self.renewals,
            "releases": self.releases,
            "double_claims": self.double_claims,
            "server_claims": self.server_claims,
            "server_double_claims": list(self.server_double_claims),
            "lost_saves": self.lost_saves,
            "misattributed_saves": self.misattributed_saves,
            "labels_unverified": self.labels_unverified,
            "refusals_checked": self.refusals_checked,
            "unclaimed_saves_accepted": self.unclaimed_saves_accepted,
            "requests": len(self.latencies_s),
            "p50_s": round(self.p50_s, 4),
            "p90_s": round(self.p90_s, 4),
            "p99_s": round(self.p99_s, 4),
            "max_s": round(self.max_s, 4),
            "p90_limit_s": self.p90_limit_s,
            "errors": list(self.errors),
            "failures": self.failures(),
            "warnings": self.warnings(),
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


def is_loopback_url(url: str) -> bool:
    """True when ``url`` names this machine: localhost or a loopback address."""
    host = (urlsplit(url).hostname or "").strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_args(argv: Sequence[str] | None = None) -> LoadConfig:
    parser = argparse.ArgumentParser(
        description="Run concurrent multi-agent claim/load/save/renew/release load tests "
        "against a scratch copy of the workbench."
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
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Pass even when some agents get no work (claim-next answers 404)",
    )
    parser.add_argument(
        "--allow-mode",
        action="store_true",
        help="Run even when the target does not enforce claims (the result says so loudly)",
    )
    parser.add_argument(
        "--target-is-scratch",
        action="store_true",
        help="The target is a scratch copy that may be written to, though it is not on this machine",
    )
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
        allow_empty=ns.allow_empty,
        allow_mode=ns.allow_mode,
        target_is_scratch=ns.target_is_scratch,
    )


def _timed(client: httpx.Client, method: str, path: str, latencies: list[float], **kwargs) -> httpx.Response:
    started = time.perf_counter()
    response = client.request(method, path, **kwargs)
    latencies.append(time.perf_counter() - started)
    return response


def _claim_refusal(response: httpx.Response) -> bool:
    """A claim refusal: 403 or 409 whose JSON carries a "code" (a save that met a newer revision is a
    409 with no code)."""
    if response.status_code not in (403, 409):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    detail = body.get("detail")
    return bool(body.get("code")) or (isinstance(detail, dict) and bool(detail.get("code")))


def _sign_in(client: httpx.Client, config: LoadConfig, latencies: list[float]) -> dict:
    """Sign in through /auth/dev and return /api/me's answer; raises RuntimeError with the reason."""
    login = _timed(client, "POST", "/auth/dev", latencies, json={"email": config.email})
    if login.status_code != 200:
        raise RuntimeError(f"/auth/dev returned {login.status_code}")
    me = _timed(client, "GET", "/api/me", latencies)
    if me.status_code != 200:
        raise RuntimeError(f"/api/me returned {me.status_code}")
    return me.json()


class _Gate:
    """A barrier a worker passes at most once, so an early return can still let the others on."""

    def __init__(self, barrier: threading.Barrier, timeout_s: float) -> None:
        self._barrier = barrier
        self._timeout_s = timeout_s
        self._passed = False

    def wait(self) -> None:
        if not self._passed:
            self._passed = True
            self._barrier.wait(timeout=self._timeout_s)

    def abort(self) -> None:
        try:
            self._barrier.abort()
        except Exception:  # noqa: BLE001
            pass


def _worker_run(
    index: int,
    config: LoadConfig,
    tracker: ActiveClaimTracker,
    barrier: threading.Barrier,
    client_factory: Callable[[], httpx.Client],
    claimed_barrier: threading.Barrier | None = None,
) -> LoadReport:
    label = f"{config.label_prefix}-{index:03d}"
    local = LoadReport(agents=1, p90_limit_s=config.p90_limit_s)
    work_id: str | None = None
    gate_timeout = max(config.timeout_s * 4, 60.0)
    start = _Gate(barrier, gate_timeout)
    claimed = _Gate(claimed_barrier, gate_timeout) if claimed_barrier is not None else None

    def pass_claimed() -> None:
        if claimed is not None:
            claimed.wait()

    with client_factory() as client:
        try:
            try:
                me = _sign_in(client, config, local.latencies_s)
            except RuntimeError as error:
                local.errors.append(f"{label}: {error}")
                start.wait()
                pass_claimed()
                return local
            csrf = str(me.get("csrf", ""))
            account = str(me.get("email") or config.email).strip().lower()
            write_headers = {"X-CSRF-Token": csrf}

            start.wait()

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
                pass_claimed()
                return local
            if claim_resp.status_code != 200:
                local.errors.append(f"{label}: claim-next returned {claim_resp.status_code}")
                pass_claimed()
                return local

            claim = claim_resp.json()
            work_id = str(claim["work_id"])
            token = str(claim["token"])
            documents = list(claim.get("documents") or [])
            local.claims_acquired += 1

            if not tracker.acquire(work_id, label):
                local.double_claims += 1

            # Every worker holds its claim (or has none to hold) here: the server's list is read now.
            pass_claimed()

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

            refusals_tried = False
            for act_id, page_num in page_targets:
                page_path = f"/api/documents/{act_id}/pages/{page_num}"
                page_resp = _timed(
                    client, "GET", page_path, local.latencies_s, headers={"X-Claim-Token": token}
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
                        f"{page_path}/image",
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

                def save_body(
                    revision: int, note: str, units: list = units, resolutions: dict = resolutions
                ) -> dict:
                    # "edited", never "checked": the words were not compared with the page.
                    return {
                        "expected_revision": revision,
                        "units": units,
                        "status": "edited",
                        "resolutions": resolutions,
                        "comment": f"claim-load {label}{note}",
                    }

                save_resp = _timed(
                    client,
                    "POST",
                    page_path,
                    local.latencies_s,
                    headers=claim_headers,
                    json=save_body(expected_rev, ""),
                )
                if save_resp.status_code != 200:
                    local.errors.append(
                        f"{label}: POST {act_id} p{page_num} returned {save_resp.status_code}"
                    )
                    continue
                new_rev = int(save_resp.json().get("revision", -1))
                local.pages_saved += 1

                verify_resp = _timed(
                    client, "GET", page_path, local.latencies_s, headers={"X-Claim-Token": token}
                )
                if verify_resp.status_code != 200:
                    local.errors.append(
                        f"{label}: verify GET {act_id} p{page_num} returned {verify_resp.status_code}"
                    )
                    continue
                verified = verify_resp.json().get("latest")
                seen_rev = int(verified.get("revision", -1)) if verified else -1
                if seen_rev < new_rev:
                    local.lost_saves += 1
                    continue
                author = str(verified.get("author") or "").strip().lower()
                if seen_rev != new_rev or author != account:
                    local.misattributed_saves += 1
                    local.errors.append(
                        f"{label}: {act_id} p{page_num} latest is revision {seen_rev} by {author or '?'}, "
                        f"expected {new_rev} by {account}"
                    )
                elif "label" not in verified:
                    local.labels_unverified += 1
                elif verified.get("label") != label:
                    local.misattributed_saves += 1
                    local.errors.append(
                        f"{label}: {act_id} p{page_num} revision {seen_rev} is labelled {verified.get('label')!r}"
                    )

                if not refusals_tried:
                    refusals_tried = True
                    for name, headers in (
                        ("wrong claim token", {**write_headers, "X-Claim-Token": f"wrong-{token}"}),
                        ("no claim token", dict(write_headers)),
                    ):
                        bad = _timed(
                            client,
                            "POST",
                            page_path,
                            local.latencies_s,
                            headers=headers,
                            json=save_body(new_rev, f" ({name}: must be refused)"),
                        )
                        local.refusals_checked += 1
                        if not _claim_refusal(bad):
                            local.unclaimed_saves_accepted += 1
                            local.errors.append(
                                f"{label}: a save with {name} on {act_id} p{page_num} got "
                                f"{bad.status_code}, not a claim refusal (403 or 409 with a code)"
                            )

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
                # "abandoned", never "done": a load test reviews nothing and must draw no audits.
                json={"outcome": "abandoned", "reason": "claim_load test run"},
            )
            if release_resp.status_code != 200:
                local.errors.append(f"{label}: release {work_id} returned {release_resp.status_code}")
            else:
                local.releases += 1
        except Exception as exc:  # noqa: BLE001
            local.errors.append(f"{label}: {type(exc).__name__}: {exc}")
            if work_id is not None:
                tracker.release(work_id, label)
            start.abort()
            if claimed is not None:
                claimed.abort()

    return local


def _read_server_claims(client: httpx.Client, report: LoadReport) -> None:
    """Read GET /api/claims once and note any work listed twice; skipped quietly when not admin."""
    try:
        response = _timed(client, "GET", "/api/claims", report.latencies_s)
    except Exception as exc:  # noqa: BLE001
        report.server_claims = "not read"
        report.errors.append(f"/api/claims: {type(exc).__name__}: {exc}")
        return
    if response.status_code in (401, 403):
        report.server_claims = "skipped (the account is not an admin)"
        return
    if response.status_code != 200:
        report.server_claims = f"not read ({response.status_code})"
        report.errors.append(f"/api/claims returned {response.status_code}")
        return
    listed = Counter(str(item.get("work_id")) for item in (response.json().get("claims") or []))
    report.server_claims = f"read ({sum(listed.values())} live claims)"
    report.server_double_claims = sorted(work for work, count in listed.items() if count > 1)


def run_load_test(
    config: LoadConfig,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> LoadReport:
    """Check the target, spawn config.agents concurrent workers and aggregate their metrics."""
    factory = client_factory or (
        lambda: httpx.Client(base_url=config.url, timeout=config.timeout_s, follow_redirects=False)
    )
    report = LoadReport(agents=config.agents, p90_limit_s=config.p90_limit_s, allow_empty=config.allow_empty)

    if not config.target_is_scratch and not is_loopback_url(config.url):
        report.refused = (
            f"{config.url} is not on this machine; pass --target-is-scratch only if it is a scratch copy "
            "that may be written to"
        )
        return report

    with factory() as control:
        try:
            me = _sign_in(control, config, report.latencies_s)
        except RuntimeError as error:
            report.refused = f"could not sign in to read the claims mode: {error}"
            return report
        report.claims_mode = str(me.get("claims") or "")
        if report.claims_mode != "enforce":
            if not config.allow_mode:
                report.refused = (
                    f"claims on the target are {report.claims_mode or 'not reported'!r}, not 'enforce'; "
                    "pass --allow-mode to run anyway"
                )
                return report
            report.mode_overridden = True

        tracker = ActiveClaimTracker()
        barrier = threading.Barrier(config.agents)
        claimed_barrier = threading.Barrier(
            config.agents, action=lambda: _read_server_claims(control, report)
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=config.agents) as pool:
            futures = [
                pool.submit(_worker_run, idx, config, tracker, barrier, factory, claimed_barrier)
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
                report.misattributed_saves += part.misattributed_saves
                report.labels_unverified += part.labels_unverified
                report.refusals_checked += part.refusals_checked
                report.unclaimed_saves_accepted += part.unclaimed_saves_accepted
                report.errors.extend(part.errors)
                report.latencies_s.extend(part.latencies_s)

    return report


def format_report(report: LoadReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    lines = [f"claim_load: {status}"]
    for note in report.warnings():
        lines.append(f"  WARNING: {note}")
    if report.refused:
        lines.append(f"  REFUSED: {report.refused}")
        return "\n".join(lines)
    lines += [
        f"  claims_mode={report.claims_mode or '?'}"
        + (" (OVERRIDDEN with --allow-mode)" if report.mode_overridden else ""),
        f"  agents={report.agents} claims={report.claims_acquired} empty={report.nothing_to_claim} "
        f"agents_without_work={report.agents_without_work}",
        f"  pages_loaded={report.pages_loaded} images={report.images_fetched} saved={report.pages_saved}",
        f"  renewals={report.renewals} releases={report.releases}",
        f"  double_claims={report.double_claims} server_claims={report.server_claims} "
        f"server_double_claims={len(report.server_double_claims)}",
        f"  lost_saves={report.lost_saves} misattributed_saves={report.misattributed_saves} "
        f"labels_unverified={report.labels_unverified}",
        f"  refusals_checked={report.refusals_checked} unclaimed_saves_accepted={report.unclaimed_saves_accepted} "
        f"errors={len(report.errors)}",
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
