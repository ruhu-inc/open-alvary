"""Live MCP host interoperability probe and measurement matrix recorder (Measurement 2).

    python -m trials.idp_check.host_probe \\
        --listen 127.0.0.1:8099 \\
        --upstream http://127.0.0.1:8022 \\
        [--output private-staging/idp-check/host-matrix.md]

Runs a logging reverse proxy in front of local or staging /mcp that strips Authorization and
Cookie headers before writing logs, captures each host's wire MCP-Protocol-Version
(2026-07-28 vs. 2025-11-25 vs. 2025-06-18) and JSON-RPC method sequence (server/discover vs.
initialize), and pairs wire logs with two recorded operator steps per host:
  (a) recording the host's OAuth registration mechanism (exact CIMD URL vs. DCR) from IdP logs;
  (b) running a standardised in-host canary prompt (without invoking a tool) to test whether
      initialize.instructions / server/discover.instructions reach the model.

Writes/updates private-staging/idp-check/host-matrix.md using columns:
    | Host | Tier | Protocol Version | Registration (CIMD URL / DCR) | Instructions Visible | Status |
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import signal
import sys
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "private-staging" / "idp-check" / "host-matrix.md"
DEFAULT_LOG_FILE = ROOT / "private-staging" / "idp-check" / "host-probe-wire.jsonl"

MATRIX_HEADER = (
    "| Host | Tier | Protocol Version | Registration (CIMD URL / DCR) | Instructions Visible | Status |"
)

SENSITIVE_REQUEST_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
SENSITIVE_RESPONSE_HEADERS = frozenset({"set-cookie"})
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
KNOWN_PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")

DEFAULT_CANARY_MARKER = "OA-POLICY-CANARY-2026-OFFICIAL-COPY-PREVAILS"
CANARY_PROMPT_TEMPLATE = (
    "Without calling any tools, read the server instructions supplied by the Open Alvary MCP "
    "server and repeat the exact policy marker or official-copy rule stated in those instructions "
    "(marker: {marker}). If you cannot see any Open Alvary server instructions, reply exactly: "
    "NO_SERVER_INSTRUCTIONS_VISIBLE"
)

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/]+=*", re.IGNORECASE)


@dataclass
class HostEntry:
    host: str
    tier: str
    protocol_version: str = "untested"
    registration: str = "untested"
    instructions_visible: str = "untested"
    status: str = "untested"
    method_sequence: list[str] = field(default_factory=list)
    notes: str = ""


DEFAULT_HOSTS: tuple[tuple[str, str], ...] = (
    ("Claude", "Tier 1"),
    ("Claude Code", "Tier 1"),
    ("Codex", "Tier 1"),
    ("Antigravity", "Tier 1"),
    ("ChatGPT", "Tier 1"),
    ("Claude Cowork", "Tier 2"),
    ("Gemini CLI", "Tier 2"),
    ("Cursor", "Tier 2"),
)

HOST_ALIASES: dict[str, str] = {
    "claude": "Claude",
    "claude-web": "Claude",
    "claude-desktop": "Claude",
    "claude-code": "Claude Code",
    "claude code": "Claude Code",
    "code": "Claude Code",
    "codex": "Codex",
    "antigravity": "Antigravity",
    "agy": "Antigravity",
    "chatgpt": "ChatGPT",
    "claude-cowork": "Claude Cowork",
    "claude cowork": "Claude Cowork",
    "cowork": "Claude Cowork",
    "gemini-cli": "Gemini CLI",
    "gemini cli": "Gemini CLI",
    "cursor": "Cursor",
}

VALID_INSTRUCTIONS = ("yes", "no", "partial", "untested")
VALID_STATUSES = ("verified", "developer_fallback", "deferred", "failed", "in_progress", "untested")


def canonical_host_name(name: str) -> str:
    cleaned = name.strip()
    return HOST_ALIASES.get(cleaned.lower(), cleaned)


def default_matrix_entries() -> list[HostEntry]:
    return [HostEntry(host=host, tier=tier) for host, tier in DEFAULT_HOSTS]


def sanitize_headers(
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    strip_names: frozenset[str] = SENSITIVE_REQUEST_HEADERS,
) -> dict[str, str]:
    """Return a copy of HTTP headers with Authorization, Cookie, and other sensitive headers stripped."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    sanitized: dict[str, str] = {}
    for key, value in items:
        lower = key.lower()
        if lower in strip_names:
            continue
        sanitized[key] = _BEARER_RE.sub(r"\1[REDACTED]", value)
    return sanitized


