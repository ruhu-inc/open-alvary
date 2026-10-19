"""Read-only, anonymous MCP endpoint over the public catalogue (JSON-RPC 2.0, Streamable HTTP).

Tools return exactly what the public REST API returns for the same request, under the same
current-policy checks and admission limits; no tool can reach private staging, unapproved text
or account data. Responses are single JSON bodies (no server-initiated streams).
"""

import json
import os
import re
from collections.abc import Callable
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from api import text_index
from api.reach import EVERYWHERE, OutsidePlanLimited, Reach
from api.repositories import EDGE_ORDER
from changes.feed import Feed, FeedError, Filters
from changes.routes import tokens
from retrieval.check import Checker
from retrieval.pack import Request as EvidenceRequest
from retrieval.pack import Retriever
from schema.public import CheckRequest

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
# Tool answers stay inside the MCP payload budget (BUD-01: 64 KiB), never shortened.
MAX_TOOL_BYTES = 60 * 1024


class TooLarge(Exception):
    pass


SERVER_INFO = {"name": "open-alvary", "title": "Open Alvary public legal catalogue", "version": "0.1.0"}

TOOLS = [
    {
        "name": "search_catalogue",
        "title": "Search the catalogue",
        "description": "Search catalogued legal sources by words in their title, citation or cleared full text. "
        "Returns metadata, rights status and official links.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Words to find, such as a title, a citation or a phrase. Case doesn't matter.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "How many sources to return, 1 to 25.",
                },
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
            "properties": {
                "source_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "The source's ID, such as ng-foi-act-2011, from a search.",
                }
            },
            "required": ["source_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_relationships",
        "title": "Get a source's history",
        "description": "Whether a source is current and how it relates to other public sources, one step "
        "away: what it repeals or amends and the law it is made under, what repealed or amended it, the "
        "instruments made under it, and the source in force in its place now when it has been repealed. "
        "Only links a reviewer has checked against the official text. A page at a time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "The source's ID, such as ng-isa-2007, from a search.",
                },
                "relation_type": {
                    "type": "string",
                    "enum": list(EDGE_ORDER),
                    "description": "Only links of this kind, such as repealed_by or enables.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Links per page, 1 to 100. The default is 25.",
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "The next_cursor of the previous page, to continue it.",
                },
            },
            "required": ["source_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_anchor",
        "title": "Get one part of a text exactly",
        "description": "One part of a published text, such as a section or article, exactly: its words, its "
        "span as UTF-8 byte offsets, the hash of its bytes for checking a quotation, a citation such as "
        "s. 23(1)(a), the official file and the attribution its rights record asks for. Never shortened: a "
        "part too large for one answer is refused; read it from the REST text route with its offsets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "anchor_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "The part's ID, from a text's structure.",
                }
            },
            "required": ["anchor_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "search_text",
        "title": "Search published texts",
        "description": "Search the words of published legal texts. Returns matching passages (sections, articles, "
        "schedule paragraphs), best first, each with an exact snippet, a citation such as s. 23(1)(a), the "
        "part's anchor_id for get_anchor and the record's legal status. Only published texts are searched: "
        "coverage says how many records in scope have one, and status tells no_result apart from a scope "
        "with no published text (unsupported_scope) and a search that could not run (degraded).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": 'Words to find. "exact phrase" in quotes, -word to leave out, OR between '
                    "alternatives. A citation of a part, such as NDPA s. 23(1)(a), puts that part first.",
                },
                "jurisdiction": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 40,
                    "description": "One jurisdiction to search, such as ng (Nigeria). Without it, every "
                    "jurisdiction available to you.",
                },
                "language": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 35,
                    "description": "Only texts in this language, such as en or fr.",
                },
                "as_of": {
                    "type": "string",
                    "format": "date",
                    "description": "Only texts in force on this date, YYYY-MM-DD. Texts with no effective "
                    "date are left out.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "How many passages, 1 to 10. The default is 5.",
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "The next_cursor of the previous answer, for more passages.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "retrieve_evidence",
        "title": "Retrieve legal evidence",
        "description": "Ranked legal evidence for a question, to cite in an answer: each unit is a provision "
        "(or a law's title and preamble) with its exact words or an excerpt, its anchor_id, byte span and "
        "span_hash, its citation such as s. 23(1)(a), what quoting it asks for, whether it was in force "
        "(currentness, with what that rests on), the definitions it relies on and why it ranks there. A "
        "question that cites a part, names a law or asks what a term means is routed to that first. Quote "
        "only the words given; check cited parts with check_currentness before showing an answer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "The question, such as: when must a data controller report a breach in Nigeria?",
                },
                "jurisdictions": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 2, "maxLength": 40},
                    "maxItems": 10,
                    "description": 'Jurisdictions to search, such as ["ng"]. With two or more, results '
                    "alternate between them. Without it, every jurisdiction available to you.",
                },
                "language": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 35,
                    "description": "Only texts in this language, such as en or fr.",
                },
                "as_of": {
                    "type": "string",
                    "format": "date",
                    "description": "Only texts in force on this date, YYYY-MM-DD, with currentness assessed "
                    "at it. Without it, nothing is assumed about the date.",
                },
                "max_units": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 15,
                    "description": "How many units, 1 to 15. The default is 8.",
                },
                "max_evidence_bytes": {
                    "type": "integer",
                    "minimum": 4096,
                    "maximum": 40960,
                    "description": "The most bytes of words, 4,096 to 40,960. The default is 24,576.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "check_currentness",
        "title": "Check cited parts",
        "description": "Before showing an answer that cites the corpus: for each cited part, whether it is "
        "still published with the same words (unchanged), whether a newer text of the same edition exists "
        "(newer_version, with the same part's anchor_id there and whether its words changed), whether the "
        "span_hash sent is wrong (span_mismatch) or the part is not public (unavailable); and whether it "
        "was in force on a date.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "anchor_id": {"type": "string", "minLength": 1, "maxLength": 160},
                            "version_id": {"type": "string", "minLength": 1, "maxLength": 160},
                            "locator": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "span_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "as_of": {"type": "string", "format": "date"},
                        },
                        "additionalProperties": False,
                    },
                    "description": "The parts: each an anchor_id, or a version_id with a locator, with the "
                    "span_hash you hold and an optional as_of date.",
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "resolve_citation",
        "title": "Resolve a citation",
        "description": "The records a citation names and, for a citation of a part such as NDPA s. 23(1)(a), "
        "art. 12 or First Schedule, para. 3, the part in the record's public text: its anchor_id for "
        "get_anchor and its exact span. Alternatives (two languages, two texts, two records) are listed with "
        "the reason, never chosen between.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "The citation, such as FOI Act 2011, s. 1(1).",
                },
                "as_of": {
                    "type": "string",
                    "format": "date",
                    "description": "Look for the part only in texts in force on this date, YYYY-MM-DD.",
                },
                "language": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 35,
                    "description": "Look for the part only in texts in this language, such as en.",
                },
            },
            "required": ["citation"],
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
                "jurisdiction": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                    "description": "Jurisdiction IDs, such as ng or rw; a country includes its states.",
                },
                "work": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                    "description": "Source IDs, to follow particular laws.",
                },
                "type": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 9,
                    "description": "Kinds of change: catalogue_added, metadata_corrected, "
                    "text_version_published, withdrawal, coverage_changed.",
                },
                "class": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                    "description": "Document types, such as act or regulation.",
                },
                "language": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                    "description": "Languages of the affected text, such as en.",
                },
                "q": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Whole words in the title, citation or summary.",
                },
                "resume_token": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "The resume_token of an earlier answer, for only newer changes.",
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "The next_cursor of the previous page, to continue it.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Changes per page, 1 to 25.",
                },
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
            "properties": {
                "event_id": {
                    "type": "string",
                    "minLength": 36,
                    "maxLength": 36,
                    "description": "The change's event_id, from list_changes.",
                }
            },
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


LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")


def _date(value) -> date | None | bool:
    """A YYYY-MM-DD argument as a date; None when absent; False when not a date."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else False
    except ValueError:
        return False


def _language(value) -> str | None | bool:
    if value is None:
        return None
    return value if isinstance(value, str) and LANGUAGE.match(value) and len(value) <= 35 else False


def _bounded(payload: dict) -> dict:
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_TOOL_BYTES:
        raise TooLarge("result_too_large: ask for fewer passages")
    return payload


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
            if name == "get_relationships" and {"source_id"} <= set(arguments) <= {
                "source_id",
                "relation_type",
                "limit",
                "cursor",
            }:
                source_id, kind = arguments["source_id"], arguments.get("relation_type")
                limit, cursor = arguments.get("limit", 25), arguments.get("cursor")
                if not isinstance(source_id, str) or not 1 <= len(source_id) <= 160:
                    return None
                if kind is not None and kind not in EDGE_ORDER:
                    return None
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                    return None
                if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 200):
                    return None
                plan.admit(service.source(source_id).jurisdiction_id)
                return service.relationships(
                    source_id, kinds=[kind] if kind else None, limit=limit, cursor=cursor
                ).model_dump(mode="json")
            if name == "get_anchor" and set(arguments) == {"anchor_id"}:
                anchor_id = arguments["anchor_id"]
                if not isinstance(anchor_id, str) or not 1 <= len(anchor_id) <= 160:
                    return None
                view = service.anchor(anchor_id)
                plan.admit(service.source(view.source_id).jurisdiction_id)
                payload = view.model_dump(mode="json")
                if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_TOOL_BYTES:
                    raise TooLarge(
                        "result_too_large: this part is larger than one answer may be; read bytes "
                        f"{view.start_byte} to {view.end_byte} of /api/v1/versions/{view.version_id}/text"
                    )
                return payload
            if name == "search_text" and {"query"} <= set(arguments) <= {
                "query",
                "jurisdiction",
                "language",
                "as_of",
                "limit",
                "cursor",
            }:
                query, limit, cursor = arguments["query"], arguments.get("limit", 5), arguments.get("cursor")
                as_of, language = _date(arguments.get("as_of")), _language(arguments.get("language"))
                jurisdiction = arguments.get("jurisdiction")
                if (
                    not isinstance(query, str)
                    or not 1 <= len(query) <= 2000
                    or as_of is False
                    or language is False
                ):
                    return None
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
                    return None
                if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 200):
                    return None
                if jurisdiction is not None:
                    if not isinstance(jurisdiction, str) or not 2 <= len(jurisdiction) <= 40:
                        return None
                    plan.admit(jurisdiction.lower())
                    jurisdictions = (jurisdiction.lower(),)
                else:
                    scope = plan.within(j.id for j in service.jurisdictions())
                    jurisdictions = None if scope is None else tuple(sorted(scope))
                filters = text_index.Filters(jurisdictions=jurisdictions, language=language, as_of=as_of)
                page = service.search_text(query, filters, limit=limit, cursor=cursor)
                return _bounded(page.model_dump(mode="json"))
            if name == "resolve_citation" and {"citation"} <= set(arguments) <= {
                "citation",
                "as_of",
                "language",
            }:
                citation = arguments["citation"]
                as_of, language = _date(arguments.get("as_of")), _language(arguments.get("language"))
                if (
                    not isinstance(citation, str)
                    or not 1 <= len(citation) <= 300
                    or as_of is False
                    or language is False
                ):
                    return None
                view = service.citations(citation, as_of=as_of, language=language)
                plan.admit(*(m.jurisdiction_id for m in view.matches))
                return _bounded(view.model_dump(mode="json"))
            if name == "retrieve_evidence" and {"query"} <= set(arguments) <= {
                "query",
                "jurisdictions",
                "language",
                "as_of",
                "max_units",
                "max_evidence_bytes",
            }:
                query = arguments["query"]
                as_of, language = _date(arguments.get("as_of")), _language(arguments.get("language"))
                units, budget = arguments.get("max_units", 8), arguments.get("max_evidence_bytes", 24_576)
                named = arguments.get("jurisdictions")
                if (
                    not isinstance(query, str)
                    or not 1 <= len(query) <= 2000
                    or as_of is False
                    or language is False
                ):
                    return None
                if not isinstance(units, int) or isinstance(units, bool) or not 1 <= units <= 15:
                    return None
                if not isinstance(budget, int) or isinstance(budget, bool) or not 4096 <= budget <= 40_960:
                    return None
                if named is not None:
                    if (
                        not isinstance(named, list)
                        or not 1 <= len(named) <= 10
                        or any(not isinstance(j, str) or not 2 <= len(j) <= 40 for j in named)
                    ):
                        return None
                    for jurisdiction in named:
                        plan.admit(jurisdiction.lower())
                    jurisdictions = tuple(sorted({j.lower() for j in named}))
                else:
                    scope = plan.within(j.id for j in service.jurisdictions())
                    jurisdictions = None if scope is None else tuple(sorted(scope))
                filters = text_index.Filters(jurisdictions=jurisdictions, language=language, as_of=as_of)
                pack = Retriever(service).retrieve(
                    EvidenceRequest(query=query, filters=filters, max_units=units, max_evidence_bytes=budget)
                )
                return _bounded(pack.model_dump(mode="json"))
            if name == "check_currentness" and set(arguments) == {"items"}:
                try:
                    request = CheckRequest.model_validate(arguments)
                except ValueError:
                    return None
                checker = Checker(service)
                page = checker.check(request.items)
                plan.admit(*sorted(checker.jurisdictions))
                return _bounded(page.model_dump(mode="json"))
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
            except TooLarge as refused:
                return _result(id_, _tool_result(str(refused), is_error=True))
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
