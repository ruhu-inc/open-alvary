"""WorkOS AuthKit confirmation checks (Measurement 1, MAC-16).

    python -m trials.idp_check.workos [--public-url https://...]
        [--output private-staging/idp-check/workos-results.md]

Reads WORKOS_API_KEY, WORKOS_CLIENT_ID, and WORKOS_AUTHKIT_DOMAIN from the git-ignored .env
(or environment variables with the WORKOS_ / AUTH0_ / IDP_CHECK_ prefixes) and never prints
credentials or tokens. Separates automated API checks from scripted manual browser checks and
writes a pass/fail table to private-staging/idp-check/workos-results.md, preserving Auth0's
recorded evidence in private-staging/idp-check/results.md.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "private-staging" / "idp-check" / "workos-results.md"
DEFAULT_TOKENS = ROOT / "private-staging" / "idp-check" / "workos-tokens.json"
WORKOS_API_BASE = "https://api.workos.com"
ENV_PREFIXES = ("WORKOS_", "AUTH0_", "IDP_CHECK_")
SECRET_KEY_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD")

CHATGPT_CIMD = "https://chatgpt.com/oauth/client.json"
CLAUDE_CIMD = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CODE_CIMD = "https://claude.ai/oauth/claude-code-client-metadata"
CODEX_CIMD = "https://chatgpt.com/oauth/codex/client.json"
REVIEWED_CIMDS = (CHATGPT_CIMD, CLAUDE_CIMD, CLAUDE_CODE_CIMD, CODEX_CIMD)

LATENCY_THRESHOLD_MS = 2000.0
DEFAULT_LATENCY_RUNS = 10

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/]+=*", re.IGNORECASE)
_TABLE_HEADER = "| Check | Mode (Auto/Manual) | Result | Evidence / Notes |"


class WorkOSCheckError(RuntimeError):
    """Raised when configuration or a fatal harness precondition fails."""


@dataclass(frozen=True)
class CheckResult:
    check: str
    mode: str  # "Auto" or "Manual"
    result: str  # "Pass", "Fail", or "Pending"
    evidence: str


@dataclass(frozen=True)
class ManualCheckSpec:
    slug: str
    check: str
    steps: tuple[str, ...]
    pass_criteria: str


MANUAL_CHECKS: tuple[ManualCheckSpec, ...] = (
    ManualCheckSpec(
        slug="chatgpt-cimd",
        check="ChatGPT CIMD (private_key_jwt + jwks_uri)",
        steps=(
            "Add {public_url}/mcp as a custom MCP server in ChatGPT (Settings -> Integrations / Apps -> Developer mode).",
            "Confirm WorkOS AuthKit accepts client_id {chatgpt_cimd} on the free tier and completes private_key_jwt + jwks_uri token exchange.",
            "Approve access on the consent / organisation picker screen, run a read-only MCP tool call, verify token audience equals {public_url}/mcp, test refresh, and revoke the grant to confirm HTTP 401.",
        ),
        pass_criteria="Live ChatGPT sign-in, private_key_jwt + jwks_uri code exchange, tool call, refresh, and revocation all succeed on WorkOS without Enterprise-only gating.",
    ),
    ManualCheckSpec(
        slug="claude-codex-loopback",
        check="Claude / Claude Code / Codex OAuth + loopback callbacks",
        steps=(
            "Connect Claude ({claude_cimd}) against {public_url}/mcp via OAuth 2.1 + PKCE S256.",
            "Connect Claude Code ({claude_code_cimd}) without pinning a callback port; verify port-agnostic loopback matching (127.0.0.1 / localhost) and paste fallback.",
            "Connect Codex ({codex_cimd}) with a random loopback port; verify corpus:read scope delivery, resource binding (MCP token refused on /api/v1 and reverse), refresh, and grant revocation (HTTP 401).",
        ),
        pass_criteria="Claude, Claude Code, and Codex complete OAuth + loopback callbacks, enforce resource binding, refresh tokens cleanly, and fail with 401 after revocation.",
    ),
    ManualCheckSpec(
        slug="consent-badges",
        check="Custom consent & organisation-picker trust badges (MAC-07)",
        steps=(
            "Trigger OAuth consent via Open Alvary's custom consent / organisation-picker flow (/console/oauth/... via WorkOS Standalone Connect or custom consent integration).",
            "Verify MAC-07 three-tier client badges: (1) 'Verified publisher' (name + logo) for a reviewed CIMD URL, (2) 'From <host>' (host only, no logo) for an unreviewed CIMD URL, and (3) 'Unverified client' (quoted name + warning) for dynamic registration.",
            "Confirm return-URI host display, loopback-only warning when applicable, scope description ('Read the published Open Alvary corpus within your organisation's access'), and that lapsed organisations are not selectable.",
        ),
        pass_criteria="All three MAC-07 trust labels, return-host/loopback warnings, and active-entitlement organisation selection render accurately and bind the chosen organisation into issued tokens.",
    ),
    ManualCheckSpec(
        slug="signin-mfa",
        check="Google, Microsoft, email-code, passkey & 2FA flows",
        steps=(
            "Sign in with Google and with email one-time code; confirm email codes are single-use, expire, and lock after repeated wrong tries, and that identical emails across providers do not auto-link.",
            "Sign in with Microsoft; verify identity is keyed on Microsoft tenant + object ID and never links to an existing account by mutable email (malicious-tenant fixture).",
            "Enrol a passkey (WebAuthn Level 3) and TOTP authenticator, require a second factor per person/role, and confirm step-up enforcement on a sensitive action.",
        ),
        pass_criteria="Google, Microsoft (tenant+oid keyed), email-code, passkey, and role-based 2FA step-up all pass MAC-08 / MAC-16 rules.",
    ),
    ManualCheckSpec(
        slug="device-flow",
        check="Device authorization flow (RFC 8628)",
        steps=(
            "Start an RFC 8628 device authorization grant against {domain} with a public CLI client.",
            "Verify polling states: authorization_pending, slow_down, access_denied (when declined), and expired_token (when unapproved past expiry).",
            "Complete an approved device sign-in on a second device through the organisation picker and verify the issued token carries the chosen organisation claim.",
        ),
        pass_criteria="All four RFC 8628 error states and approved cross-device sign-in with organisation claim behave per MAC-16.",
    ),
    ManualCheckSpec(
        slug="compliance-pricing",
        check="SOC 2 / ISO 27001, EU data & launch pricing (MAC-16)",
        steps=(
            "Obtain and review WorkOS's current SOC 2 Type II or ISO/IEC 27001 report and file the evidence.",
            "Verify EU user data hosting or documented transfer basis for MAC-14's privacy notice.",
            "Record launch cost (<= US$100/month up to 1,000 MAU including custom sign-in domain) plus 10,000 MAU and enterprise SSO pricing.",
        ),
        pass_criteria="Audit report obtained, EU data/transfer basis documented, and launch pricing is within US$100/month.",
    ),
)


def environment(path: Path = ROOT / ".env") -> dict[str, str]:
    """Load environment values from git-ignored .env and process env (including WORKOS_*)."""
    values: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip("'\"")
    values.update({k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIXES)})
    return values


def collect_secrets(env: Mapping[str, str], extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Collect sensitive credential strings so they can be redacted from all outputs."""
    found: set[str] = set()
    for key, value in env.items():
        if value and (key == "WORKOS_API_KEY" or key.endswith(SECRET_KEY_SUFFIXES)):
            found.add(value)
    for item in extra:
        if item:
            found.add(item)
    return tuple(sorted(found, key=len, reverse=True))