def _parse_json_object(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_protocol_version(
    req_headers: Mapping[str, str], req_json: Mapping[str, Any] | None
) -> tuple[str | None, str | None, str | None]:
    """Return (effective_version, header_version, body_version)."""
    header_version: str | None = None
    for key, value in req_headers.items():
        if key.lower() == "mcp-protocol-version" and value.strip():
            header_version = value.strip()
            break

    body_version: str | None = None
    if isinstance(req_json, Mapping):
        params = req_json.get("params")
        if isinstance(params, Mapping):
            pv = params.get("protocolVersion")
            if isinstance(pv, str) and pv.strip():
                body_version = pv.strip()
            meta = params.get("_meta")
            if body_version is None and isinstance(meta, Mapping):
                meta_pv = meta.get("io.modelcontextprotocol/protocolVersion")
                if isinstance(meta_pv, str) and meta_pv.strip():
                    body_version = meta_pv.strip()

    return (header_version or body_version), header_version, body_version


def _extract_client_info(req_json: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(req_json, Mapping):
        return None
    params = req_json.get("params")
    if not isinstance(params, Mapping):
        return None
    info = params.get("clientInfo")
    if isinstance(info, Mapping):
        return {k: v for k, v in info.items() if k in ("name", "version", "title")}
    meta = params.get("_meta")
    if isinstance(meta, Mapping):
        meta_info = meta.get("io.modelcontextprotocol/clientInfo")
        if isinstance(meta_info, Mapping):
            return {k: v for k, v in meta_info.items() if k in ("name", "version", "title")}
    return None


def infer_host_label(
    explicit_host: str | None,
    req_headers: Mapping[str, str],
    client_info: Mapping[str, Any] | None,
) -> str:
    if explicit_host and explicit_host.strip():
        return canonical_host_name(explicit_host)
    if client_info and isinstance(client_info.get("name"), str) and client_info["name"].strip():
        return canonical_host_name(client_info["name"])
    for key, value in req_headers.items():
        if key.lower() == "user-agent" and value.strip():
            lowered = value.lower()
            for alias, target in HOST_ALIASES.items():
                if alias in lowered:
                    return target
            return value.strip()[:60]
    return "unknown"


def maybe_inject_canary(resp_body: bytes, marker: str) -> bytes:
    """Optionally append a canary marker to result.instructions in JSON-RPC responses."""
    doc = _parse_json_object(resp_body)
    if not doc:
        return resp_body
    result = doc.get("result")
    if not isinstance(result, dict):
        return resp_body
    instructions = result.get("instructions")
    if not isinstance(instructions, str) or marker in instructions:
        return resp_body
    suffix = f" [Canary policy marker: {marker}]"
    result["instructions"] = instructions.rstrip() + suffix
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


def build_wire_event(
    *,
    http_method: str,
    path: str,
    req_headers: Mapping[str, str] | Iterable[tuple[str, str]],
    req_body: bytes,
    status_code: int,
    resp_headers: Mapping[str, str] | Iterable[tuple[str, str]],
    resp_body: bytes,
    host_label: str | None = None,
    canary_marker: str = DEFAULT_CANARY_MARKER,
) -> dict[str, Any]:
    """Create a credential-free wire log event capturing MCP-Protocol-Version and method."""
    safe_req_headers = sanitize_headers(req_headers, strip_names=SENSITIVE_REQUEST_HEADERS)
    safe_resp_headers = sanitize_headers(resp_headers, strip_names=SENSITIVE_RESPONSE_HEADERS)

    req_json = _parse_json_object(req_body)
    resp_json = _parse_json_object(resp_body)

    effective_ver, header_ver, body_ver = _extract_protocol_version(safe_req_headers, req_json)
    rpc_method = req_json.get("method") if isinstance(req_json, Mapping) else None
    if not isinstance(rpc_method, str):
        rpc_method = None

    tool_name: str | None = None
    if rpc_method == "tools/call" and isinstance(req_json, Mapping):
        params = req_json.get("params")
        if isinstance(params, Mapping) and isinstance(params.get("name"), str):
            tool_name = params["name"]
    if tool_name is None:
        for key, value in safe_req_headers.items():
            if key.lower() == "mcp-name" and value.strip():
                tool_name = value.strip()
                break

    client_info = _extract_client_info(req_json)
    resolved_host = infer_host_label(host_label, safe_req_headers, client_info)

    resp_protocol_version: str | None = None
    resp_supported_versions: list[str] | None = None
    instructions_returned = False
    canary_in_instructions = False
    if isinstance(resp_json, Mapping) and isinstance(resp_json.get("result"), Mapping):
        result = resp_json["result"]
        pv = result.get("protocolVersion")
        if isinstance(pv, str):
            resp_protocol_version = pv
        sv = result.get("supportedVersions")
        if isinstance(sv, list):
            resp_supported_versions = [str(item) for item in sv]
        instr = result.get("instructions")
        if isinstance(instr, str) and instr.strip():
            instructions_returned = True
            canary_in_instructions = canary_marker in instr

    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": resolved_host,
        "http_method": http_method,
        "path": path,
        "status_code": status_code,
        "mcp_protocol_version": effective_ver,
        "header_protocol_version": header_ver,
        "body_protocol_version": body_ver,
        "response_protocol_version": resp_protocol_version,
        "response_supported_versions": resp_supported_versions,
        "method": rpc_method,
        "tool_name": tool_name,
        "client_info": client_info,
        "instructions_returned": instructions_returned,
        "canary_in_instructions": canary_in_instructions,
        "request_headers": safe_req_headers,
        "response_headers": safe_resp_headers,
    }


def append_wire_log(log_file: Path, event: Mapping[str, Any], lock: threading.Lock | None = None) -> None:
    line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    if lock is not None:
        lock.acquire()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        existed = log_file.exists()
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line)
        if not existed:
            os.chmod(log_file, 0o600)
    finally:
        if lock is not None:
            lock.release()


