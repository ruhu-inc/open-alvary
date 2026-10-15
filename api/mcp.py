"""Read-only, anonymous MCP endpoint over the public catalogue (JSON-RPC 2.0, Streamable HTTP).

Tools return exactly what the public REST API returns for the same request, under the same
current-policy checks and admission limits; no tool can reach private staging, unapproved text
or account data. Responses are single JSON bodies (no server-initiated streams).
"""

import json
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "open-alvary", "title": "Open Alvary public legal catalogue", "version": "0.1.0"}

TOOLS = [
    {
        "name": "search_catalogue",
        "title": "Search the catalogue",
        "description": "Search catalogued legal sources by title or citation. Returns metadata, rights status and official links.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_source",
        "title": "Get one source",
        "description": "Get one catalogued source by its ID, including rights status and access mode.",
        "inputSchema": {
            "type": "object",
            "properties": {"source_id": {"type": "string", "minLength": 1, "maxLength": 160}},
            "required": ["source_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_coverage",
        "title": "Catalogue coverage",
        "description": "Counts of catalogued sources and of sources with openly redistributable full text.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _tool_result(payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class OutsideAccess(LookupError):
    """A record or jurisdiction outside the organisation's access (MAC-06 jurisdiction_not_included)."""


def create_router(
    catalogue_factory: Callable,
    unavailable: tuple[type[Exception], ...],
    reach: Callable[[Request], set[str] | None] = lambda request: None,
) -> APIRouter:
    """``reach`` gives the jurisdictions a request may read (None: all), from its organisation's access."""
    router = APIRouter()

    def call_tool(name, arguments, allowed):
        if not isinstance(arguments, dict):
            return None
        with catalogue_factory() as service:
            if name == "search_catalogue" and set(arguments) <= {"query", "limit"}:
                query, limit = arguments.get("query"), arguments.get("limit", 10)
                if not isinstance(query, str) or not 1 <= len(query) <= 200:
                    return None
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
                    return None
                if allowed is not None and len(allowed) != 1:
                    raise OutsideAccess("Your access covers several jurisdictions; search one at a time.")
                jurisdiction = next(iter(allowed)) if allowed else None
                return service.search(query, jurisdiction=jurisdiction, limit=limit, offset=0).model_dump(
                    mode="json"
                )
            if name == "get_source" and set(arguments) == {"source_id"}:
                if not isinstance(arguments["source_id"], str) or not 1 <= len(arguments["source_id"]) <= 160:
                    return None
                view = service.source(arguments["source_id"])
                if allowed is not None and view.jurisdiction_id not in allowed:
                    raise OutsideAccess("That source is outside your organisation's access.")
                return view.model_dump(mode="json")
            if name == "get_coverage" and not arguments:
                return service.coverage().model_dump(mode="json")
        return None

    @router.post("/mcp")
    async def mcp(request: Request):
        origin = request.headers.get("origin")
        # Refuse cross-origin browser calls: the Origin host must equal the requested Host.
        # (TLS ends at the edge proxy, so the scheme is not compared.) Non-browser clients send none.
        if origin is not None and urlsplit(origin).netloc != request.headers.get("host", ""):
            return JSONResponse(status_code=403, content=_error(None, -32600, "Origin not allowed"))
        try:
            message = json.loads(await request.body())
        except ValueError:
            return JSONResponse(status_code=400, content=_error(None, -32700, "Parse error"))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return JSONResponse(status_code=400, content=_error(None, -32600, "Invalid request"))
        method, id_ = message["method"], message.get("id")
        if id_ is None:
            return Response(status_code=202)  # notifications (for example notifications/initialized)
        params = message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": "Read-only public catalogue. Metadata is not permission to reuse linked documents; "
                    "check each source's rights status.",
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": TOOLS})
        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return _error(id_, -32602, "Invalid params")
            try:
                # Database work runs in a worker thread, never on the event loop.
                payload = await run_in_threadpool(
                    call_tool, params["name"], params.get("arguments", {}), reach(request)
                )
            except unavailable:
                return _result(id_, _tool_result("Catalogue authority unavailable", is_error=True))
            except OutsideAccess as outside:
                return _result(id_, _tool_result(str(outside), is_error=True))
            except LookupError:
                return _result(id_, _tool_result("Source not available", is_error=True))
            except ValueError:
                return _error(id_, -32602, "Invalid arguments")
            if payload is None:
                return _error(id_, -32602, "Unknown tool or invalid arguments")
            return _result(id_, _tool_result(payload))
        return _error(id_, -32601, "Method not found")

    return router