def redact_secrets(text: str, secret_values: Iterable[str]) -> str:
    """Replace any known secret or Bearer token in text with [REDACTED]."""
    cleaned = _BEARER_RE.sub(r"\1[REDACTED]", text)
    for secret in secret_values:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def normalize_domain(domain: str) -> str:
    cleaned = domain.strip()
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.rstrip("/")


def require_workos_config(env: Mapping[str, str]) -> tuple[str, str, str]:
    """Return (api_key, client_id, authkit_domain) or raise WorkOSCheckError listing missing names."""
    required = ("WORKOS_API_KEY", "WORKOS_CLIENT_ID", "WORKOS_AUTHKIT_DOMAIN")
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise WorkOSCheckError(
            "Missing required WorkOS environment variable(s): "
            + ", ".join(missing)
            + ". Add them to git-ignored .env or export them in the environment."
        )
    return (
        env["WORKOS_API_KEY"].strip(),
        env["WORKOS_CLIENT_ID"].strip(),
        normalize_domain(env["WORKOS_AUTHKIT_DOMAIN"]),
    )


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile for a non-empty list of floats."""
    if not values:
        raise ValueError("percentile requires at least one measurement")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[index]


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("median requires at least one measurement")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def check_oidc_discovery(
    client: httpx.Client, domain: str, secret_values: Iterable[str]
) -> tuple[CheckResult, dict[str, Any]]:
    """Automated check 1: OAuth 2.1 / OIDC discovery metadata and JWKS."""
    base = f"https://{domain}"
    merged: dict[str, Any] = {}
    notes: list[str] = []
    endpoints_ok = 0

    for suffix in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            resp = client.get(f"{base}{suffix}")
        except httpx.HTTPError as exc:
            notes.append(f"{suffix} error: {redact_secrets(str(exc), secret_values)}")
            continue
        if resp.status_code == 200:
            doc = _safe_json(resp)
            merged.update(doc)
            endpoints_ok += 1
            notes.append(f"{suffix}=200")
        else:
            notes.append(f"{suffix}=HTTP {resp.status_code}")

    if endpoints_ok == 0:
        return (
            CheckResult(
                check="OIDC / OAuth 2.1 discovery",
                mode="Auto",
                result="Fail",
                evidence="; ".join(notes) or "Discovery metadata unreachable",
            ),
            {},
        )

    pkce = set(merged.get("code_challenge_methods_supported") or [])
    auth_methods = set(merged.get("token_endpoint_auth_methods_supported") or [])
    grants = set(merged.get("grant_types_supported") or [])
    cimd = bool(merged.get("client_id_metadata_document_supported"))
    iss_param = merged.get("authorization_response_iss_parameter_supported")
    jwks_uri = merged.get("jwks_uri")

    jwks_count = 0
    if isinstance(jwks_uri, str) and jwks_uri.startswith("https://"):
        try:
            jwks_resp = client.get(jwks_uri)
            if jwks_resp.status_code == 200:
                keys = _safe_json(jwks_resp).get("keys")
                jwks_count = len(keys) if isinstance(keys, list) else 0
                notes.append(f"jwks_keys={jwks_count}")
            else:
                notes.append(f"jwks=HTTP {jwks_resp.status_code}")
        except httpx.HTTPError as exc:
            notes.append(f"jwks error: {redact_secrets(str(exc), secret_values)}")

    missing: list[str] = []
    if "S256" not in pkce:
        missing.append("S256 PKCE")
    if "none" not in auth_methods:
        missing.append("auth_method=none")
    if "private_key_jwt" not in auth_methods:
        missing.append("auth_method=private_key_jwt")
    if not cimd:
        missing.append("client_id_metadata_document_supported=true")
    if not merged.get("authorization_endpoint") or not merged.get("token_endpoint"):
        missing.append("authorize/token endpoints")
    if jwks_count < 1:
        missing.append("non-empty JWKS")

    notes.append(f"cimd={cimd}")
    notes.append(f"iss_param={iss_param}")
    notes.append(f"auth_methods={sorted(auth_methods)}")
    if grants:
        notes.append(f"device_grant={'urn:ietf:params:oauth:grant-type:device_code' in grants}")
    notes.append(f"revocation={bool(merged.get('revocation_endpoint'))}")
    notes.append(f"dcr={bool(merged.get('registration_endpoint'))}")

    if missing:
        return (
            CheckResult(
                check="OIDC / OAuth 2.1 discovery",
                mode="Auto",
                result="Fail",
                evidence=f"Missing {', '.join(missing)}; " + "; ".join(notes),
            ),
            merged,
        )

    return (
        CheckResult(
            check="OIDC / OAuth 2.1 discovery",
            mode="Auto",
            result="Pass",
            evidence="; ".join(notes),
        ),
        merged,
    )


def check_cimd_lookup(
    client: httpx.Client,
    domain: str,
    discovery: Mapping[str, Any],
    public_url: str | None,
    secret_values: Iterable[str],
) -> CheckResult:
    """Automated check 2: CIMD metadata lookup and authorization-endpoint acceptance."""
    notes: list[str] = []
    cimd_supported = bool(discovery.get("client_id_metadata_document_supported"))
    notes.append(f"advertised_cimd={cimd_supported}")

    chatgpt_doc: dict[str, Any] = {}
    try:
        doc_resp = client.get(CHATGPT_CIMD)
        if doc_resp.status_code == 200:
            chatgpt_doc = _safe_json(doc_resp)
            auth_method = chatgpt_doc.get("token_endpoint_auth_method")
            jwks_uri = chatgpt_doc.get("jwks_uri")
            notes.append(f"chatgpt_cimd=200 (auth={auth_method}, jwks={bool(jwks_uri)})")
        else:
            notes.append(f"chatgpt_cimd=HTTP {doc_resp.status_code}")
    except httpx.HTTPError as exc:
        notes.append(f"chatgpt_cimd error: {redact_secrets(str(exc), secret_values)}")

    auth_endpoint = str(discovery.get("authorization_endpoint") or f"https://{domain}/oauth2/authorize")
    redirects = chatgpt_doc.get("redirect_uris")
    redirect_uri = (
        redirects[0]
        if isinstance(redirects, list) and redirects and isinstance(redirects[0], str)
        else "https://chatgpt.com/connector_platform_oauth_redirect"
    )
    _, challenge = _pkce_pair()
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": CHATGPT_CIMD,
        "redirect_uri": redirect_uri,
        "scope": "openid",
        "state": secrets.token_urlsafe(12),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if public_url:
        params["resource"] = f"{public_url.rstrip('/')}/mcp"

    authorize_accepted = False
    try:
        probe = client.get(f"{auth_endpoint}?{urlencode(params)}", follow_redirects=False)
        body_snippet = redact_secrets(probe.text[:160].replace("\n", " "), secret_values)
        if probe.status_code in (200, 302, 303):
            location = probe.headers.get("location", "")
            if "error=invalid_client" in location or "error=unauthorized_client" in location:
                notes.append(f"authorize_cimd=rejected ({probe.status_code} redirect error)")
            else:
                authorize_accepted = True
                notes.append(f"authorize_cimd=HTTP {probe.status_code} (accepted)")
        else:
            notes.append(f"authorize_cimd=HTTP {probe.status_code} ({body_snippet})")
    except httpx.HTTPError as exc:
        notes.append(f"authorize_cimd error: {redact_secrets(str(exc), secret_values)}")

    passed = cimd_supported and authorize_accepted
    return CheckResult(
        check="CIMD metadata registration / lookup",
        mode="Auto",
        result="Pass" if passed else "Fail",
        evidence="; ".join(notes),
    )


def check_refresh_rotation(
    client: httpx.Client,
    domain: str,
    discovery: Mapping[str, Any],
    client_id: str,
    token_file: Path,
    secret_values: Iterable[str],
) -> CheckResult:
    """Automated check 3: Refresh token rotation and reuse detection (MAC-16)."""
    token_endpoint = str(discovery.get("token_endpoint") or f"https://{domain}/oauth2/token")
    stored_refresh = ""
    if token_file.exists():
        try:
            stored = json.loads(token_file.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                stored_refresh = str(stored.get("refresh_token") or "").strip()
        except (OSError, ValueError):
            stored_refresh = ""

    extra_secrets = list(secret_values)
    if stored_refresh:
        extra_secrets.append(stored_refresh)

    if not stored_refresh:
        try:
            probe = client.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": f"oa_probe_invalid_{secrets.token_hex(8)}",
                },
            )
            err = _safe_json(probe).get("error") or f"HTTP {probe.status_code}"
            probe_note = f"synthetic invalid refresh -> HTTP {probe.status_code} ({err})"
        except httpx.HTTPError as exc:
            probe_note = f"token endpoint error: {redact_secrets(str(exc), extra_secrets)}"
        return CheckResult(
            check="Token refresh rotation",
            mode="Auto",
            result="Pending",
            evidence=(
                f"No test refresh token in {token_file.relative_to(ROOT) if token_file.is_relative_to(ROOT) else token_file}; "
                f"{probe_note}. Save a browser sign-in token JSON (mode 600) and re-run to test reuse family revocation."
            ),
        )

    def _refresh(token: str) -> httpx.Response:
        return client.post(
            token_endpoint,
            data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": token},
        )

    try:
        first = _refresh(stored_refresh)
        first_body = _safe_json(first)
        new_refresh = str(first_body.get("refresh_token") or "").strip()
        if new_refresh:
            extra_secrets.append(new_refresh)
        if first.status_code != 200 or not new_refresh:
            return CheckResult(
                check="Token refresh rotation",
                mode="Auto",
                result="Fail",
                evidence=(
                    f"Initial refresh failed: HTTP {first.status_code} "
                    f"({redact_secrets(str(first_body.get('error', 'missing refresh_token')), extra_secrets)})"
                ),
            )

        rotated = new_refresh != stored_refresh
        replay_old = _refresh(stored_refresh)
        replay_old_err = _safe_json(replay_old).get("error")
        replay_new = _refresh(new_refresh)
        replay_new_err = _safe_json(replay_new).get("error")
    except httpx.HTTPError as exc:
        return CheckResult(
            check="Token refresh rotation",
            mode="Auto",
            result="Fail",
            evidence=f"HTTP error during rotation check: {redact_secrets(str(exc), extra_secrets)}",
        )

    passed = (
        rotated
        and replay_old.status_code >= 400
        and replay_old_err == "invalid_grant"
        and replay_new.status_code >= 400
        and replay_new_err == "invalid_grant"
    )
    evidence = (
        f"refresh_1=200 (rotated={rotated}); "
        f"replay_old=HTTP {replay_old.status_code} ({replay_old_err}); "
        f"replay_new=HTTP {replay_new.status_code} ({replay_new_err})"
    )
    return CheckResult(
        check="Token refresh rotation",
        mode="Auto",
        result="Pass" if passed else "Fail",
        evidence=redact_secrets(evidence, extra_secrets),
    )


def check_account_export_deletion(
    client: httpx.Client,
    api_key: str,
    secret_values: Iterable[str],
    *,
    skip_mutation: bool = False,
) -> CheckResult:
    """Automated check 4: Account export and deletion via WorkOS User Management API."""
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    users_url = f"{WORKOS_API_BASE}/user_management/users"

    if skip_mutation:
        try:
            resp = client.get(users_url, params={"limit": 1}, headers=headers)
            return CheckResult(
                check="Account export & deletion APIs",
                mode="Auto",
                result="Pending" if resp.status_code == 200 else "Fail",
                evidence=f"Read-only check (--skip-user-mutation): GET /user_management/users -> HTTP {resp.status_code}",
            )
        except httpx.HTTPError as exc:
            return CheckResult(
                check="Account export & deletion APIs",
                mode="Auto",
                result="Fail",
                evidence=f"API unreachable: {redact_secrets(str(exc), secret_values)}",
            )

    test_email = f"idp-check-{secrets.token_hex(6)}@example.invalid"
    created_id: str | None = None
    notes: list[str] = []
    try:
        create_resp = client.post(
            users_url,
            headers=headers,
            json={
                "email": test_email,
                "first_name": "OpenAlvary",
                "last_name": "IdpCheck",
                "email_verified": False,
            },
        )
        create_body = _safe_json(create_resp)
        created_id = str(create_body.get("id") or "").strip() or None
        if create_resp.status_code not in (200, 201) or not created_id:
            return CheckResult(
                check="Account export & deletion APIs",
                mode="Auto",
                result="Fail",
                evidence=(
                    f"Create test user failed: HTTP {create_resp.status_code} "
                    f"{redact_secrets(create_resp.text[:160], secret_values)}"
                ),
            )
        notes.append(f"created={created_id}")

        export_resp = client.get(f"{users_url}/{created_id}", headers=headers)
        export_body = _safe_json(export_resp)
        exported_keys = sorted(k for k in ("id", "email", "created_at", "updated_at") if k in export_body)
        export_ok = export_resp.status_code == 200 and "id" in export_body and "email" in export_body
        notes.append(f"export=HTTP {export_resp.status_code} (fields={','.join(exported_keys)})")

        delete_resp = client.delete(f"{users_url}/{created_id}", headers=headers)
        delete_ok = delete_resp.status_code in (200, 202, 204)
        notes.append(f"delete=HTTP {delete_resp.status_code}")

        verify_resp = client.get(f"{users_url}/{created_id}", headers=headers)
        verify_ok = verify_resp.status_code == 404
        notes.append(f"post_delete_get=HTTP {verify_resp.status_code}")

        if delete_ok:
            created_id = None
        passed = export_ok and delete_ok and verify_ok
        return CheckResult(
            check="Account export & deletion APIs",
            mode="Auto",
            result="Pass" if passed else "Fail",
            evidence="; ".join(notes),
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            check="Account export & deletion APIs",
            mode="Auto",
            result="Fail",
            evidence=f"HTTP error: {redact_secrets(str(exc), secret_values)}",
        )
    finally:
        if created_id:
            try:
                client.delete(f"{users_url}/{created_id}", headers=headers)
            except httpx.HTTPError:
                pass


def check_latency(
    client: httpx.Client,
    domain: str,
    discovery: Mapping[str, Any],
    client_id: str,
    secret_values: Iterable[str],
    *,
    runs: int = DEFAULT_LATENCY_RUNS,
) -> CheckResult:
    """Automated check 5: p95 latency < 2 s over N sequential /authorize + /token calls."""
    auth_endpoint = str(discovery.get("authorization_endpoint") or f"https://{domain}/oauth2/authorize")
    token_endpoint = str(discovery.get("token_endpoint") or f"https://{domain}/oauth2/token")

    auth_ms: list[float] = []
    token_ms: list[float] = []
    pair_ms: list[float] = []
    errors: list[str] = []

    for i in range(runs):
        verifier, challenge = _pkce_pair()
        auth_query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:53682/callback",
                "scope": "openid",
                "state": f"lat-{i}-{secrets.token_hex(4)}",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        try:
            t0 = time.perf_counter()
            auth_resp = client.get(f"{auth_endpoint}?{auth_query}", follow_redirects=False)
            t1 = time.perf_counter()
            tok_resp = client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": f"oa_latency_probe_{secrets.token_hex(8)}",
                    "redirect_uri": "http://127.0.0.1:53682/callback",
                    "code_verifier": verifier,
                },
            )
            t2 = time.perf_counter()
        except httpx.HTTPError as exc:
            errors.append(f"run {i + 1}: {redact_secrets(str(exc), secret_values)}")
            continue

        a_ms = (t1 - t0) * 1000.0
        k_ms = (t2 - t1) * 1000.0
        auth_ms.append(a_ms)
        token_ms.append(k_ms)
        pair_ms.append(a_ms + k_ms)
        if auth_resp.status_code >= 500 or tok_resp.status_code >= 500:
            errors.append(f"run {i + 1}: 5xx (auth={auth_resp.status_code}, token={tok_resp.status_code})")

    if len(pair_ms) < runs:
        return CheckResult(
            check=f"Latency (p95 < 2 s over {runs} /authorize + /token calls)",
            mode="Auto",
            result="Fail",
            evidence=f"Completed {len(pair_ms)}/{runs} sequential pairs; errors: {'; '.join(errors[:3])}",
        )

    auth_p95 = percentile(auth_ms, 95)
    token_p95 = percentile(token_ms, 95)
    pair_p95 = percentile(pair_ms, 95)
    passed = not errors and pair_p95 < LATENCY_THRESHOLD_MS
    evidence = (
        f"/authorize median {median(auth_ms):.0f} ms / p95 {auth_p95:.0f} ms; "
        f"/token median {median(token_ms):.0f} ms / p95 {token_p95:.0f} ms; "
        f"combined p95 {pair_p95:.0f} ms (limit < {LATENCY_THRESHOLD_MS:.0f} ms)"
    )
    if errors:
        evidence += f"; errors: {'; '.join(errors[:3])}"
    return CheckResult(
        check=f"Latency (p95 < 2 s over {runs} /authorize + /token calls)",
        mode="Auto",
        result="Pass" if passed else "Fail",
        evidence=evidence,
    )


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").strip()


def parse_existing_manual_rows(path: Path) -> dict[str, CheckResult]:
    """Read previously recorded Manual rows from workos-results.md so re-runs preserve them."""
    if not path.exists():
        return {}
    preserved: dict[str, CheckResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped == _TABLE_HEADER or stripped.startswith("|---"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        check_name, mode, result, evidence = cells[0], cells[1], cells[2], cells[3]
        if mode.lower() == "manual":
            preserved[check_name] = CheckResult(
                check=check_name, mode="Manual", result=result, evidence=evidence
            )
    return preserved


def parse_record_overrides(items: Iterable[str]) -> dict[str, tuple[str, str]]:
    """Parse --record 'slug-or-name=Pass: evidence notes' strings."""
    slug_to_name = {spec.slug.lower(): spec.check for spec in MANUAL_CHECKS}
    name_to_name = {spec.check.lower(): spec.check for spec in MANUAL_CHECKS}
    overrides: dict[str, tuple[str, str]] = {}
    for raw in items:
        if "=" not in raw:
            raise WorkOSCheckError(f"Invalid --record format (expected CHECK=RESULT[:NOTES]): {raw}")
        key_part, val_part = raw.split("=", 1)
        lookup = key_part.strip().lower()
        target = slug_to_name.get(lookup) or name_to_name.get(lookup) or key_part.strip()
        if ":" in val_part:
            status_raw, evidence = val_part.split(":", 1)
        else:
            status_raw, evidence = val_part, ""
        status_norm = status_raw.strip().capitalize()
        if status_norm not in ("Pass", "Fail", "Pending"):
            raise WorkOSCheckError(
                f"Invalid result '{status_raw.strip()}' for '{target}' (expected Pass, Fail, or Pending)"
            )
        overrides[target] = (status_norm, evidence.strip())
    return overrides


def build_manual_rows(
    existing: Mapping[str, CheckResult],
    overrides: Mapping[str, tuple[str, str]],
) -> list[CheckResult]:
    rows: list[CheckResult] = []
    known_names = {spec.check for spec in MANUAL_CHECKS}
    for spec in MANUAL_CHECKS:
        if spec.check in overrides:
            res, ev = overrides[spec.check]
            rows.append(CheckResult(check=spec.check, mode="Manual", result=res, evidence=ev))
        elif spec.check in existing:
            rows.append(existing[spec.check])
        else:
            rows.append(
                CheckResult(
                    check=spec.check,
                    mode="Manual",
                    result="Pending",
                    evidence=f"Run manual script (--record {spec.slug}=Pass:<notes>)",
                )
            )
    for name, (res, ev) in overrides.items():
        if name not in known_names:
            rows.append(CheckResult(check=name, mode="Manual", result=res, evidence=ev))
    return rows


def render_manual_script(domain: str, public_url: str | None) -> str:
    url = (public_url or "https://<public-test-host>").rstrip("/")
    lines = ["## Scripted Manual Browser Checks", ""]
    for idx, spec in enumerate(MANUAL_CHECKS, start=1):
        lines.append(f"### {idx}. {spec.check} (`--record {spec.slug}=Pass|Fail:<notes>`)")
        for step_no, step in enumerate(spec.steps, start=1):
            rendered = step.format(
                public_url=url,
                domain=domain or "<WORKOS_AUTHKIT_DOMAIN>",
                chatgpt_cimd=CHATGPT_CIMD,
                claude_cimd=CLAUDE_CIMD,
                claude_code_cimd=CLAUDE_CODE_CIMD,
                codex_cimd=CODEX_CIMD,
            )
            lines.append(f"{step_no}. {rendered}")
        lines.append(f"- **Pass criteria:** {spec.pass_criteria}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(
    rows: Iterable[CheckResult],
    *,
    domain: str,
    public_url: str | None,
    secret_values: Iterable[str] = (),
) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    row_list = list(rows)
    auto_pass = sum(1 for r in row_list if r.mode == "Auto" and r.result == "Pass")
    auto_total = sum(1 for r in row_list if r.mode == "Auto")
    manual_pass = sum(1 for r in row_list if r.mode == "Manual" and r.result == "Pass")
    manual_total = sum(1 for r in row_list if r.mode == "Manual")

    lines = [
        "# WorkOS AuthKit confirmation checks (Measurement 1, MAC-16)",
        "",
        f"- **Updated:** {now}",
        f"- **AuthKit domain:** `{domain or '<unset>'}`",
        f"- **Public MCP test URL:** `{public_url.rstrip('/') + '/mcp' if public_url else '<not provided>'}`",
        f"- **Summary:** Auto {auto_pass}/{auto_total} passed; Manual {manual_pass}/{manual_total} passed.",
        "- **Auth0 baseline preserved:** `private-staging/idp-check/results.md` is untouched.",
        "",
        _TABLE_HEADER,
        "|---|---|---|---|",
    ]
    for row in row_list:
        lines.append(
            f"| {_escape_cell(row.check)} | {_escape_cell(row.mode)} | "
            f"{_escape_cell(row.result)} | {_escape_cell(redact_secrets(row.evidence, secret_values))} |"
        )
    lines.append("")
    lines.append(render_manual_script(domain, public_url))
    return redact_secrets("\n".join(lines), secret_values)


def run_automated_checks(
    env: Mapping[str, str],
    *,
    public_url: str | None = None,
    token_file: Path = DEFAULT_TOKENS,
    latency_runs: int = DEFAULT_LATENCY_RUNS,
    skip_user_mutation: bool = False,
    http_client: httpx.Client | None = None,
) -> tuple[str, list[CheckResult]]:
    """Execute all automated WorkOS AuthKit checks and return (domain, results)."""
    api_key, client_id, domain = require_workos_config(env)
    secret_values = collect_secrets(env)

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=20.0)
    try:
        discovery_result, discovery_doc = check_oidc_discovery(client, domain, secret_values)
        cimd_result = check_cimd_lookup(client, domain, discovery_doc, public_url, secret_values)
        refresh_result = check_refresh_rotation(
            client, domain, discovery_doc, client_id, token_file, secret_values
        )
        account_result = check_account_export_deletion(
            client, api_key, secret_values, skip_mutation=skip_user_mutation
        )
        latency_result = check_latency(
            client, domain, discovery_doc, client_id, secret_values, runs=latency_runs
        )
        return domain, [discovery_result, cimd_result, refresh_result, account_result, latency_result]
    finally:
        if owns_client:
            client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trials.idp_check.workos",
        description=(
            "Run WorkOS AuthKit MAC-16 checks (automated API checks + scripted manual browser checks) "
            "and write a pass/fail table to private-staging/idp-check/workos-results.md."
        ),
    )
    parser.add_argument(
        "--public-url",
        help="Public HTTPS base URL of the staging or test MCP server (e.g. https://mcp-check.example.com).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output markdown path (default: {DEFAULT_OUTPUT.relative_to(ROOT)}).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help="Path to git-ignored .env file holding WORKOS_* credentials (default: .env).",
    )
    parser.add_argument(
        "--refresh-token-file",
        type=Path,
        default=DEFAULT_TOKENS,
        help="Path to mode-600 JSON file containing a test refresh_token for rotation checks.",
    )
    parser.add_argument(
        "--latency-runs",
        type=int,
        default=DEFAULT_LATENCY_RUNS,
        help=f"Number of sequential /authorize + /token calls for p95 latency (default: {DEFAULT_LATENCY_RUNS}).",
    )
    parser.add_argument(
        "--skip-user-mutation",
        action="store_true",
        help="Skip creating/deleting a synthetic test user during the account export/deletion check.",
    )
    parser.add_argument(
        "--record",
        action="append",
        default=[],
        metavar="CHECK=RESULT[:NOTES]",
        help=(
            "Record a manual check outcome (repeatable), e.g. "
            "--record 'chatgpt-cimd=Pass: private_key_jwt succeeded on free tier'."
        ),
    )
    parser.add_argument(
        "--manual-only",
        action="store_true",
        help="Skip automated API checks and only update/render manual checks and the scripted runbook.",
    )
    parser.add_argument(
        "--print-manual",
        action="store_true",
        help="Print the scripted manual browser checks to stdout after writing the report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.latency_runs < 1:
        raise WorkOSCheckError("--latency-runs must be at least 1")

    env = environment(args.env_file)
    secret_values = collect_secrets(env)
    overrides = parse_record_overrides(args.record)
    existing_manual = parse_existing_manual_rows(args.output)

    auto_rows: list[CheckResult] = []
    if args.manual_only:
        domain = normalize_domain(env.get("WORKOS_AUTHKIT_DOMAIN", ""))
    else:
        domain, auto_rows = run_automated_checks(
            env,
            public_url=args.public_url,
            token_file=args.refresh_token_file,
            latency_runs=args.latency_runs,
            skip_user_mutation=args.skip_user_mutation,
        )

    manual_rows = build_manual_rows(existing_manual, overrides)
    all_rows = auto_rows + manual_rows

    markdown = render_markdown(
        all_rows,
        domain=domain,
        public_url=args.public_url,
        secret_values=secret_values,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")

    for row in all_rows:
        print(
            f"[{row.mode:6s}] {row.result:7s}  {row.check}  -- {redact_secrets(row.evidence, secret_values)}"
        )
    print(f"\nWrote WorkOS results to {args.output}")
    if args.print_manual:
        print()
        print(render_manual_script(domain, args.public_url))

    if any(r.mode == "Auto" and r.result == "Fail" for r in all_rows):
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkOSCheckError as error:
        raise SystemExit(str(error)) from None
