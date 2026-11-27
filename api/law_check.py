"""Law check's public MCP server part (/agents/mcp, LC-1).

Accepts keyless JSON-RPC 2.0 requests from browsers on any origin (CORS *), answers handshake calls
directly with zero database work, applies per-visitor admission limits (ALVARY_AGENTS_ADMISSION_CONFIG,
route_class="agents"), narrows tools and bounds to Law check's five read-only tools, and invokes the
keyed CredentialGate in place under Law check's own service key (ALVARY_LAW_CHECK_KEY).

Inner responses and headers are never forwarded directly: only successes and an explicit allowlist of
tool error codes leave this boundary, and caller arguments are never logged.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Callable

from anyio import to_thread
from starlette.responses import JSONResponse, Response

from api.admission import (
    GLOBAL_KEY,
    AdmissionConfig,
    AdmissionStore,
    AdmissionUnavailable,
    client_address,
    network_group,
    pseudonym,
)

log = logging.getLogger("open_alvary.law_check")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
MAX_BODY_BYTES = 16 * 1024
MAX_ANCHOR_TEXT_BYTES = 8 * 1024
DEFAULT_MAX_UNITS = 5
MAX_UNITS = 5
MIN_EVIDENCE_BYTES = 4096
DEFAULT_MAX_EVIDENCE_BYTES = 12_288
MAX_EVIDENCE_BYTES = 12_288
PAUSE_CACHE_SECONDS = 5.0

SERVER_INFO = {
    "name": "open-alvary-law-check",
    "title": "Open Alvary Law check",
    "version": "0.1.0",
}

INSTRUCTIONS = (
    "Read-only Law check service over Open Alvary's covered legal texts. "
    "Use check_claim to verify quoted words against a published section and obtain a signed receipt. "
    "Information only, not legal advice."
)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept, MCP-Protocol-Version, Mcp-Session-Id",
    "Access-Control-Expose-Headers": "Mcp-Session-Id",
    "Access-Control-Max-Age": "86400",
    "Cache-Control": "no-store",
}

ALLOWED_TOOLS = frozenset(
    {
        "search_catalogue",
        "resolve_citation",
        "get_anchor",
        "retrieve_evidence",
        "check_claim",
    }
)

ALLOWED_ERROR_CODES = frozenset(
    {
        "rate_limited",
        "service_rate_limited",
        "unavailable",
        "temporarily_unavailable",
        "result_too_large",
        "quote_too_short",
        "quote_too_spread",
        "paused",
    }
)

LAW_CHECK_TOOLS = [
    {
        "name": "search_catalogue",
        "title": "Search covered laws",
        "description": "Search the legal records covered by Law check by words in their title, citation, "
        "summary or published text. Every word must match a whole word, in any order. Returns metadata, "
        "rights status and official links.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Words to find, such as a title or a citation.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "How many records to return, 1 to 25.",
                },
                "jurisdiction": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 40,
                    "description": "One jurisdiction to search, such as ng (Nigeria).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "resolve_citation",
        "title": "Resolve a citation",
        "description": "Resolve a citation to covered legal records and, for a part citation such as "
        "Freedom of Information Act 2011, s. 1(1), locate the part in the published text and return its "
        "anchor_id and exact span. Alternatives are listed with the reason, never chosen between.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "The citation, such as Freedom of Information Act 2011, s. 1(1).",
                },
                "as_of": {
                    "type": "string",
                    "format": "date",
                    "description": "Optional date YYYY-MM-DD for texts in force on that date.",
                },
                "language": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 35,
                    "description": "Optional language code, such as en.",
                },
            },
            "required": ["citation"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_anchor",
        "title": "Read one part of a published text",
        "description": "Read one part of a covered published text, such as a section or subsection, "
        "with its exact words, byte span, span_hash, citation and official link. Parts holding more than "
        "8 KiB of words are refused; name a smaller part.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "anchor_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "The part's anchor_id from resolve_citation or retrieve_evidence.",
                }
            },
            "required": ["anchor_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "retrieve_evidence",
        "title": "Find relevant passages in covered laws",
        "description": "Find ranked passages from covered published laws for a legal question, each with "
        "its exact words or an excerpt, anchor_id, citation, official link and recorded status. Returns at "
        "most 5 units and 12,288 bytes of words. Verify every quoted claim with check_claim.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "The legal question or citation to search for.",
                },
                "jurisdictions": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 2, "maxLength": 40},
                    "maxItems": 10,
                    "description": 'Optional jurisdictions to search, such as ["ng"].',
                },
                "language": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 35,
                    "description": "Optional language code, such as en.",
                },
                "as_of": {
                    "type": "string",
                    "format": "date",
                    "description": "Optional date YYYY-MM-DD.",
                },
                "source_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "Optional single covered record ID.",
                },
                "max_units": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_UNITS,
                    "description": f"How many passages, 1 to {MAX_UNITS}. Default is {DEFAULT_MAX_UNITS}.",
                },
                "max_evidence_bytes": {
                    "type": "integer",
                    "minimum": MIN_EVIDENCE_BYTES,
                    "maximum": MAX_EVIDENCE_BYTES,
                    "description": f"Maximum bytes of words, {MIN_EVIDENCE_BYTES:,} to "
                    f"{MAX_EVIDENCE_BYTES:,}. Default is {DEFAULT_MAX_EVIDENCE_BYTES:,}.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["applicable"],
                    "description": "Optional applicable-law scope for a single jurisdiction.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "check_claim",
        "title": "Check quoted words and issue a signed receipt",
        "description": "Deterministically checks whether quoted words appear in the named section of a "
        "covered text Open Alvary publishes and what Open Alvary's record lists about its status, "
        "returning a publisher-signed oa-receipt-1 receipt beside the claim. Does not give legal advice "
        "or judge whether a section supports a paraphrase.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "Citation such as 'Freedom of Information Act 2011, s. 1(1)'. Send "
                    "citation or anchor_id, never both.",
                },
                "anchor_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "Part anchor_id when already held. Send citation or anchor_id, never "
                    "both.",
                },
                "quote": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "Optional exact words the claim says the law uses; at least 40 "
                    "characters after normalisation unless the quote is the whole part.",
                },
                "as_of": {
                    "type": "string",
                    "format": "date",
                    "description": "Optional assessment date YYYY-MM-DD (not after today in UTC).",
                },
                "jurisdiction": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 40,
                    "description": "Optional jurisdiction such as ng; checks whether the record is "
                    "recorded as binding there.",
                },
                "language": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 35,
                    "description": "Optional language code, such as en.",
                },
                "reading": {
                    "type": "string",
                    "maxLength": 600,
                    "description": "Optional sentence this check supports. Echoed beside the receipt; "
                    "not checked, not signed, never logged.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


# Law check's own words for the refusals it passes on. The keyed MCP's texts speak to an app that holds a
# key (REST paths to read instead, the console, a plan's countries); a visitor has none of those.
BUSY = "Law check is busy right now. Try again in {seconds} seconds."
NOT_COVERED = (
    "Open Alvary does not publish this text, or this part of it. "
    "The published texts are listed at https://open.alvary.ai/browse"
)
CANNOT_ANSWER = "Law check cannot answer right now. Try again shortly."
TOO_LARGE = {
    "get_anchor": "this part holds more than 8 KiB of words; name a smaller part.",
    "check_claim": "this part is too large to check at once; name a smaller part.",
}
TOO_LARGE_DEFAULT = "the answer would be larger than Law check returns at once; ask for less."
# The verifier's own refusals are Law check's words already, and say what to change in the quote.
VERIFIER_CODES = frozenset({"quote_too_short", "quote_too_spread"})


def _encodable(value) -> bool:
    """Whether ``value`` can be written as UTF-8 JSON. json.loads accepts a lone surrogate escape
    (``"\\ud800"``), which no response and no inner request can carry."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError):
        return False
    return True