def summarize_wire_log(log_file: Path) -> dict[str, dict[str, Any]]:
    """Aggregate observed protocol versions and ordered method sequences per host from JSONL."""
    summary: dict[str, dict[str, Any]] = {}
    if not log_file.exists():
        return summary
    for raw in log_file.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        host = canonical_host_name(str(event.get("host") or "unknown"))
        bucket = summary.setdefault(
            host,
            {"protocol_versions": [], "methods": [], "instructions_returned": False},
        )
        ver = event.get("mcp_protocol_version")
        if isinstance(ver, str) and ver and ver not in bucket["protocol_versions"]:
            bucket["protocol_versions"].append(ver)
        method = event.get("method")
        if isinstance(method, str) and method:
            if not bucket["methods"] or bucket["methods"][-1] != method:
                bucket["methods"].append(method)
        if event.get("instructions_returned"):
            bucket["instructions_returned"] = True
    return summary


def parse_listen(listen: str) -> tuple[str, int]:
    if ":" not in listen:
        raise ValueError(f"--listen must be HOST:PORT (got {listen!r})")
    host, port_raw = listen.rsplit(":", 1)
    port = int(port_raw)
    if not (0 <= port <= 65535):
        raise ValueError(f"--listen port out of range: {port}")
    return host.strip("[]") or "127.0.0.1", port


class ProbeProxyServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        upstream: str,
        log_file: Path,
        host_label: str | None = None,
        inject_canary: bool = False,
        canary_marker: str = DEFAULT_CANARY_MARKER,
        timeout: float = 30.0,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.log_file = log_file
        self.host_label = host_label
        self.inject_canary = inject_canary
        self.canary_marker = canary_marker
        self.log_lock = threading.Lock()
        self.http_client = httpx.Client(timeout=timeout, follow_redirects=False)
        super().__init__(server_address, _ProbeRequestHandler)

    def server_close(self) -> None:
        self.http_client.close()
        super().server_close()


