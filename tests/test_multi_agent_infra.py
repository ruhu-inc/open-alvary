"""Tests for Phase 5 multi-agent infrastructure (Terraform) and claim_load harness."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import httpx
import pytest

from scripts import claim_load

ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_TF = ROOT / "deployment" / "gcp-terraform" / "env" / "workbench.tf"
RENDER_TF = ROOT / "deployment" / "gcp-terraform" / "env" / "render.tf"


def _extract_block(text: str, header_pattern: str) -> str:
    match = re.search(header_pattern, text)
    assert match is not None, f"Missing block matching {header_pattern!r}"
    start = match.end()
    depth = 1
    pos = start
    while pos < len(text) and depth > 0:
        ch = text[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"Unclosed block matching {header_pattern!r}"
    return text[match.start() : pos]


# --- Terraform invariants -------------------------------------------------------------------------


def _default(text: str, variable: str) -> str:
    block = _extract_block(text, rf'variable\s+"{variable}"\s*\{{')
    found = re.search(r'default\s*=\s*"?([^"\n]+?)"?\s*$', block, re.MULTILINE)
    assert found is not None, f"{variable} has no default"
    return found.group(1)


# The defaults are the sizes staging already runs, so an apply that sets none of them changes nothing
# and never restarts the review database by surprise. The larger sizes for many agents are settings.
def test_workbench_tf_database_tier_and_pitr() -> None:
    text = WORKBENCH_TF.read_text(encoding="utf-8")
    assert _default(text, "review_postgres_tier") == "db-f1-micro"
    assert "db-custom-1-3840" in text  # the tier for many agents is named where the sizes are explained

    db_block = _extract_block(text, r'resource\s+"google_sql_database_instance"\s+"review"\s*\{')
    assert "db-f1-micro" not in db_block
    assert re.search(r"tier\s*=\s*var\.review_postgres_tier\b", db_block)
    assert re.search(r"point_in_time_recovery_enabled\s*=\s*true\b", db_block)


def test_workbench_tf_scaling_resources_and_cache() -> None:
    text = WORKBENCH_TF.read_text(encoding="utf-8")
    assert _default(text, "workbench_min_instances") == "0"
    assert _default(text, "workbench_max_instances") == "2"
    assert _default(text, "workbench_cpu") == "1"
    assert _default(text, "workbench_memory") == "2Gi"
    assert _default(text, "workbench_cache_mb") == "512"
    assert re.search(r"ALVARY_REVIEW_CACHE_MB\s*=\s*tostring\(var\.workbench_cache_mb\)", text)

    svc_block = _extract_block(text, r'resource\s+"google_cloud_run_v2_service"\s+"workbench"\s*\{')
    scaling_block = _extract_block(svc_block, r"scaling\s*\{")
    assert re.search(r"min_instance_count\s*=\s*var\.workbench_min_instances\b", scaling_block)
    assert re.search(r"max_instance_count\s*=\s*var\.workbench_max_instances\b", scaling_block)

    resources_block = _extract_block(svc_block, r"resources\s*\{")
    assert re.search(r"cpu\s*=\s*var\.workbench_cpu\b", resources_block)
    assert re.search(r"memory\s*=\s*var\.workbench_memory\b", resources_block)


def test_render_tf_scaling_concurrency_and_isolation() -> None:
    text = RENDER_TF.read_text(encoding="utf-8")
    assert _default(text, "render_min_instances") == "0"
    assert _default(text, "render_max_instances") == "4"

    svc_block = _extract_block(text, r'resource\s+"google_cloud_run_v2_service"\s+"render"\s*\{')
    assert re.search(r'ingress\s*=\s*"INGRESS_TRAFFIC_INTERNAL_ONLY"', svc_block)
    assert re.search(r"max_instance_request_concurrency\s*=\s*1\b", svc_block)

    scaling_block = _extract_block(svc_block, r"scaling\s*\{")
    assert re.search(r"min_instance_count\s*=\s*var\.render_min_instances\b", scaling_block)
    assert re.search(r"max_instance_count\s*=\s*var\.render_max_instances\b", scaling_block)

    vpc_block = _extract_block(svc_block, r"vpc_access\s*\{")
    assert re.search(r'egress\s*=\s*"ALL_TRAFFIC"', vpc_block)

    # Network isolation invariants remain intact.
    assert re.search(r"delete_default_routes_on_create\s*=\s*true\b", text)
    assert re.search(r"private_ip_google_access\s*=\s*false\b", text)
    fw_block = _extract_block(text, r'resource\s+"google_compute_firewall"\s+"render_egress"\s*\{')
    assert re.search(r'direction\s*=\s*"EGRESS"', fw_block)
    assert re.search(r'protocol\s*=\s*"all"', fw_block)


# --- claim_load.py CLI and report logic -----------------------------------------------------------


def test_claim_load_parse_args_defaults_and_overrides() -> None:
    default_cfg = claim_load.parse_args([])
    assert default_cfg.url == "http://127.0.0.1:8000"
    assert default_cfg.agents == 10
    assert default_cfg.pages == 2
    assert default_cfg.include_images is True
    assert default_cfg.p90_limit_s == 1.0
    assert default_cfg.json_output is False
    assert default_cfg.allow_empty is False
    assert default_cfg.allow_mode is False
    assert default_cfg.target_is_scratch is False

    custom_cfg = claim_load.parse_args(
        [
            "--url",
            "http://127.0.0.1:9090/",
            "--agents",
            "25",
            "--pages",
            "4",
            "--no-include-images",
            "--p90-limit",
            "0.75",
            "--json",
            "--allow-empty",
            "--allow-mode",
            "--target-is-scratch",
        ]
    )
    assert custom_cfg.url == "http://127.0.0.1:9090"
    assert custom_cfg.agents == 25
    assert custom_cfg.pages == 4
    assert custom_cfg.include_images is False
    assert custom_cfg.p90_limit_s == 0.75
    assert custom_cfg.json_output is True
    assert custom_cfg.allow_empty is True
    assert custom_cfg.allow_mode is True
    assert custom_cfg.target_is_scratch is True


@pytest.mark.parametrize(
    "bad_argv",
    [
        ["--agents", "0"],
        ["--pages", "0"],
        ["--p90-limit", "0"],
    ],
)
def test_claim_load_parse_args_rejects_invalid_bounds(bad_argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        claim_load.parse_args(bad_argv)
    assert exc_info.value.code == 2


def test_claim_load_percentile_and_report_thresholds() -> None:
    assert claim_load.percentile([], 0.90) == 0.0
    assert claim_load.percentile([0.25], 0.90) == 0.25
    samples = [i / 100.0 for i in range(1, 101)]
    assert claim_load.percentile(samples, 0.50) == pytest.approx(0.50)
    assert claim_load.percentile(samples, 0.90) == pytest.approx(0.90)
    assert claim_load.percentile(samples, 0.99) == pytest.approx(0.99)

    ok = claim_load.LoadReport(
        agents=4,
        claims_acquired=4,
        pages_loaded=8,
        images_fetched=8,
        pages_saved=8,
        renewals=4,
        releases=4,
        latencies_s=[0.05, 0.10, 0.15, 0.20],
        p90_limit_s=1.0,
    )
    assert ok.passed is True
    assert ok.failures() == []
    assert "PASS" in claim_load.format_report(ok)
    assert ok.to_dict()["passed"] is True

    bad = claim_load.LoadReport(
        agents=4,
        claims_acquired=4,
        double_claims=1,
        lost_saves=2,
        misattributed_saves=1,
        unclaimed_saves_accepted=3,
        server_double_claims=["work-007"],
        errors=["boom"],
        latencies_s=[0.2, 1.5],
        p90_limit_s=1.0,
    )
    assert bad.passed is False
    failures = bad.failures()
    assert any("double_claims=1" in f for f in failures)
    assert any("server_double_claims=1" in f and "work-007" in f for f in failures)
    assert any("lost_saves=2" in f for f in failures)
    assert any("misattributed_saves=1" in f for f in failures)
    assert any("unclaimed_saves_accepted=3" in f for f in failures)
    assert any("errors=1" in f for f in failures)
    assert any("p90=" in f for f in failures)
    assert "FAIL" in claim_load.format_report(bad)


def test_claim_load_report_fails_when_agents_get_no_work_unless_allowed() -> None:
    idle = claim_load.LoadReport(agents=150, claims_acquired=50, nothing_to_claim=100)
    assert idle.passed is False
    assert idle.agents_without_work == 100
    assert any("agents_without_work=100" in f for f in idle.failures())
    assert any("nothing_to_claim=100" in f for f in idle.failures())
    assert "agents_without_work=100" in claim_load.format_report(idle)

    allowed = claim_load.LoadReport(agents=150, claims_acquired=50, nothing_to_claim=100, allow_empty=True)
    assert allowed.passed is True
    assert allowed.to_dict()["agents_without_work"] == 100


@pytest.mark.parametrize(
    ("url", "loopback"),
    [
        ("http://127.0.0.1:8000", True),
        ("http://127.3.4.5", True),
        ("http://[::1]:8000", True),
        ("http://localhost:8000", True),
        ("https://review.example.org", False),
        ("http://10.0.0.5:8000", False),
        ("http://192.168.1.2", False),
    ],
)
def test_claim_load_loopback_detection(url: str, loopback: bool) -> None:
    assert claim_load.is_loopback_url(url) is loopback


# --- claim_load.py concurrent harness behavior ----------------------------------------------------

LOCAL = "http://127.0.0.1:8000"
EMAIL = "load-agent@example.org"


class _FakeWorkbench:
    """Thread-safe in-memory fake workbench for exercising claim_load end to end. Claims are enforced
    unless ``claims_mode`` says otherwise: a save needs a token the fake gave for that work."""

    def __init__(
        self,
        work_count: int,
        pages_per_doc: int = 3,
        *,
        force_double_claim: bool = False,
        drop_saves: bool = False,
        overlap_barrier: threading.Barrier | None = None,
        claims_mode: str | None = "enforce",
        accept_unclaimed: bool = False,
        author_override: str | None = None,
        show_label: bool = True,
        claims_listing: str = "admin",
    ) -> None:
        self._lock = threading.Lock()
        self._available = [f"work-{i:03d}" for i in range(1, work_count + 1)]
        self._pages_per_doc = pages_per_doc
        self._force_double_claim = force_double_claim
        self._drop_saves = drop_saves
        self._overlap_barrier = overlap_barrier
        self._claims_mode = claims_mode
        self._accept_unclaimed = accept_unclaimed
        self._author_override = author_override
        self._show_label = show_label
        self._claims_listing = claims_listing
        self._latest: dict[tuple[str, int], dict] = {}
        self._tokens: dict[str, dict[str, str]] = {}  # work -> token -> label
        self.statuses: list[str] = []
        self.outcomes: list[str] = []
        self.claims_reads = 0

    @staticmethod
    def _work_of(act_id: str) -> str:
        return act_id.rsplit("-", 1)[0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "POST" and path == "/auth/dev":
            return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": "oa_review_session=tok"})

        if method == "GET" and path == "/api/me":
            me = {"signed_in": True, "csrf": "csrf-secret", "email": EMAIL}
            if self._claims_mode is not None:
                me["claims"] = self._claims_mode
            return httpx.Response(200, json=me)

        if method == "GET" and path == "/api/claims":
            if self._claims_listing == "forbidden":
                return httpx.Response(403, json={"message": "This needs the admin role"})
            with self._lock:
                self.claims_reads += 1
                listed = [
                    {"work_id": work, "label": label}
                    for work, tokens in sorted(self._tokens.items())
                    for label in tokens.values()
                ]
            if self._claims_listing == "twice" and listed:
                listed.append(dict(listed[0]))
            return httpx.Response(200, json={"claims": listed})

        if method == "POST" and path == "/api/queue/claim-next":
            assert request.headers.get("X-CSRF-Token") == "csrf-secret"
            body = json.loads(request.content.decode())
            label = body["label"]
            with self._lock:
                if self._force_double_claim:
                    work_id = "work-shared"
                elif not self._available:
                    return httpx.Response(404, json={"message": "Nothing to claim under these limits."})
                else:
                    work_id = self._available.pop(0)
                token = f"tok-{label}-{work_id}"
                self._tokens.setdefault(work_id, {})[token] = label
            return httpx.Response(
                200,
                json={
                    "work_id": work_id,
                    "task": body.get("task", "review"),
                    "token": token,
                    "expires_at": "2026-11-20T14:32:00Z",
                    "documents": [{"id": f"{work_id}-en", "pages": self._pages_per_doc}],
                },
            )

        page_match = re.fullmatch(r"/api/documents/([^/]+)/pages/(\d+)", path)
        if page_match and method == "GET":
            if self._overlap_barrier is not None:
                self._overlap_barrier.wait()
            act_id, page_num = page_match.group(1), int(page_match.group(2))
            with self._lock:
                latest = self._latest.get((act_id, page_num))
                latest = dict(latest) if latest else None
            if latest and not self._show_label:
                latest.pop("label", None)
            return httpx.Response(
                200,
                json={
                    "page": page_num,
                    "machine_units": [{"kind": "paragraph", "text": "ok"}],
                    "latest": latest,
                },
            )

        img_match = re.fullmatch(r"/api/documents/([^/]+)/pages/(\d+)/image", path)
        if img_match and method == "GET":
            return httpx.Response(
                200, content=b"\xff\xd8\xff\xe0fake-jpeg", headers={"Content-Type": "image/jpeg"}
            )

        if page_match and method == "POST":
            assert request.headers.get("X-CSRF-Token") == "csrf-secret"
            act_id, page_num = page_match.group(1), int(page_match.group(2))
            body = json.loads(request.content.decode())
            token = request.headers.get("X-Claim-Token") or ""
            with self._lock:
                label = self._tokens.get(self._work_of(act_id), {}).get(token)
                if label is None and not self._accept_unclaimed:
                    return httpx.Response(
                        409, json={"code": "claim", "message": "This work is claimed by another reviewer."}
                    )
                self.statuses.append(body["status"])
                new_rev = (self._latest.get((act_id, page_num)) or {}).get("revision", 0) + 1
                if not self._drop_saves:
                    self._latest[(act_id, page_num)] = {
                        "revision": new_rev,
                        "units": body["units"],
                        "status": body["status"],
                        "resolutions": {},
                        "author": self._author_override or EMAIL,
                        "label": label or "unlabelled",
                    }
            return httpx.Response(200, json={"revision": new_rev})

        if re.fullmatch(r"/api/claims/[^/]+/renew", path) and method == "POST":
            assert request.headers.get("X-CSRF-Token") == "csrf-secret"
            assert request.headers.get("X-Claim-Token")
            return httpx.Response(200, json={"ok": True})

        release = re.fullmatch(r"/api/claims/([^/]+)/release", path)
        if release and method == "POST":
            assert request.headers.get("X-CSRF-Token") == "csrf-secret"
            token = request.headers.get("X-Claim-Token")
            assert token
            body = json.loads(request.content.decode())
            with self._lock:
                self.outcomes.append(body["outcome"])
                self._tokens.get(release.group(1), {}).pop(token, None)
            return httpx.Response(200, json={"ok": True})

        return httpx.Response(404, json={"message": f"Unhandled {method} {path}"})


def _run(fake: _FakeWorkbench, **settings) -> claim_load.LoadReport:
    config = claim_load.LoadConfig(**{"url": LOCAL, **settings})
    return claim_load.run_load_test(
        config,
        client_factory=lambda: httpx.Client(base_url=config.url, transport=httpx.MockTransport(fake.handler)),
    )


def test_claim_load_concurrent_happy_path_with_images() -> None:
    fake = _FakeWorkbench(work_count=6, pages_per_doc=3)
    report = _run(fake, agents=6, pages=2, include_images=True)
    assert report.passed is True, report.failures()
    assert report.claims_mode == "enforce"
    assert report.mode_overridden is False
    assert report.claims_acquired == 6
    assert report.nothing_to_claim == 0
    assert report.agents_without_work == 0
    assert report.pages_loaded == 12
    assert report.images_fetched == 12
    assert report.pages_saved == 12
    assert report.renewals == 6
    assert report.releases == 6
    assert report.double_claims == 0
    assert report.lost_saves == 0
    assert report.misattributed_saves == 0
    assert report.labels_unverified == 0
    # One save with a wrong token and one with none per worker, each refused.
    assert report.refusals_checked == 12
    assert report.unclaimed_saves_accepted == 0
    # Read while all six held their claims; nothing listed twice.
    assert fake.claims_reads == 1
    assert report.server_claims == "read (6 live claims)"
    assert report.server_double_claims == []
    assert report.p90_s < 1.0
    # Never marks a page checked or a work done.
    assert set(fake.statuses) == {"edited"}
    assert fake.outcomes == ["abandoned"] * 6


def test_claim_load_queue_exhaustion_fails_unless_allowed() -> None:
    report = _run(_FakeWorkbench(work_count=2, pages_per_doc=2), agents=4, pages=2, include_images=False)
    assert report.passed is False
    assert report.claims_acquired == 2
    assert report.nothing_to_claim == 2
    assert report.agents_without_work == 2
    assert report.pages_saved == 4
    assert "agents_without_work=2" in claim_load.format_report(report)

    allowed = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=2),
        agents=4,
        pages=2,
        include_images=False,
        allow_empty=True,
    )
    assert allowed.passed is True, allowed.failures()
    assert allowed.images_fetched == 0
    assert allowed.agents_without_work == 2


def test_claim_load_all_claims_404_fails() -> None:
    report = _run(_FakeWorkbench(work_count=0), agents=3, pages=1, include_images=False)
    assert report.claims_acquired == 0
    assert report.nothing_to_claim == 3
    assert report.passed is False
    text = claim_load.format_report(report)
    assert "FAIL" in text
    assert "agents_without_work=3" in text


def test_claim_load_detects_double_claim() -> None:
    # Barrier on the first page GET guarantees all 4 workers hold the shared claim simultaneously.
    fake = _FakeWorkbench(
        work_count=4,
        pages_per_doc=1,
        force_double_claim=True,
        overlap_barrier=threading.Barrier(4),
    )
    report = _run(fake, agents=4, pages=1, include_images=False)
    assert report.double_claims >= 1
    assert report.passed is False


def test_claim_load_reports_works_the_server_lists_twice() -> None:
    report = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1, claims_listing="twice"),
        agents=2,
        pages=1,
        include_images=False,
    )
    assert report.server_double_claims == ["work-001"]
    assert report.passed is False
    assert any("server_double_claims=1" in f for f in report.failures())


def test_claim_load_skips_claims_listing_quietly_without_admin() -> None:
    report = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1, claims_listing="forbidden"),
        agents=2,
        pages=1,
        include_images=False,
    )
    assert report.passed is True, report.failures()
    assert report.server_claims.startswith("skipped")
    assert report.errors == []


def test_claim_load_detects_lost_save() -> None:
    report = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1, drop_saves=True),
        agents=2,
        pages=1,
        include_images=False,
    )
    assert report.lost_saves == 2
    assert report.passed is False


def test_claim_load_detects_save_by_another_account() -> None:
    report = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1, author_override="someone@example.org"),
        agents=2,
        pages=1,
        include_images=False,
    )
    assert report.misattributed_saves == 2
    assert report.passed is False


def test_claim_load_notes_when_the_label_cannot_be_checked() -> None:
    report = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1, show_label=False),
        agents=2,
        pages=1,
        include_images=False,
    )
    assert report.passed is True, report.failures()
    assert report.labels_unverified == 2
    assert any("labels_unverified=2" in w for w in report.warnings())


def test_claim_load_fails_when_unclaimed_saves_are_accepted() -> None:
    report = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1, accept_unclaimed=True),
        agents=2,
        pages=1,
        include_images=False,
    )
    assert report.refusals_checked == 4
    assert report.unclaimed_saves_accepted == 4
    assert report.passed is False


def test_claim_load_refusal_needs_a_code() -> None:
    def answer(status: int, body: dict) -> httpx.Response:
        return httpx.Response(status, json=body)

    assert claim_load._claim_refusal(answer(409, {"code": "claim", "message": "held"}))
    assert claim_load._claim_refusal(answer(403, {"code": "blocked_label", "message": "blocked"}))
    assert claim_load._claim_refusal(answer(409, {"detail": {"code": "claim", "message": "held"}}))
    # Someone saved first: a 409 with no code is not a claim refusal.
    assert not claim_load._claim_refusal(answer(409, {"message": "This page was saved by someone else"}))
    assert not claim_load._claim_refusal(answer(200, {"revision": 3}))
    assert not claim_load._claim_refusal(answer(422, {"code": "x"}))


@pytest.mark.parametrize("mode", ["off", "warn", None])
def test_claim_load_refuses_a_target_without_enforced_claims(mode: str | None) -> None:
    fake = _FakeWorkbench(work_count=2, pages_per_doc=1, claims_mode=mode)
    report = _run(fake, agents=2, pages=1, include_images=False)
    assert report.passed is False
    assert "not 'enforce'" in report.refused
    assert report.claims_acquired == 0
    assert fake.statuses == []
    assert "REFUSED" in claim_load.format_report(report)


def test_claim_load_allow_mode_runs_and_says_so_loudly() -> None:
    report = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1, claims_mode="off"),
        agents=2,
        pages=1,
        include_images=False,
        allow_mode=True,
    )
    assert report.passed is True, report.failures()
    assert report.mode_overridden is True
    assert report.to_dict()["mode_overridden"] is True
    text = claim_load.format_report(report)
    assert "CLAIMS ARE NOT ENFORCED" in text
    assert "OVERRIDDEN" in text


def test_claim_load_refuses_a_remote_target_unless_scratch() -> None:
    fake = _FakeWorkbench(work_count=2, pages_per_doc=1)
    report = _run(fake, url="https://review.example.org", agents=2, pages=1, include_images=False)
    assert report.passed is False
    assert "not on this machine" in report.refused
    assert fake.statuses == []

    scratch = _run(
        _FakeWorkbench(work_count=2, pages_per_doc=1),
        url="https://scratch.example.org",
        agents=2,
        pages=1,
        include_images=False,
        target_is_scratch=True,
    )
    assert scratch.passed is True, scratch.failures()


def test_claim_load_main_exits_non_zero_when_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert claim_load.main(["--url", "https://review.example.org"]) == 1
    assert "REFUSED" in capsys.readouterr().out