def _rpc_error(id_, code: int, message: str, data: dict | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _rpc_result(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _tool_error(code: str, message: str, retry_after: int | None = None, extra: dict | None = None) -> dict:
    text = message if message.startswith(f"{code}:") else f"{code}: {message}"
    structured: dict = {"code": code}
    if retry_after is not None:
        structured["retry_after_seconds"] = retry_after
    if extra:
        for k, v in extra.items():
            if k not in ("code", "retry_after_seconds"):
                structured[k] = v
    return {
        "content": [{"type": "text", "text": text}],
        "isError": True,
        "structuredContent": structured,
    }


def _json_response(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=CORS_HEADERS)


class LawCheckMCP:
    """ASGI application mounted at /agents/mcp."""

    def __init__(
        self,
        gate=None,
        *,
        admission_config: AdmissionConfig | None = None,
        admission_store: AdmissionStore | None = None,
        key_provider: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        require_admission: bool = True,
    ):
        self.gate = gate
        self.admission_config = admission_config
        self.admission_store = admission_store
        self.key_provider = key_provider or (lambda: os.getenv("ALVARY_LAW_CHECK_KEY"))
        self.clock = clock
        self.require_admission = require_admission
        self.route_class = "agents"
        self._waiting = 0
        self._paused_until = 0.0

    def _admission_key(self, raw_key: str) -> str:
        return f"{self.route_class}:{raw_key.replace(':', '-')}"

    def _admission_request(self, scope) -> dict | None:
        config = self.admission_config
        if config is None:
            return None
        peer = scope.get("client")[0] if scope.get("client") else None
        address = client_address(scope.get("headers", []), peer, config.trusted_proxies)
        try:
            group = network_group(address) if address else None
        except ValueError:
            group = None
        if group is None:
            return None
        keys = [self._admission_key(pseudonym(config.hmac_secret, group))]
        if config.previous_hmac_secret:
            keys.append(self._admission_key(pseudonym(config.previous_hmac_secret, group)))
        limits = {"capacity": config.capacity, "refill_per_minute": config.refill_per_minute}
        return {
            "buckets": [{"key": k, **limits} for k in keys],
            "concurrency": [
                {"key": keys[0], "limit": config.identity_concurrency},
                {"key": self._admission_key(GLOBAL_KEY), "limit": config.global_concurrency},
            ],
            "lease_seconds": config.lease_seconds,
        }

    async def _admit_visitor(self, scope) -> tuple[str | None, dict | None]:
        """Returns (lease_id, error_tool_result). If error_tool_result is not None, refuse."""
        config, store = self.admission_config, self.admission_store
        if config is None or store is None:
            return None, None
        request = self._admission_request(scope)
        if request is None:
            return None, _tool_error(
                "temporarily_unavailable", "Client address unavailable; try again shortly."
            )
        global_key = self._admission_key(GLOBAL_KEY)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.queue_deadline_seconds
        queued, delay = False, 0.02
        try:
            while True:
                try:
                    result = await to_thread.run_sync(store.admit, request)
                except AdmissionUnavailable:
                    return None, _tool_error(
                        "temporarily_unavailable",
                        "Law check cannot check limits right now. Try again shortly.",
                    )
                if result["admitted"]:
                    return result["lease_id"], None
                if set(result["blocked"]) != {global_key}:
                    retry = max(1, int(result.get("retry_after") or 1))
                    return None, _tool_error(
                        "rate_limited",
                        f"rate_limited: try again in {retry} seconds.",
                        retry_after=retry,
                    )
                if not queued:
                    if self._waiting >= config.queue_size:
                        return None, _tool_error(
                            "temporarily_unavailable", "Law check is busy right now. Try again shortly."
                        )
                    self._waiting += 1
                    queued = True
                if loop.time() + delay > deadline:
                    return None, _tool_error(
                        "temporarily_unavailable", "Law check is busy right now. Try again shortly."
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
        finally:
            if queued:
                self._waiting -= 1

    async def _release_visitor(self, lease_id: str | None) -> None:
        if lease_id is None or self.admission_store is None:
            return
        try:
            await to_thread.run_sync(self.admission_store.release, lease_id)
        except AdmissionUnavailable:
            pass

    @staticmethod
    async def _read_body(scope, receive) -> tuple[bytes | None, bool]:
        """Reads body up to MAX_BODY_BYTES. Returns (body, too_large)."""
        declared = [v for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
        if declared:
            try:
                size = int(declared[0])
            except ValueError:
                return None, True
            if size > MAX_BODY_BYTES or len(declared) > 1:
                return None, True
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return None, True
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks), False

    def _log_call(
        self, request_id: str, tool: str | None, outcome: str, response_bytes: int, started: float
    ) -> None:
        log.info(
            json.dumps(
                {
                    "event": "law_check_mcp",
                    "request_id": request_id,
                    "tool": tool,
                    "outcome": outcome,
                    "bytes": response_bytes,
                    "duration_ms": round((self.clock() - started) * 1000.0, 2),
                },
                separators=(",", ":"),
            )
        )

    async def _invoke_gate(self, id_, tool_name: str, arguments: dict, key: str) -> tuple[int, dict, bytes]:
        """Calls the keyed CredentialGate in place with a synthetic ASGI scope."""
        inner_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": id_,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        inner_scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", b"law-check.internal"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(inner_body)).encode("ascii")),
                (b"authorization", f"Bearer {key}".encode("latin-1")),
            ],
            "client": None,
            "server": ("law-check.internal", 443),
            "state": {"law_check_internal": True},
        }
        body_sent = False

        async def inner_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": inner_body, "more_body": False}
            return {"type": "http.disconnect"}

        status_code = 500
        headers_map: dict[str, str] = {}
        chunks: list[bytes] = []

        async def inner_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                for k, v in message.get("headers", []):
                    headers_map[k.decode("latin-1").lower()] = v.decode("latin-1")
            elif message["type"] == "http.response.body":
                part = message.get("body", b"")
                if part:
                    chunks.append(part)

        await self.gate(inner_scope, inner_receive, inner_send)
        return status_code, headers_map, b"".join(chunks)

    def _translate_inner(
        self, id_, tool_name: str, status: int, headers: dict[str, str], body: bytes
    ) -> tuple[dict, str]:
        """Builds Law check's own JSON-RPC response from the inner gate response. Returns (payload, outcome)."""
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {}

        if status == 429:
            retry = None
            if "retry-after" in headers:
                try:
                    retry = int(headers["retry-after"])
                except ValueError:
                    retry = None
            if retry is None and isinstance(parsed, dict):
                data = (parsed.get("error") or {}).get("data")
                if isinstance(data, dict) and isinstance(data.get("retry_after_seconds"), int):
                    retry = data["retry_after_seconds"]
            retry = max(1, retry or 1)
            return (
                _rpc_result(
                    id_,
                    _tool_error(
                        "service_rate_limited",
                        f"service_rate_limited: {BUSY.format(seconds=retry)}",
                        retry_after=retry,
                    ),
                ),
                "service_rate_limited",
            )

        if status in (401, 403):
            self._paused_until = self.clock() + PAUSE_CACHE_SECONDS
            return (
                _rpc_result(id_, _tool_error("paused", "paused: Law check is paused. Try again later.")),
                "paused",
            )

        if status != 200 or not isinstance(parsed, dict):
            return (
                _rpc_result(
                    id_,
                    _tool_error(
                        "temporarily_unavailable",
                        "temporarily_unavailable: Law check cannot answer right now. Try again shortly.",
                    ),
                ),
                "temporarily_unavailable",
            )

        if "error" in parsed:
            err = parsed["error"]
            if isinstance(err, dict) and err.get("code") == -32602:
                return _rpc_error(
                    id_, -32602, str(err.get("message") or "Invalid arguments")
                ), "invalid_params"
            return (
                _rpc_result(
                    id_,
                    _tool_error(
                        "temporarily_unavailable",
                        "temporarily_unavailable: Law check cannot answer right now. Try again shortly.",
                    ),
                ),
                "temporarily_unavailable",
            )

        result = parsed.get("result")
        if not isinstance(result, dict):
            return (
                _rpc_result(
                    id_,
                    _tool_error(
                        "temporarily_unavailable",
                        "temporarily_unavailable: Law check cannot answer right now. Try again shortly.",
                    ),
                ),
                "temporarily_unavailable",
            )

        if result.get("isError"):
            structured = (
                result.get("structuredContent") if isinstance(result.get("structuredContent"), dict) else {}
            )
            code = structured.get("code")
            if code not in ALLOWED_ERROR_CODES:
                return (
                    _rpc_result(
                        id_,
                        _tool_error(
                            "temporarily_unavailable",
                            "temporarily_unavailable: Law check cannot answer right now. Try again shortly.",
                        ),
                    ),
                    "temporarily_unavailable",
                )
            retry_after = structured.get("retry_after_seconds")
            if not isinstance(retry_after, int) or isinstance(retry_after, bool):
                retry_after = None
            if code == "rate_limited":
                # The plan's own limit (for example its rate outside its countries), never this visitor's:
                # the per-visitor limit is answered before the gate is called.
                retry = max(1, retry_after or 1)
                return (
                    _rpc_result(
                        id_,
                        _tool_error(
                            "service_rate_limited",
                            f"service_rate_limited: {BUSY.format(seconds=retry)}",
                            retry_after=retry,
                        ),
                    ),
                    "service_rate_limited",
                )
            if code in VERIFIER_CODES:
                content = result.get("content")
                text = ""
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    text = str(content[0].get("text") or "")
            elif code == "result_too_large":
                text = TOO_LARGE.get(tool_name, TOO_LARGE_DEFAULT)
            elif code == "unavailable":
                text = NOT_COVERED
            else:
                text = CANNOT_ANSWER
            # Nothing else of the inner answer is passed on: its text and any further fields stay inside.
            return _rpc_result(id_, _tool_error(code, text or code, retry_after=retry_after)), code

        # Success: apply Law check's 8 KiB words cap on get_anchor.
        if tool_name == "get_anchor":
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                try:
                    anchor_data = json.loads(content[0].get("text", ""))
                except ValueError:
                    anchor_data = None
                if isinstance(anchor_data, dict) and isinstance(anchor_data.get("text"), str):
                    if len(anchor_data["text"].encode("utf-8")) > MAX_ANCHOR_TEXT_BYTES:
                        return (
                            _rpc_result(
                                id_,
                                _tool_error("result_too_large", TOO_LARGE["get_anchor"]),
                            ),
                            "result_too_large",
                        )

        return _rpc_result(id_, result), "ok"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return

        method = scope.get("method", "GET").upper()
        if method == "OPTIONS":
            response = Response(status_code=204, headers=CORS_HEADERS)
            return await response(scope, receive, send)

        started = self.clock()
        request_id = uuid.uuid4().hex[:16]

        async def finish(response: Response, tool: str | None, outcome: str):
            body_len = len(getattr(response, "body", b"") or b"")
            self._log_call(request_id, tool, outcome, body_len, started)
            await response(scope, receive, send)

        if method != "POST":
            return await finish(
                Response(status_code=405, headers={**CORS_HEADERS, "Allow": "POST, OPTIONS"}),
                None,
                "method_not_allowed",
            )

        # Refuse any request carrying Authorization (never accept credentials at this address).
        if any(k.lower() == b"authorization" for k, _ in scope.get("headers", [])):
            return await finish(
                _json_response(
                    _rpc_error(
                        None,
                        -32600,
                        "credential_not_accepted: Law check takes no credentials; do not send an Authorization header.",
                        {"code": "credential_not_accepted"},
                    ),
                    status_code=400,
                ),
                None,
                "credential_not_accepted",
            )

        raw_body, too_large = await self._read_body(scope, receive)
        if too_large or raw_body is None:
            return await finish(
                _json_response(
                    _rpc_error(
                        None,
                        -32600,
                        f"request_too_large: request body exceeds {MAX_BODY_BYTES:,} bytes.",
                        {"code": "request_too_large"},
                    ),
                    status_code=413,
                ),
                None,
                "request_too_large",
            )

        try:
            message = json.loads(raw_body)
        except ValueError:
            return await finish(
                _json_response(_rpc_error(None, -32700, "Parse error"), status_code=400),
                None,
                "parse_error",
            )

        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return await finish(
                _json_response(_rpc_error(None, -32600, "Invalid request"), status_code=400),
                None,
                "invalid_request",
            )

        # A lone surrogate anywhere in the message (json.loads accepts the escape) can be written neither
        # in an answer nor in the inner request: refuse it here, before any answer echoes the id.
        if not _encodable(message):
            return await finish(
                _json_response(_rpc_error(None, -32600, "Invalid request"), status_code=400),
                None,
                "invalid_request",
            )

        rpc_method = message["method"]
        id_ = message.get("id")

        # Notifications: answered directly with 202 and zero database work.
        if id_ is None:
            return await finish(Response(status_code=202, headers=CORS_HEADERS), None, "notification")

        params = message.get("params") or {}

        # Handshake calls: zero database work, zero per-visitor limit, succeed even when paused.
        if rpc_method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return await finish(
                _json_response(
                    _rpc_result(
                        id_,
                        {
                            "protocolVersion": version,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": SERVER_INFO,
                            "instructions": INSTRUCTIONS,
                        },
                    )
                ),
                None,
                "initialize",
            )

        if rpc_method == "ping":
            return await finish(_json_response(_rpc_result(id_, {})), None, "ping")

        if rpc_method == "tools/list":
            return await finish(
                _json_response(_rpc_result(id_, {"tools": LAW_CHECK_TOOLS})),
                None,
                "tools_list",
            )

        if rpc_method != "tools/call":
            return await finish(
                _json_response(_rpc_error(id_, -32601, "Method not found")),
                None,
                "method_not_found",
            )

        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return await finish(
                _json_response(_rpc_error(id_, -32602, "Invalid params")),
                None,
                "invalid_params",
            )

        tool_name = params["name"]
        raw_args = params.get("arguments", {})
        if tool_name not in ALLOWED_TOOLS:
            # The name is the caller's own text (up to the body cap): it is neither logged nor sent back.
            return await finish(
                _json_response(_rpc_error(id_, -32602, "Unknown tool")),
                None,
                "unknown_tool",
            )
        if not isinstance(raw_args, dict):
            return await finish(
                _json_response(_rpc_error(id_, -32602, "Invalid arguments")),
                tool_name,
                "invalid_params",
            )

        # Narrow Law check bounds and fill defaults before admission or gate.
        arguments = dict(raw_args)
        if tool_name == "retrieve_evidence":
            if "max_units" in arguments:
                units = arguments["max_units"]
                if not isinstance(units, int) or isinstance(units, bool) or not 1 <= units <= MAX_UNITS:
                    return await finish(
                        _json_response(
                            _rpc_error(id_, -32602, f"max_units must be an integer between 1 and {MAX_UNITS}")
                        ),
                        tool_name,
                        "invalid_params",
                    )
            else:
                arguments["max_units"] = DEFAULT_MAX_UNITS

            if "max_evidence_bytes" in arguments:
                budget = arguments["max_evidence_bytes"]
                if (
                    not isinstance(budget, int)
                    or isinstance(budget, bool)
                    or not MIN_EVIDENCE_BYTES <= budget <= MAX_EVIDENCE_BYTES
                ):
                    return await finish(
                        _json_response(
                            _rpc_error(
                                id_,
                                -32602,
                                f"max_evidence_bytes must be an integer between {MIN_EVIDENCE_BYTES} and {MAX_EVIDENCE_BYTES}",
                            )
                        ),
                        tool_name,
                        "invalid_params",
                    )
            else:
                arguments["max_evidence_bytes"] = DEFAULT_MAX_EVIDENCE_BYTES

        # Missing key, missing gate, missing admission config (when required), or active pause cache -> paused.
        key = self.key_provider()
        if (
            self.gate is None
            or not key
            or (self.require_admission and self.admission_config is None)
            or self.clock() < self._paused_until
        ):
            return await finish(
                _json_response(
                    _rpc_result(id_, _tool_error("paused", "paused: Law check is paused. Try again later."))
                ),
                tool_name,
                "paused",
            )

        # Per-visitor admission.
        lease_id, refused_tool_error = await self._admit_visitor(scope)
        if refused_tool_error is not None:
            outcome = refused_tool_error["structuredContent"]["code"]
            return await finish(_json_response(_rpc_result(id_, refused_tool_error)), tool_name, outcome)

        try:
            inner_status, inner_headers, inner_body = await self._invoke_gate(id_, tool_name, arguments, key)
        finally:
            await self._release_visitor(lease_id)

        payload, outcome = self._translate_inner(id_, tool_name, inner_status, inner_headers, inner_body)
        return await finish(_json_response(payload), tool_name, outcome)