class _ProbeRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProbeProxyServer  # type: ignore[assignment]

    def _forward(self) -> None:
        content_length = int(self.headers.get("Content-Length") or "0")
        req_body = self.rfile.read(content_length) if content_length > 0 else b""
        req_headers_raw = list(self.headers.items())

        forward_headers = {
            key: value for key, value in req_headers_raw if key.lower() not in HOP_BY_HOP_HEADERS
        }
        target_url = urljoin(f"{self.server.upstream}/", self.path.lstrip("/"))

        try:
            with self.server.http_client.stream(
                self.command,
                target_url,
                headers=forward_headers,
                content=req_body if req_body else None,
            ) as upstream_resp:
                content_type = upstream_resp.headers.get("content-type", "")
                is_sse = "text/event-stream" in content_type.lower()
                resp_headers_raw = list(upstream_resp.headers.items())

                if is_sse:
                    self.send_response(upstream_resp.status_code)
                    for key, value in resp_headers_raw:
                        if key.lower() not in HOP_BY_HOP_HEADERS:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    captured = bytearray()
                    for chunk in upstream_resp.iter_bytes():
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            if len(captured) < 65536:
                                captured.extend(chunk[: 65536 - len(captured)])
                    resp_body = bytes(captured)
                else:
                    resp_body = upstream_resp.read()
                    if self.server.inject_canary and "application/json" in content_type.lower():
                        resp_body = maybe_inject_canary(resp_body, self.server.canary_marker)
                    self.send_response(upstream_resp.status_code)
                    for key, value in resp_headers_raw:
                        if key.lower() not in HOP_BY_HOP_HEADERS:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if self.command != "HEAD" and resp_body:
                        self.wfile.write(resp_body)

                status_code = upstream_resp.status_code
        except httpx.HTTPError as exc:
            error_body = json.dumps({"error": "upstream_unavailable", "detail": str(exc)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(error_body)
            status_code = 502
            resp_headers_raw = [("Content-Type", "application/json")]
            resp_body = error_body

        event = build_wire_event(
            http_method=self.command,
            path=self.path,
            req_headers=req_headers_raw,
            req_body=req_body,
            status_code=status_code,
            resp_headers=resp_headers_raw,
            resp_body=resp_body,
            host_label=self.server.host_label,
            canary_marker=self.server.canary_marker,
        )
        append_wire_log(self.server.log_file, event, self.server.log_lock)
        print(
            f"[{event['timestamp']}] host={event['host']} {event['http_method']} {event['path']} "
            f"-> HTTP {event['status_code']} | version={event['mcp_protocol_version'] or '-'} "
            f"| method={event['method'] or '-'}",
            flush=True,
        )

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_proxy_server(
    listen: str,
    upstream: str,
    *,
    log_file: Path = DEFAULT_LOG_FILE,
    host_label: str | None = None,
    inject_canary: bool = False,
    canary_marker: str = DEFAULT_CANARY_MARKER,
) -> ProbeProxyServer:
    host, port = parse_listen(listen)
    return ProbeProxyServer(
        (host, port),
        upstream=upstream,
        log_file=log_file,
        host_label=host_label,
        inject_canary=inject_canary,
        canary_marker=canary_marker,
    )


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").strip()


def load_matrix(path: Path) -> list[HostEntry]:
    """Load existing host-matrix.md rows, preserving canonical Tier-1 / Tier-2 ordering."""
    entries_by_host: dict[str, HostEntry] = {e.host: e for e in default_matrix_entries()}
    order = [e.host for e in default_matrix_entries()]

    if not path.exists():
        return [entries_by_host[h] for h in order]

    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped == MATRIX_HEADER or stripped.startswith("|---"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 6:
            continue
        host = canonical_host_name(cells[0])
        entry = entries_by_host.get(host)
        if entry is None:
            entry = HostEntry(host=host, tier=cells[1] or "Tier 2")
            entries_by_host[host] = entry
            order.append(host)
        else:
            entry.tier = cells[1] or entry.tier
        entry.protocol_version = cells[2] or "untested"
        entry.registration = cells[3] or "untested"
        entry.instructions_visible = cells[4] or "untested"
        entry.status = cells[5] or "untested"

    seq_re = re.compile(r"^- \*\*(.+?):\*\* wire=`([^`]*)`(?:\s*\|\s*(.*))?$")
    for line in text.splitlines():
        match = seq_re.match(line.strip())
        if not match:
            continue
        host = canonical_host_name(match.group(1))
        if host in entries_by_host:
            wire = match.group(2).strip()
            if wire and wire != "none":
                entries_by_host[host].method_sequence = [m.strip() for m in wire.split("->") if m.strip()]
            note_part = (match.group(3) or "").strip()
            if note_part:
                entries_by_host[host].notes = note_part

    return [entries_by_host[h] for h in order]


def merge_wire_summary(entries: list[HostEntry], summary: Mapping[str, Mapping[str, Any]]) -> list[HostEntry]:
    by_host = {entry.host: entry for entry in entries}
    for host, data in summary.items():
        if host == "unknown":
            continue
        entry = by_host.get(host)
        if entry is None:
            entry = HostEntry(host=host, tier="Tier 2")
            entries.append(entry)
            by_host[host] = entry
        versions = list(data.get("protocol_versions") or [])
        if versions:
            entry.protocol_version = ", ".join(versions)
        methods = list(data.get("methods") or [])
        if methods:
            entry.method_sequence = methods
    return entries


def update_host_entry(
    entries: list[HostEntry],
    *,
    host: str,
    tier: str | None = None,
    protocol_version: str | None = None,
    registration: str | None = None,
    instructions_visible: str | None = None,
    status: str | None = None,
    method_sequence: list[str] | None = None,
    notes: str | None = None,
    force_verified: bool = False,
) -> HostEntry:
    canonical = canonical_host_name(host)
    entry = next((e for e in entries if e.host == canonical), None)
    if entry is None:
        entry = HostEntry(host=canonical, tier=tier or "Tier 2")
        entries.append(entry)
    elif tier:
        entry.tier = tier

    if protocol_version is not None:
        entry.protocol_version = protocol_version.strip() or "untested"
    if registration is not None:
        entry.registration = registration.strip() or "untested"
    if instructions_visible is not None:
        norm_instr = instructions_visible.strip().lower()
        if norm_instr not in VALID_INSTRUCTIONS:
            raise ValueError(
                f"Invalid --instructions-visible {instructions_visible!r}; expected one of {VALID_INSTRUCTIONS}"
            )
        entry.instructions_visible = norm_instr
    if method_sequence is not None:
        entry.method_sequence = [m.strip() for m in method_sequence if m.strip()]
    if notes is not None:
        entry.notes = notes.strip()
    if status is not None:
        norm_status = status.strip().lower()
        if norm_status not in VALID_STATUSES:
            raise ValueError(f"Invalid --status {status!r}; expected one of {VALID_STATUSES}")
        if norm_status == "verified" and not force_verified:
            missing = [
                label
                for label, val in (
                    ("protocol_version", entry.protocol_version),
                    ("registration", entry.registration),
                    ("instructions_visible", entry.instructions_visible),
                )
                if not val or val.lower() == "untested"
            ]
            if missing:
                raise ValueError(
                    f"Cannot mark {entry.host} as 'verified' while {', '.join(missing)} "
                    "is still untested (record all Measurement 2 steps first, or pass --force-verified)."
                )
        entry.status = norm_status
    return entry


def render_matrix_markdown(
    entries: Iterable[HostEntry],
    *,
    canary_marker: str = DEFAULT_CANARY_MARKER,
) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    entry_list = list(entries)
    lines = [
        "# Live MCP Host Interoperability Matrix (Measurement 2)",
        "",
        f"- **Updated:** {now}",
        "- **Wire log hygiene:** `Authorization` and `Cookie` headers are stripped before logging.",
        "- **Verification rule:** No host is marked `verified` until Protocol Version, OAuth Registration (CIMD URL / DCR), and Instructions Visible are recorded.",
        "",
        MATRIX_HEADER,
        "|---|---|---|---|---|---|",
    ]
    for e in entry_list:
        lines.append(
            f"| {_escape_cell(e.host)} | {_escape_cell(e.tier)} | "
            f"{_escape_cell(e.protocol_version)} | {_escape_cell(e.registration)} | "
            f"{_escape_cell(e.instructions_visible)} | {_escape_cell(e.status)} |"
        )
    lines.extend(["", "## Observed Wire Sequences & Operator Notes", ""])
    for e in entry_list:
        seq = " -> ".join(e.method_sequence) if e.method_sequence else "none"
        note_suffix = f" | {e.notes}" if e.notes else ""
        lines.append(f"- **{e.host}:** wire=`{seq}`{note_suffix}")
    lines.extend(
        [
            "",
            "## Operator Steps per Host",
            "",
            "1. **Wire capture:** Point the host at the reverse proxy (`python -m trials.idp_check.host_probe --listen 127.0.0.1:8099 --upstream http://127.0.0.1:8022 --host <Host>`) to capture `MCP-Protocol-Version` (`2026-07-28` vs. `2025-11-25` vs. `2025-06-18`) and method sequence (`server/discover` vs. `initialize`).",
            "2. **Step (a) — OAuth registration mechanism:** Inspect identity provider logs and record the host's exact `https://` CIMD URL or `DCR` (dynamic client registration).",
            "3. **Step (b) — Standardised in-host canary prompt:** Send the prompt below in the host without invoking any tool to confirm whether `initialize.instructions` / `server/discover.instructions` reach the model:",
            "",
            f"> {CANARY_PROMPT_TEMPLATE.format(marker=canary_marker)}",
            "",
        ]
    )
    return "\n".join(lines)


def write_matrix(
    path: Path, entries: Iterable[HostEntry], *, canary_marker: str = DEFAULT_CANARY_MARKER
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_matrix_markdown(entries, canary_marker=canary_marker), encoding="utf-8")


def run_interactive_recorder(output: Path, log_file: Path, canary_marker: str) -> int:
    entries = merge_wire_summary(load_matrix(output), summarize_wire_log(log_file))
    print("Available hosts:")
    for idx, entry in enumerate(entries, start=1):
        seq = " -> ".join(entry.method_sequence) if entry.method_sequence else "none"
        print(
            f"  {idx}. {entry.host} ({entry.tier}) "
            f"[ver={entry.protocol_version}, reg={entry.registration}, "
            f"instr={entry.instructions_visible}, status={entry.status}, wire={seq}]"
        )
    choice = input("\nSelect host number or enter host name: ").strip()
    if not choice:
        return 0
    if choice.isdigit() and 1 <= int(choice) <= len(entries):
        target = entries[int(choice) - 1]
    else:
        target = update_host_entry(entries, host=choice)

    print(f"\nRecording Measurement 2 for {target.host} ({target.tier})")
    print("Standardised in-host canary prompt:")
    print(f"  {CANARY_PROMPT_TEMPLATE.format(marker=canary_marker)}\n")

    pv = input(
        f"MCP-Protocol-Version (2026-07-28 / 2025-11-25 / 2025-06-18) [{target.protocol_version}]: "
    ).strip()
    reg = input(
        f"Step (a) OAuth Registration from IdP logs (exact CIMD URL or DCR) [{target.registration}]: "
    ).strip()
    instr = input(
        f"Step (b) Instructions Visible from canary prompt (yes/no/partial/untested) [{target.instructions_visible}]: "
    ).strip()
    status = input(
        f"Host Status (verified/developer_fallback/deferred/failed/in_progress/untested) [{target.status}]: "
    ).strip()
    notes = input(f"Optional notes [{target.notes}]: ").strip()

    update_host_entry(
        entries,
        host=target.host,
        protocol_version=pv or target.protocol_version,
        registration=reg or target.registration,
        instructions_visible=instr or target.instructions_visible,
        status=status or target.status,
        notes=notes or target.notes,
    )
    write_matrix(output, entries, canary_marker=canary_marker)
    print(f"Updated {output} for {target.host}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trials.idp_check.host_probe",
        description=(
            "Run a credential-stripping MCP logging reverse proxy and record live host "
            "interoperability measurements into private-staging/idp-check/host-matrix.md."
        ),
    )
    parser.add_argument(
        "--listen",
        help="Proxy listen address as HOST:PORT (e.g. 127.0.0.1:8099).",
    )
    parser.add_argument(
        "--upstream",
        help="Upstream MCP server base URL (e.g. http://127.0.0.1:8022).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Host matrix markdown file (default: {DEFAULT_OUTPUT.relative_to(ROOT)}).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help=f"Credential-stripped wire JSONL log (default: {DEFAULT_LOG_FILE.relative_to(ROOT)}).",
    )
    parser.add_argument(
        "--host",
        help="Host label for proxy capture or target host for --record (e.g. 'Claude Code', 'Antigravity').",
    )
    parser.add_argument(
        "--tier",
        choices=("Tier 1", "Tier 2"),
        help="Tier label when recording a custom host.",
    )
    parser.add_argument(
        "--inject-canary",
        action="store_true",
        help=(
            "When proxying local/staging /mcp, append the canary policy marker to "
            "result.instructions on initialize / server/discover responses for Step (b)."
        ),
    )
    parser.add_argument(
        "--canary-marker",
        default=DEFAULT_CANARY_MARKER,
        help=f"Canary policy marker text (default: {DEFAULT_CANARY_MARKER}).",
    )
    parser.add_argument(
        "--canary-prompt",
        action="store_true",
        help="Print the standardised in-host canary prompt for Step (b) and exit.",
    )
    parser.add_argument(
        "--init-matrix",
        action="store_true",
        help="Initialize or refresh host-matrix.md with Tier-1 and Tier-2 hosts and exit.",
    )
    parser.add_argument(
        "--sync-log",
        action="store_true",
        help="Merge observed wire protocol versions and method sequences from --log-file into --output.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Record operator Step (a)/(b) results for --host into --output.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactively record Step (a) registration and Step (b) canary results into --output.",
    )
    parser.add_argument(
        "--protocol-version",
        help="Observed MCP-Protocol-Version for --record (e.g. 2026-07-28, 2025-11-25, 2025-06-18).",
    )
    parser.add_argument(
        "--registration",
        help="Step (a) OAuth registration mechanism from IdP logs (exact CIMD URL or DCR).",
    )
    parser.add_argument(
        "--instructions-visible",
        choices=VALID_INSTRUCTIONS,
        help="Step (b) canary prompt outcome: yes, no, partial, or untested.",
    )
    parser.add_argument(
        "--status",
        choices=VALID_STATUSES,
        help="Host matrix status (verified, developer_fallback, deferred, failed, in_progress, untested).",
    )
    parser.add_argument(
        "--method-sequence",
        help="Comma-separated JSON-RPC method sequence for --record (e.g. server/discover,tools/list).",
    )
    parser.add_argument(
        "--notes",
        help="Optional operator notes (e.g. IdP log reference or canary quote).",
    )
    parser.add_argument(
        "--force-verified",
        action="store_true",
        help="Allow setting --status verified even if some Measurement 2 columns are untested.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.canary_prompt:
        print(CANARY_PROMPT_TEMPLATE.format(marker=args.canary_marker))
        return 0

    if args.interactive:
        return run_interactive_recorder(args.output, args.log_file, args.canary_marker)

    if args.init_matrix or args.sync_log or args.record:
        entries = load_matrix(args.output)
        if args.sync_log or args.record:
            entries = merge_wire_summary(entries, summarize_wire_log(args.log_file))
        if args.record:
            if not args.host:
                parser.error("--record requires --host")
            methods = (
                [m.strip() for m in args.method_sequence.split(",") if m.strip()]
                if args.method_sequence
                else None
            )
            update_host_entry(
                entries,
                host=args.host,
                tier=args.tier,
                protocol_version=args.protocol_version,
                registration=args.registration,
                instructions_visible=args.instructions_visible,
                status=args.status,
                method_sequence=methods,
                notes=args.notes,
                force_verified=args.force_verified,
            )
        write_matrix(args.output, entries, canary_marker=args.canary_marker)
        print(f"Updated host matrix: {args.output}")
        return 0

    if not args.listen or not args.upstream:
        parser.error(
            "Provide both --listen HOST:PORT and --upstream URL to run the proxy, "
            "or use --record, --interactive, --sync-log, --init-matrix, or --canary-prompt."
        )

    server = create_proxy_server(
        args.listen,
        args.upstream,
        log_file=args.log_file,
        host_label=args.host,
        inject_canary=args.inject_canary,
        canary_marker=args.canary_marker,
    )
    host_addr, port = server.server_address
    print(
        f"MCP host probe proxy listening on http://{host_addr}:{port} -> {server.upstream}\n"
        f"Wire log (Authorization/Cookie stripped): {args.log_file}\n"
        f"Host matrix output: {args.output}\n"
        f"Canary marker: {args.canary_marker} (inject={'on' if args.inject_canary else 'off'})\n"
        "Press Ctrl+C to stop and sync observed wire versions/methods into the host matrix.",
        flush=True,
    )

    def _handle_stop(signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    prev_sigterm = signal.signal(signal.SIGTERM, _handle_stop)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        server.server_close()
        entries = merge_wire_summary(load_matrix(args.output), summarize_wire_log(args.log_file))
        write_matrix(args.output, entries, canary_marker=args.canary_marker)
        print(f"\nSynced wire observations into {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        raise SystemExit(str(error)) from None
