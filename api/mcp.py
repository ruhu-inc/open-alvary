"""Read-only, anonymous MCP endpoint over the public catalogue (JSON-RPC 2.0, Streamable HTTP).

Tools return exactly what the public REST API returns for the same request, under the same
current-policy checks and admission limits; no tool can reach private staging, unapproved text
or account data. Responses are single JSON bodies (no server-initiated streams).
"""

import json
import os
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from api.reach import EVERYWHERE, OutsidePlanLimited, Reach
from changes.feed import Feed, FeedError, Filters
from changes.routes import tokens

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
                "jurisdiction": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 40,
                    "description": "One jurisdiction to search, such as rw (Rwanda) or ng (Nigeria). "
                    "Without it, the search covers every jurisdiction available to you.",
                },
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
        "name": "list_changes",
        "title": "List catalogue changes",
        "description": "Changes to the catalogue in commit order: records added, corrected or withdrawn, and text "
        "published. Filter by jurisdiction, work, type, document class, language or keyword. Pass the "
        "returned resume_token next time to get only newer changes. Changes to what Open Alvary holds, not "
        "every change in a country's law.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jurisdiction": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "work": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "type": {"type": "array", "items": {"type": "string"}, "maxItems": 9},
                "class": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "language": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "q": {"type": "string", "maxLength": 200},
                "resume_token": {"type": "string", "maxLength": 2000},
                "cursor": {"type": "string", "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_change",
        "title": "Get one change",
        "description": "One catalogue change by its event_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"event_id": {"type": "string", "minLength": 36, "maxLength": 36}},
            "required": ["event_id"],
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


def changes_tool(conn, name, arguments):
    """The change feed as MCP tools: the same events and errors as GET /changes (changes-contracts)."""
    origin = "https://" + os.getenv("PUBLIC_DOMAIN", "open.alvary.ai")
    feed = Feed(conn, tokens(), origin)
    if name == "get_change":
        if set(arguments) != {"event_id"} or not isinstance(arguments["event_id"], str):
            return None
        return feed.event(arguments["event_id"])
    lists = ("jurisdiction", "work", "type", "class", "language")
    if not set(arguments) <= {*lists, "q", "resume_token", "cursor", "limit"}:
        return None
    if any(
        not isinstance(arguments.get(k, []), list)
        or not all(isinstance(v, str) for v in arguments.get(k, []))
        for k in lists
    ):
        return None
    limit = arguments.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
        return None
    values = {k: arguments[k] for k in lists if k in arguments}
    if isinstance(arguments.get("q"), str):
        values["q"] = [arguments["q"]]
    return feed.page(
        Filters.parse(values), limit, cursor=arguments.get("cursor"), resume=arguments.get("resume_token")
    )


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _tool_result(payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def create_router(
    catalogue_factory: Callable,
    unavailable: tuple[type[Exception], ...],
    reach: Callable[[Request], Reach] = lambda request: EVERYWHERE,
) -> APIRouter:
    """``reach`` gives the organisation's plan (api.reach): its countries at the plan's limits, every
    other jurisdiction at the Starter rate."""
    router = APIRouter()

    def call_tool(name, arguments, plan: Reach):
        if not isinstance(arguments, dict):
            return None
        with catalogue_factory() as service:
            if name == "search_catalogue" and set(arguments) <= {"query", "limit", "jurisdiction"}:
                query, limit = arguments.get("query"), arguments.get("limit", 10)
                jurisdiction = arguments.get("jurisdiction")
                if not isinstance(query, str) or not 1 <= len(query) <= 200:
                    return None
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
                    return None
                if jurisdiction is not None:
                    if not isinstance(jurisdiction, str) or not 2 <= len(jurisdiction) <= 40:
                        return None
                    jurisdiction = jurisdiction.lower()
                    plan.admit(jurisdiction)
                else:
                    jurisdiction = plan.within(j.id for j in service.jurisdictions())
                return service.search(query, jurisdiction=jurisdiction, limit=limit, offset=0).model_dump(
                    mode="json"
                )
            if name == "get_source" and set(arguments) == {"source_id"}:
                if not isinstance(arguments["source_id"], str) or not 1 <= len(arguments["source_id"]) <= 160:
                    return None
                view = service.source(arguments["source_id"])
                plan.admit(view.jurisdiction_id)
                return view.model_dump(mode="json")
            if name in ("list_changes", "get_change"):
                return changes_tool(service.repository.connection, name, arguments)
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
            except FeedError as refused:
                return _result(id_, _tool_result(f"{refused.code}: {refused.detail}", is_error=True))
            except OutsidePlanLimited as outside:
                return _result(
                    id_, _tool_result(f"{outside} Retry after {outside.retry_after} seconds.", is_error=True)
                )
            except LookupError:
                return _result(id_, _tool_result("Source not available", is_error=True))
            except ValueError:
                return _error(id_, -32602, "Invalid arguments")
            if payload is None:
                return _error(id_, -32602, "Unknown tool or invalid arguments")
            return _result(id_, _tool_result(payload))
        return _error(id_, -32601, "Method not found")

    return router
