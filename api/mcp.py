"""Read-only MCP endpoint over the public catalogue (JSON-RPC 2.0, Streamable HTTP).

Tools return exactly what the public REST API returns for the same request, under the same
current-policy checks and admission limits; no tool can reach private staging, unapproved text
or account data. Responses are single JSON bodies (no server-initiated streams).
"""

import asyncio
import base64
import dataclasses
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from api import text_index
from api.catalogue import NO_WORDS
from api.mcp_schemas import OUTPUT_SCHEMAS
from api.reach import EVERYWHERE, OutsidePlan, OutsidePlanLimited, Reach, format_jurisdictions
from api.repositories import EDGE_ORDER, ResultTooLarge
from api.search import normalize_citation
from api.search import words as search_words
from changes.feed import Feed, FeedError, Filters, Withdrawals
from changes.routes import tokens
from retrieval.check import Checker
from retrieval.pack import Request as EvidenceRequest
from retrieval.pack import Retriever
from schema.public import CheckRequest, SearchPage

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
SUPPORTED_PROTOCOL_VERSIONS = ["2026-07-28", "2025-06-18"]
ACCEPTED_HEADER_VERSIONS = frozenset({"2026-07-28", "2025-06-18", "2025-03-26"})
# Tool answers stay inside the MCP payload budget (BUD-01: 64 KiB), never shortened.
MAX_TOOL_BYTES = 60 * 1024
MAX_SUMMARY_BYTES = 2048
MANAGE_URL = "https://open.alvary.ai/console/access"
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class TooLarge(Exception):
    pass


class _CursorError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SERVER_INFO = {"name": "open-alvary", "title": "Open Alvary public legal catalogue", "version": "0.1.0"}

INSTRUCTIONS = (
    "Read-only public legal catalogue for African jurisdictions. "
    "The official gazetted or court copy always prevails over any reproduced text. "
    "Check get_relationships and check_currentness before relying on a provision. "
    "Distinguish records without a published text from cleared full text: a catalogue record does not "
    "grant reuse rights; check each source's rights status. "
    "Zero results or unavailable means Open Alvary does not hold or publish the text within your plan's "
    "active jurisdictions, never that a law does not exist. "
    "Do not give legal advice, and do not harvest a whole Act part-by-part. "
    "Usage guide: https://open.alvary.ai/docs/ai-tools"
)


def _annotations() -> dict[str, bool]:
    return {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


TOOLS = [
    {
        "name": "check_claim",
        "title": "Check a legal claim and issue a signed receipt",
        "description": "Preview (may change): deterministically checks exact quoted words only against a named "
        "part of a text Open Alvary publishes and what Open Alvary's record lists about its status on a date "
        "(and whether it is recorded as binding in a jurisdiction), returning a publisher-signed oa-receipt-1 "
        "receipt beside the claim. Does not judge paraphrases and gives no legal advice.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 600,
                    "description": "A citation such as 'Freedom of Information Act 2011, s. 1(1)'. One of "
                    "citation or anchor_id is required.",
                },
                "anchor_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "The part's anchor_id when already held. One of citation or anchor_id is "
                    "required.",
                },
                "quote": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "Optional exact words the claim says the law uses; at least 40 characters "
                    "after normalisation unless the quote is the whole of the part's words.",
                },
                "as_of": {
                    "type": "string",
                    "format": "date",
                    "description": "Optional assessment date YYYY-MM-DD, not after today in UTC. Left out, "
                    "checks status today.",
                },
                "jurisdiction": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 40,
                    "description": "Optional jurisdiction such as ng; asks whether the record is recorded as "
                    "binding there.",
                },
                "language": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 35,
                    "description": "Optional language such as en.",
                },
                "reading": {
                    "type": "string",
                    "maxLength": 600,
                    "description": "Optional sentence this check is meant to support. Echoed beside the "
                    "receipt; not checked, not signed, not in the claim digest, never logged.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["check_claim"],
    },
    {
        "name": "check_currentness",
        "title": "Check cited parts",
        "description": "Before showing an answer that cites the corpus: for each cited part, whether it is "
        "still published with the same words (unchanged), whether a newer text of the same edition exists "
        "(newer_version, with the same part's anchor_id there and whether its words changed), whether the "
        "span_hash sent is wrong (span_mismatch) or the part is not public (unavailable; with a key, "
        "withdrawn instead when its text was public and was withdrawn for a reason that may be told); whether it "
        "was in force on a date; and, given a jurisdiction, whether the record binds there on that date "
        "(applicability: bound, not_bound or unknown, with the memberships, legal effect or treaty party "
        "entry it rests on). Absence of a recorded link or treatment does not prove none exists.",
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
                            "jurisdiction": {
                                "type": "string",
                                "pattern": "^[a-z]{2}(?:-[a-z0-9]{1,12})?$",
                            },
                        },
                        "additionalProperties": False,
                    },
                    "description": "The parts: each an anchor_id, or a version_id with a locator, with the "
                    "span_hash you hold, an optional as_of date and, to ask whether the record binds in a "
                    "jurisdiction on that date, its jurisdiction, such as sn.",
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["check_currentness"],
    },
    {
        "name": "get_anchor",
        "title": "Get one part of a text exactly",
        "description": "One part of a published text, such as a section or article, exactly: its words, its "
        "span as UTF-8 byte offsets, the hash of its bytes for checking a quotation, a citation such as "
        "s. 23(1)(a), the official file and the attribution its rights record asks for. Never shortened: a "
        "part too large for one answer is refused; read it from the REST text route with its offsets. With a key, "
        "a part whose text was public and was withdrawn for a reason that may be told answers withdrawn, not "
        "unavailable. The official published copy always prevails; do not harvest a whole Act part-by-part.",
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["get_anchor"],
    },
    {
        "name": "get_change",
        "title": "Get one change",
        "description": "One catalogue change by its event_id, as list_changes gives it. Reflects catalogue "
        "commits only, not every change in a jurisdiction's law.",
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["get_change"],
    },
    {
        "name": "get_coverage",
        "title": "Catalogue coverage",
        "description": "Counts of catalogued sources and of sources with openly redistributable full text. "
        "Call before making scope claims about what Open Alvary covers.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["get_coverage"],
    },
    {
        "name": "get_relationships",
        "title": "Get a source's history",
        "description": "Whether a source is current and how it relates to other public sources, one step "
        "away: what it repeals, amends or commences and the law it is made under, what repealed, amended "
        "or commenced it, the treaty or directive it transposes, domesticates, implements or ratifies and "
        "the laws that do so for it, what it cites and is cited by, the instruments made under it, and the "
        "source in force in its place now when it has been repealed. For case law: each judgment paragraph "
        "that cites a provision or another judgment, with the paragraph's words, and how a judgment follows, "
        "applies, distinguishes, doubts or overrules another, or was so treated, where a legally qualified "
        "reviewer recorded it. Only links a reviewer has checked against the official text; absence of a "
        "recorded link or treatment does not prove none exists. A page at a time.",
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
                    "description": "Only links of this kind, such as repealed_by, enables, cited_by or "
                    "overruled_by.",
                },
                "locator": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "Only the citations and treatments of this part of the source, or of a "
                    "part within it: a provision's locator, such as sec_2__subsec_1, for the judgment "
                    "paragraphs citing it (relation_type cited_by), or a judgment paragraph's, such as para_12.",
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["get_relationships"],
    },
    {
        "name": "get_source",
        "title": "Get one source",
        "description": "Get one catalogued source by its ID, including rights status and access mode. Inspect "
        "rights and withdrawal status: with a key, a record that was public and was withdrawn for a reason "
        "that may be told answers withdrawn, not unavailable — stop serving it and its texts.",
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["get_source"],
    },
    {
        "name": "list_changes",
        "title": "List catalogue changes",
        "description": "Changes to the catalogue in commit order: records added, corrected or withdrawn, text "
        "published, legal effects (repeals, amendments, commencements, transpositions and others a "
        "reviewer checked) recorded or withdrawn, reviewed memberships of the African Union and the "
        "regional bodies recorded, ended or corrected, and members' suspensions from participation recorded "
        "or lifted (a suspended member remains one). Filter by jurisdiction, work, type, document class, "
        "language or keyword. Pass the returned resume_token next time to get only newer changes. With a "
        "key, a record withdrawn because its rights record changed is named by its IDs only; other "
        "withdrawals of records no longer public, and a text withdrawn for privacy, name nothing. Reflects "
        "catalogue commits only, not every change in a jurisdiction's law.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jurisdiction": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                    "description": "Jurisdiction IDs, such as ng or rw; a jurisdiction includes its "
                    "subdivisions, and a body such as aa-ecowas includes changes to its membership and its "
                    "members' suspensions.",
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
                    "maxItems": 10,
                    "description": "Kinds of change: catalogue_added, metadata_corrected, rights_changed, "
                    "text_version_published, legal_effect_recorded, treatment_recorded, withdrawal, "
                    "coverage_changed, membership_recorded, suspension_recorded.",
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["list_changes"],
    },
    {
        "name": "list_expressions",
        "title": "List a source's language editions",
        "description": "A source's language editions with a published text: for each, its texts, the one to read "
        "when no date is given, and what the edition is worth as law as its record states it (authoritative, "
        "equally_authentic, official_translation, unofficial_translation or machine_translation, with where "
        "that is stated; unknown when nothing is recorded); and the instrument's own rule for which version "
        "prevails where its language versions differ (language_rule), or unknown. Inspect language authority: "
        "unofficial translations are not authoritative, and a preferred text is a reading default only.",
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["list_expressions"],
    },
    {
        "name": "resolve_citation",
        "title": "Resolve a citation",
        "description": "The records a citation names and, for a citation of a part such as NDPA s. 23(1)(a), "
        "art. 12 or First Schedule, para. 3, the part in the record's public text: its anchor_id for "
        "get_anchor and its exact span. Alternatives (two languages, two texts, two records) are listed with "
        "the reason, never chosen between. Unresolved does not mean no such law exists.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "citation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 600,
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["resolve_citation"],
    },
    {
        "name": "retrieve_evidence",
        "title": "Retrieve legal evidence",
        "description": "Ranked legal evidence for a question, to cite in an answer: each unit is a provision "
        "(or a law's title and preamble) with its exact words or an excerpt, its anchor_id, byte span and "
        "span_hash, its citation such as s. 23(1)(a), the pages of the official file it is on, links to "
        "read it on Open Alvary and the official file's address and hash, what quoting it asks for, whether "
        "it was in force (currentness, with what that rests on), the definitions it relies on and why it "
        "ranks there. A question that cites a part (NDPA s. 24(1)(a), section 24 of the Nigeria Data "
        "Protection Act, Part V of the NDPA), names a page of a law (page 18 of the NDPA), is an Open Alvary "
        "or official address, names a law or asks what a term means is routed to that first. Quote only the "
        "words given; check cited parts with check_currentness before showing an answer. The official "
        "published copy always prevails; do not harvest a whole Act part-by-part.",
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
                "source_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "description": "Only this record, such as ng-data-protection-act-2023: then a pinpoint "
                    "alone (s. 24) or a page (page 18) is read within it.",
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
                "scope": {
                    "type": "string",
                    "enum": ["applicable"],
                    "description": "applicable, with exactly one jurisdiction: also the law that binds there "
                    "on as_of (today without it): instruments of the African Union and regional bodies it "
                    "belonged to (reviewed memberships only) and treaties it ratified or acceded to. Each unit "
                    "then says how it relates to that jurisdiction (applicability: national, regional_direct, "
                    "treaty_in_force_for_state, unknown and others, with what that rests on), and the answer "
                    "states what was added and left out. For a place within a national jurisdiction (ng-la), "
                    "the national law too, and its memberships and treaties.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["retrieve_evidence"],
    },
    {
        "name": "search_catalogue",
        "title": "Search the catalogue",
        "description": "Search catalogued legal sources by words in their title, citation or summary, or in "
        "one part of their cleared full text. Every word must match a whole word, in any order. Returns "
        "metadata, rights status and official links. Records without a published text do not grant reuse "
        "rights. Verify currentness and relationships with get_relationships and check_currentness before "
        "relying on a law.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "Words to find, such as a title or a citation. Every word must appear; case "
                    "and punctuation don't matter.",
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
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["search_catalogue"],
    },
    {
        "name": "search_text",
        "title": "Search published texts",
        "description": "Search the words of published legal texts. Returns matching passages (sections, articles, "
        "schedule paragraphs), best first, each with an exact snippet, a citation such as s. 23(1)(a), the "
        "part's anchor_id for get_anchor and the record's legal status. Only published texts are searched "
        "(records without a published text do not grant reuse rights): coverage says how many records in scope "
        "have one, and status tells no_result apart from a scope with no published text (unsupported_scope) "
        "and a search that could not run (degraded). Verify currentness and relationships with "
        "get_relationships and check_currentness before relying on a law.",
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
                "scope": {
                    "type": "string",
                    "enum": ["applicable"],
                    "description": "applicable: also the law that binds in the one jurisdiction given on as_of "
                    "(today without it): instruments of the African Union and regional bodies it belonged to "
                    "(reviewed memberships only) and treaties it ratified or acceded to. Each passage then "
                    "says how it relates to that jurisdiction (applicability), and the answer states what "
                    "was added and left out. For a place within a national jurisdiction (ng-la), the national "
                    "law too, and its memberships and treaties.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": _annotations(),
        "outputSchema": OUTPUT_SCHEMAS["search_text"],
    },
]

TOOLS_2026 = TOOLS
TOOLS_2025 = [{k: v for k, v in t.items() if k != "outputSchema"} for t in TOOLS]
TOOL_SCHEMA_REVISION = hashlib.sha256(
    json.dumps(TOOLS_2026, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()[:16]


def normalize_host(value: str) -> str:
    """Lowercase a Host header and strip standard :80/:443 ports while keeping non-standard ports."""
    host = value.strip().lower()
    if host.startswith("["):
        if host.endswith("]:443"):
            return host[:-4]
        if host.endswith("]:80"):
            return host[:-3]
        return host
    if host.count(":") == 1:
        name, port = host.rsplit(":", 1)
        if port in ("80", "443"):
            return name
    return host


def _encode_cursor(offset: int) -> str:
    raw = json.dumps({"rev": TOOL_SCHEMA_REVISION, "offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not (1 <= len(cursor) <= 256) or not _B64URL_RE.fullmatch(cursor):
        raise _CursorError("invalid_cursor", "Invalid tools/list cursor.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _CursorError("invalid_cursor", "Invalid tools/list cursor.") from None
    if not isinstance(parsed, dict) or set(parsed.keys()) != {"rev", "offset"}:
        raise _CursorError("invalid_cursor", "Invalid tools/list cursor.")
    rev, offset = parsed["rev"], parsed["offset"]
    if not isinstance(rev, str) or not isinstance(offset, int) or isinstance(offset, bool):
        raise _CursorError("invalid_cursor", "Invalid tools/list cursor.")
    if rev != TOOL_SCHEMA_REVISION:
        raise _CursorError("stale_cursor", "Cursor does not match the current tool schema revision.")
    if offset < 0 or offset >= len(TOOLS_2026):
        raise _CursorError("invalid_cursor", "Cursor offset is out of range.")
    return offset


def _truncate_utf8(text: str, limit: int = MAX_SUMMARY_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    clipped = raw[: max(0, limit - 3)]
    return clipped.decode("utf-8", errors="ignore").rstrip() + "..."


def _summary_text(name: str, payload: dict) -> str:
    if name == "search_catalogue":
        items = payload.get("items") or []
        parts = [f"Found {payload.get('total', 0)} catalogued source(s) (returned {len(items)})."]
        for item in items[:5]:
            parts.append(
                f"- {item.get('title', '')} ({item.get('id', '')}, {item.get('jurisdiction_id', '')})"
            )
        return _truncate_utf8("\n".join(parts))
    if name == "get_source":
        return _truncate_utf8(
            f"{payload.get('title', '')} ({payload.get('id', '')}, {payload.get('jurisdiction_id', '')}, "
            f"status: {payload.get('legal_status', 'unknown')})."
        )
    if name == "get_relationships":
        return _truncate_utf8(
            f"{payload.get('source_id', '')}: {payload.get('total', 0)} reviewed relationship(s), "
            f"legal_status={payload.get('legal_status', 'unknown')}."
        )
    if name == "list_expressions":
        items = payload.get("items") or []
        return _truncate_utf8(f"{payload.get('source_id', '')}: {len(items)} language edition(s).")
    if name == "get_anchor":
        label = payload.get("citation") or payload.get("pinpoint") or payload.get("anchor_id", "")
        return _truncate_utf8(f"{label} ({payload.get('source_id', '')}, {payload.get('version_id', '')}).")
    if name == "search_text":
        items = payload.get("items") or []
        return _truncate_utf8(
            f"Text search ({payload.get('status', '')}): {payload.get('total', 0)} matching passage(s) "
            f"(returned {len(items)})."
        )
    if name == "retrieve_evidence":
        units = payload.get("evidence") or []
        return _truncate_utf8(f"Retrieved {len(units)} evidence unit(s).")
    if name == "check_currentness":
        results = payload.get("results") or []
        return _truncate_utf8(f"Checked {len(results)} cited part(s).")
    if name == "resolve_citation":
        matches = payload.get("matches") or []
        return _truncate_utf8(
            f"Citation resolution: {payload.get('status', '')} ({len(matches)} matching record(s))."
        )
    if name == "list_changes":
        events = payload.get("events") or []
        return _truncate_utf8(
            f"Returned {len(events)} catalogue change(s) (has_more={payload.get('has_more', False)})."
        )
    if name == "get_change":
        summary = payload.get("summary") or ""
        return _truncate_utf8(
            f"Change {payload.get('event_id', '')} ({payload.get('event_type', '')}): {summary}".strip()
        )
    if name == "get_coverage":
        return _truncate_utf8(
            f"Coverage: {payload.get('catalogued_sources', 0)} catalogued sources, "
            f"{payload.get('open_full_text_sources', 0)} with openly redistributable full text."
        )
    if name == "check_claim":
        receipt = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else {}
        return _truncate_utf8(
            f"Claim check ({receipt.get('status', '')}): {receipt.get('statement', '')}".strip()
        )
    return _truncate_utf8(f"Tool {name} completed.")


def _scoped_search_notice(plan: Reach, allowed_source_ids: frozenset[str] | None) -> str | None:
    if allowed_source_ids is not None:
        return None
    active = plan.active_jurisdictions()
    if active is None:
        return None
    return (
        f"Search limited to your plan's active jurisdiction(s) ({format_jurisdictions(active)}) "
        "plus continental and regional instruments (aa-*). An owner can change jurisdictions or plan "
        f"under Plan and limits at {MANAGE_URL}."
    )


def changes_tool(conn, name, arguments, keyed: bool = False):
    """The change feed as MCP tools: the same events and errors as GET /changes (changes-contracts),
    keyed or not as the caller is (changes.feed.Withdrawals)."""
    origin = "https://" + os.getenv("PUBLIC_DOMAIN", "open.alvary.ai")
    feed = Feed(conn, tokens(), origin, keyed=keyed)
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


def _bounded(payload: dict, fewer: str = "passages") -> dict:
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_TOOL_BYTES:
        raise TooLarge(f"result_too_large: ask for fewer {fewer}")
    return payload


def _error(id_, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": error}


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


class ToldWithdrawn(Exception):
    """A keyed caller asked for a record, text or part that was public and was withdrawn for a reason that
    may be told (changes.feed.Withdrawals): answered ``withdrawn``, as REST's reads are (owner decision
    2026-10-22). ``argument`` names what was withdrawn: source_id or anchor_id."""

    def __init__(self, argument: str):
        super().__init__(argument)
        self.argument = argument


# As REST's reads say it (api.v1.WITHDRAWN).
WITHDRAWN = {
    "source_id": "withdrawn: this record was public and has been withdrawn. Stop serving it and its texts.",
    "anchor_id": "withdrawn: the text this part is in was public and has been withdrawn. Stop serving copies of it.",
}


class _VerifyError(Exception):
    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _tool_result(payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tool_error(
    code: str,
    message: str,
    retry_after: int | None = None,
    extra: dict | None = None,
    *,
    include_result_type: bool = False,
) -> dict:
    """A tool that ran and failed: the message for the model as text, as before, and the same machine-
    readable code as the REST refusal's in structuredContent (MCP 2025-06-18 / 2026-07-28)."""
    structured: dict = {"code": code}
    if retry_after is not None:
        structured["retry_after_seconds"] = retry_after
    if extra:
        structured.update(extra)
    out = _tool_result(message, is_error=True) | {"structuredContent": structured}
    if include_result_type:
        out = {"resultType": "complete", **out}
    return out


NOT_COUNTED = (
    "This plan answers for the records on its list only; other records are neither counted nor named."
)
UNNAMED_LINK = (
    "A record in this evidence has a reviewed amending, repealing or superseding link to a record outside "
    "this plan's list, which is not named here: check its status before relying on it."
)


def _listed_statement(statement, admitted: int):
    """An applicable-law scope's statement on a plan with a record list: how many listed records the scope
    added, and nothing counted or said of any other record (the scope's own statement counts and names
    records whatever the plan's list)."""
    return statement.model_copy(update={"admitted": admitted, "left_out": {}, "notices": (NOT_COUNTED,)})


def _listed_pack(payload: dict, allowed_source_ids: frozenset[str]) -> dict:
    """An evidence pack on a plan with a record list names no record off the list: a unit's qualifying
    links (amended_by, repealed_by, superseded_by) to other records are dropped, with one notice saying
    that there are such links, so the reader is still told to check."""
    unnamed = False
    for unit in payload.get("evidence") or ():
        context = unit.get("context") if isinstance(unit, dict) else None
        links = context.get("qualifications") if isinstance(context, dict) else None
        if links:
            kept = [link for link in links if link.get("source_id") in allowed_source_ids]
            unnamed = unnamed or len(kept) != len(links)
            context["qualifications"] = kept
    if unnamed and isinstance(payload.get("coverage"), dict):
        payload["coverage"]["notices"] = [*(payload["coverage"].get("notices") or ()), UNNAMED_LINK]
    return payload


def _filter_citation_view(view, allowed_source_ids: frozenset[str]):
    kept_matches = tuple(m for m in view.matches if m.id in allowed_source_ids)
    provision = view.provision
    if provision is not None:
        kept_candidates = tuple(c for c in provision.candidates if c.source_id in allowed_source_ids)
        nearest = (
            provision.nearest
            if provision.nearest is not None and provision.nearest.source_id in allowed_source_ids
            else None
        )
        if not kept_matches:
            provision = None
        elif len(kept_candidates) == 1:
            provision = provision.model_copy(
                update={
                    "status": "resolved",
                    "reason": None,
                    "candidates": kept_candidates,
                    "nearest": None,
                }
            )
        elif len(kept_candidates) > 1:
            reason = (
                "several_works"
                if len({c.source_id for c in kept_candidates}) > 1
                else "several_texts"
                if len({c.version_id for c in kept_candidates}) > 1
                else "repeated_number"
            )
            provision = provision.model_copy(
                update={
                    "status": "ambiguous",
                    "reason": reason,
                    "candidates": kept_candidates,
                    "nearest": None,
                }
            )
        else:
            reason = provision.reason if provision.status == "unresolved" else "not_found"
            provision = provision.model_copy(
                update={
                    "status": "unresolved",
                    "reason": reason,
                    "candidates": (),
                    "nearest": nearest,
                }
            )
    status = "resolved" if len(kept_matches) == 1 else "ambiguous" if kept_matches else "unresolved"
    return view.model_copy(update={"status": status, "matches": kept_matches, "provision": provision})


def create_router(
    catalogue_factory: Callable,
    unavailable: tuple[type[Exception], ...],
    reach: Callable[[Request], Reach] = lambda request: EVERYWHERE,
    signer: Callable[[], object] | None = None,
    allowed_origins: frozenset[str] | None = None,
    allowed_hosts: frozenset[str] | None = None,
    front_door_id: str | None = None,
    tools_page_size: int = 25,
    tool_timeout: float = 25.0,
) -> APIRouter:
    """``reach`` gives the organisation's plan (api.reach): its jurisdictions at the plan's limits, every
    other jurisdiction at the Starter rate, or refused on Starter."""
    router = APIRouter()
    norm_allowed_origins = (
        frozenset(o.strip().lower().rstrip("/") for o in allowed_origins)
        if allowed_origins is not None
        else None
    )
    norm_allowed_hosts = (
        frozenset(normalize_host(h) for h in allowed_hosts) if allowed_hosts is not None else None
    )
    page_size = max(1, tools_page_size)

    def _effective_host(request: Request) -> str:
        if front_door_id and request.headers.get("x-azure-fdid") == front_door_id:
            return request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
        return request.headers.get("host", "").strip()

    def _origin_ok(origin: str, effective_host: str) -> bool:
        stripped = origin.strip()
        if not stripped or stripped.lower() == "null":
            return False
        parts = urlsplit(stripped)
        scheme = parts.scheme.lower()
        if (
            scheme not in ("http", "https")
            or not parts.netloc
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
        ):
            return False
        normalized = f"{scheme}://{parts.netloc.lower()}"
        if norm_allowed_origins is not None:
            return normalized in norm_allowed_origins
        if not effective_host:
            return False
        return parts.netloc.lower() == normalize_host(effective_host)

    def call_tool(
        name,
        arguments,
        plan: Reach,
        keyed: bool = False,
        allowed_source_ids: frozenset[str] | None = None,
        anchor_text_bytes: int | None = None,
    ):
        """``keyed``: the credential gate admitted a key or token, so the change tools, the check and the
        record reads tell what a keyed caller is told of withdrawals (changes.feed.Withdrawals): a record
        read that finds nothing public answers ``withdrawn`` where the record or the part's text was
        public and its withdrawal may be told, as REST's reads do.

        ``anchor_text_bytes``: the plan's limit on one part's words (its ``anchor_text_bytes`` setting).
        get_anchor refuses a larger part before any of it is written, so the refusal is what the
        organisation's data a day is charged for, not the words it never receives."""
        try:
            return read_tool(name, arguments, plan, keyed, allowed_source_ids, anchor_text_bytes)
        except LookupError:
            if keyed and isinstance(arguments, dict):
                with catalogue_factory() as service:
                    connection = service.repository.connection
                    if connection.dialect.name == "postgresql":
                        told = Withdrawals(connection)
                        for argument, test in (("source_id", told.record), ("anchor_id", told.anchor)):
                            value = arguments.get(argument)
                            if isinstance(value, str) and test(value):
                                raise ToldWithdrawn(argument) from None
            raise

    def read_tool(
        name,
        arguments,
        plan: Reach,
        keyed: bool = False,
        allowed_source_ids: frozenset[str] | None = None,
        anchor_text_bytes: int | None = None,
    ):
        if not isinstance(arguments, dict):
            return None
        with catalogue_factory() as service:

            def applicable(jurisdiction, as_of, source_id):
                """An applicable-law scope (api.applicable); its records are read under the plan as REST's."""
                scope = service.applicable_scope(jurisdiction, as_of or None, named=source_id)
                plan.admit(*sorted(scope.jurisdictions))
                return scope

            if name == "search_catalogue" and set(arguments) <= {"query", "limit", "jurisdiction"}:
                query, limit = arguments.get("query"), arguments.get("limit", 10)
                jurisdiction = arguments.get("jurisdiction")
                unnamed_search = jurisdiction is None
                if not isinstance(query, str) or not 1 <= len(query) <= 500:
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
                if allowed_source_ids is not None:
                    if not normalize_citation(query):
                        return None
                    if not search_words(query):
                        return SearchPage(total=0, items=(), notices=(NO_WORDS,)).model_dump(mode="json")
                    if not allowed_source_ids:
                        return SearchPage(total=0, items=(), notices=()).model_dump(mode="json")
                    by_text, notices = service._by_text(query, jurisdiction)
                    by_text = by_text & allowed_source_ids
                    total, pairs = service.repository.search_page(
                        query,
                        by_text,
                        jurisdiction=jurisdiction,
                        limit=limit,
                        offset=0,
                        access_where=service.repository._sources.c.id.in_(sorted(allowed_source_ids)),
                    )
                    return SearchPage(total=total, items=service._views(pairs), notices=notices).model_dump(
                        mode="json"
                    )
                payload = service.search(query, jurisdiction=jurisdiction, limit=limit, offset=0).model_dump(
                    mode="json"
                )
                if unnamed_search:
                    notice = _scoped_search_notice(plan, allowed_source_ids)
                    if notice is not None:
                        payload["notices"] = [*(payload.get("notices") or ()), notice]
                return payload
            if name == "get_source" and set(arguments) == {"source_id"}:
                if not isinstance(arguments["source_id"], str) or not 1 <= len(arguments["source_id"]) <= 160:
                    return None
                view = service.source(arguments["source_id"])
                plan.admit(view.jurisdiction_id)
                return view.model_dump(mode="json")
            if name == "get_relationships" and {"source_id"} <= set(arguments) <= {
                "source_id",
                "relation_type",
                "locator",
                "limit",
                "cursor",
            }:
                source_id, kind = arguments["source_id"], arguments.get("relation_type")
                limit, cursor = arguments.get("limit", 25), arguments.get("cursor")
                locator = arguments.get("locator")
                if not isinstance(source_id, str) or not 1 <= len(source_id) <= 160:
                    return None
                if kind is not None and kind not in EDGE_ORDER:
                    return None
                if locator is not None and (not isinstance(locator, str) or not 1 <= len(locator) <= 300):
                    return None
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                    return None
                if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 200):
                    return None
                plan.admit(service.source(source_id).jurisdiction_id)
                # Each citation carries its paragraph's words: a page larger than a tool answer is refused,
                # never shortened (BUD-01).
                return _bounded(
                    service.relationships(
                        source_id, kinds=[kind] if kind else None, limit=limit, cursor=cursor, locator=locator
                    ).model_dump(mode="json"),
                    "links (a smaller limit)",
                )
            if name == "list_expressions" and set(arguments) == {"source_id"}:
                source_id = arguments["source_id"]
                if not isinstance(source_id, str) or not 1 <= len(source_id) <= 160:
                    return None
                plan.admit(service.source(source_id).jurisdiction_id)
                return _bounded(service.expressions(source_id).model_dump(mode="json"))
            if name == "get_anchor" and set(arguments) == {"anchor_id"}:
                anchor_id = arguments["anchor_id"]
                if not isinstance(anchor_id, str) or not 1 <= len(anchor_id) <= 160:
                    return None
                view = service.anchor(anchor_id)
                if allowed_source_ids is not None and view.source_id not in allowed_source_ids:
                    raise LookupError("Source not available")
                plan.admit(service.source(view.source_id).jurisdiction_id)
                if anchor_text_bytes is not None and len(view.text.encode()) > anchor_text_bytes:
                    raise TooLarge(
                        f"result_too_large: this part holds more than {anchor_text_bytes:,} bytes of words; "
                        "name a smaller part."
                    )
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
                "scope",
            }:
                query, limit, cursor = arguments["query"], arguments.get("limit", 5), arguments.get("cursor")
                as_of, language = _date(arguments.get("as_of")), _language(arguments.get("language"))
                jurisdiction = arguments.get("jurisdiction")
                unnamed_search = jurisdiction is None
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
                if arguments.get("scope", "applicable") != "applicable":
                    return None
                if jurisdiction is not None:
                    if not isinstance(jurisdiction, str) or not 2 <= len(jurisdiction) <= 40:
                        return None
                    plan.admit(jurisdiction.lower())
                    jurisdictions = (jurisdiction.lower(),)
                elif "scope" in arguments:
                    return None
                else:
                    scope = plan.within(j.id for j in service.jurisdictions())
                    jurisdictions = None if scope is None else tuple(sorted(scope))
                widened = applicable(jurisdictions[0], as_of, None) if "scope" in arguments else None
                filters = text_index.Filters(jurisdictions=jurisdictions, language=language, as_of=as_of)
                page = service.search_text(query, filters, limit=limit, cursor=cursor, scope=widened)
                payload = page.model_dump(mode="json")
                if unnamed_search:
                    notice = _scoped_search_notice(plan, allowed_source_ids)
                    if notice is not None:
                        payload["notices"] = [*(payload.get("notices") or ()), notice]
                return _bounded(payload)
            if name == "resolve_citation" and {"citation"} <= set(arguments) <= {
                "citation",
                "as_of",
                "language",
            }:
                citation = arguments["citation"]
                as_of, language = _date(arguments.get("as_of")), _language(arguments.get("language"))
                if (
                    not isinstance(citation, str)
                    or not 1 <= len(citation) <= 600
                    or as_of is False
                    or language is False
                ):
                    return None
                view = service.citations(citation, as_of=as_of, language=language)
                if allowed_source_ids is not None:
                    view = _filter_citation_view(view, allowed_source_ids)
                plan.admit(*(m.jurisdiction_id for m in view.matches))
                return _bounded(view.model_dump(mode="json"))
            if name == "retrieve_evidence" and {"query"} <= set(arguments) <= {
                "query",
                "jurisdictions",
                "language",
                "as_of",
                "source_id",
                "max_units",
                "max_evidence_bytes",
                "scope",
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
                source_id = arguments.get("source_id")
                if source_id is not None and (
                    not isinstance(source_id, str) or not 1 <= len(source_id) <= 160
                ):
                    return None
                if "scope" in arguments and (
                    arguments["scope"] != "applicable" or named is None or len(jurisdictions) != 1
                ):
                    return None
                widened = applicable(jurisdictions[0], as_of, source_id) if "scope" in arguments else None
                if allowed_source_ids is not None:
                    effective_allowed = (
                        {source_id} & allowed_source_ids if source_id is not None else set(allowed_source_ids)
                    )
                    if effective_allowed:
                        public_allowed = service._public(effective_allowed)
                        if widened is not None:
                            allowed_j = set(widened.jurisdictions)
                            allowed_w = set(widened.admitted)
                            effective_allowed = {
                                sid
                                for sid, (src, _) in public_allowed.items()
                                if src.jurisdiction_id in allowed_j or sid in allowed_w
                            }
                        elif jurisdictions is not None:
                            allowed_j = set(jurisdictions)
                            effective_allowed = {
                                sid
                                for sid, (src, _) in public_allowed.items()
                                if src.jurisdiction_id in allowed_j
                            }
                        else:
                            effective_allowed = set(public_allowed)
                    filters = text_index.Filters(
                        jurisdictions=(),
                        admitted=tuple(sorted(effective_allowed)),
                        language=language,
                        as_of=as_of,
                        source_id=source_id if source_id in effective_allowed else None,
                    )
                    if widened is not None:
                        widened = dataclasses.replace(
                            widened,
                            within=None,
                            admitted=tuple(sorted(effective_allowed)),
                            statement=_listed_statement(
                                widened.statement, len(effective_allowed & set(widened.admitted))
                            ),
                        )
                else:
                    filters = text_index.Filters(
                        jurisdictions=jurisdictions, language=language, as_of=as_of, source_id=source_id
                    )
                pack = Retriever(service).retrieve(
                    EvidenceRequest(
                        query=query,
                        filters=filters,
                        max_units=units,
                        max_evidence_bytes=budget,
                        scope=widened,
                    )
                )
                payload = pack.model_dump(mode="json")
                if allowed_source_ids is not None:
                    payload = _listed_pack(payload, allowed_source_ids)
                elif named is None:
                    notice = _scoped_search_notice(plan, allowed_source_ids)
                    if notice is not None and isinstance(payload.get("coverage"), dict):
                        payload["coverage"]["notices"] = [*(payload["coverage"].get("notices") or ()), notice]
                return _bounded(payload)
            if name == "check_claim" and set(arguments) <= {
                "citation",
                "anchor_id",
                "quote",
                "as_of",
                "jurisdiction",
                "language",
                "reading",
            }:
                from api.receipts import load_signer_from_env
                from retrieval.verify import VerifyToolError, verify_claim

                debited = False

                def admit_once(code: str) -> None:
                    nonlocal debited
                    if not plan.covers(code) and not debited:
                        plan.admit(code)
                        debited = True

                active_signer = signer() if signer is not None else load_signer_from_env()
                try:
                    result = verify_claim(
                        service,
                        arguments,
                        signer=active_signer,
                        allowed_source_ids=allowed_source_ids,
                        check_jurisdiction=admit_once,
                    )
                except VerifyToolError as error:
                    msg = (
                        error.message
                        if error.message.startswith(f"{error.code}:")
                        else f"{error.code}: {error.message}"
                    )
                    raise _VerifyError(error.code, msg, error.extra) from None
                return _bounded(result)
            if name == "check_currentness" and set(arguments) == {"items"}:
                try:
                    request = CheckRequest.model_validate(arguments)
                except ValueError:
                    return None
                connection = service.repository.connection
                told = Withdrawals(connection) if keyed and connection.dialect.name == "postgresql" else None
                checker = Checker(service, withdrawn=told.item if told else None)
                page = checker.check(request.items)
                plan.admit(*sorted(checker.jurisdictions))
                return _bounded(page.model_dump(mode="json"))
            if name in ("list_changes", "get_change"):
                return changes_tool(service.repository.connection, name, arguments, keyed)
            if name == "get_coverage" and not arguments:
                return service.coverage().model_dump(mode="json")
        return None

    @router.post("/mcp")
    async def mcp(request: Request):
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:16]
        effective_host = _effective_host(request)
        if norm_allowed_hosts is not None and not getattr(request.state, "law_check_internal", False):
            if not effective_host or normalize_host(effective_host) not in norm_allowed_hosts:
                return JSONResponse(
                    status_code=403,
                    content=_error(
                        None,
                        10002,
                        "Host not allowed",
                        {
                            "code": "host_not_allowed",
                            "retryable": False,
                            "request_id": request_id,
                            "schema_version": "1",
                        },
                    ),
                    headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
                )
        origin = request.headers.get("origin")
        if origin is not None and not _origin_ok(origin, effective_host):
            return JSONResponse(
                status_code=403,
                content=_error(
                    None,
                    10002,
                    "Origin not allowed",
                    {
                        "code": "origin_not_allowed",
                        "retryable": False,
                        "request_id": request_id,
                        "schema_version": "1",
                    },
                ),
                headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
            )
        try:
            message = json.loads(await request.body())
        except ValueError:
            return JSONResponse(status_code=400, content=_error(None, -32700, "Parse error"))
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return JSONResponse(status_code=400, content=_error(None, -32600, "Invalid request"))
        method = message["method"]
        raw_id = message.get("id")
        if raw_id is not None and (not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool)):
            return JSONResponse(status_code=400, content=_error(None, -32600, "Invalid request"))
        id_ = raw_id
        request.state.jsonrpc_id = id_

        raw_params = message.get("params")
        params = {} if raw_params is None else raw_params
        meta = params.get("_meta") if isinstance(params, dict) else None
        meta_version = meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None

        header_version = request.headers.get("mcp-protocol-version")
        if (
            header_version is not None
            and header_version not in ACCEPTED_HEADER_VERSIONS
            and method != "initialize"
        ):
            return JSONResponse(
                status_code=400,
                content=_error(
                    id_,
                    -32600,
                    f"Unsupported MCP-Protocol-Version: {header_version}",
                    {
                        "code": "unsupported_protocol_version",
                        "supportedVersions": SUPPORTED_PROTOCOL_VERSIONS,
                    },
                ),
            )

        is_2026 = (
            header_version == "2026-07-28"
            or meta_version == "2026-07-28"
            or (method == "server/discover" and request.headers.get("mcp-method") is not None)
        )

        if is_2026:
            if id_ is None:
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        None,
                        -32600,
                        "Notifications are not supported on this stateless endpoint.",
                        {"code": "notifications_unsupported"},
                    ),
                )
            if method == "initialize":
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        id_,
                        -32600,
                        "initialize is not supported under MCP 2026-07-28; use server/discover.",
                        {"code": "unsupported_method_for_version"},
                    ),
                )
            mcp_method = request.headers.get("mcp-method")
            mcp_name = request.headers.get("mcp-name")
            if header_version != "2026-07-28" or mcp_method != method:
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        id_,
                        -32600,
                        "MCP HTTP headers must match the JSON-RPC envelope.",
                        {"code": "header_mismatch"},
                    ),
                )
            if method == "tools/call":
                param_name = params.get("name") if isinstance(params, dict) else None
                if not isinstance(param_name, str) or mcp_name != param_name:
                    return JSONResponse(
                        status_code=400,
                        content=_error(
                            id_,
                            -32600,
                            "Mcp-Name header must match params.name on tools/call.",
                            {"code": "header_mismatch"},
                        ),
                    )
            elif mcp_name is not None:
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        id_,
                        -32600,
                        "Mcp-Name header is only allowed on tools/call.",
                        {"code": "header_mismatch"},
                    ),
                )
            if method == "server/discover":
                if meta_version is not None and meta_version != "2026-07-28":
                    return JSONResponse(
                        status_code=400,
                        content=_error(
                            id_,
                            -32600,
                            "MCP-Protocol-Version header does not match params._meta protocolVersion.",
                            {"code": "header_mismatch"},
                        ),
                    )
                return _result(
                    id_,
                    {
                        "resultType": "complete",
                        "supportedVersions": SUPPORTED_PROTOCOL_VERSIONS,
                        "capabilities": {"tools": {}},
                        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
                        "instructions": INSTRUCTIONS,
                        "ttlMs": 0,
                        "cacheScope": "public",
                    },
                )
            if not isinstance(params, dict) or not isinstance(meta, dict):
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        id_,
                        -32602,
                        "params._meta is required under MCP 2026-07-28.",
                        {"code": "invalid_meta"},
                    ),
                )
            if not isinstance(meta_version, str):
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        id_,
                        -32602,
                        "params._meta must include io.modelcontextprotocol/protocolVersion.",
                        {"code": "invalid_meta"},
                    ),
                )
            if meta_version != "2026-07-28":
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        id_,
                        -32600,
                        "MCP-Protocol-Version header does not match params._meta protocolVersion.",
                        {"code": "header_mismatch"},
                    ),
                )
            client_caps = meta.get("io.modelcontextprotocol/clientCapabilities")
            client_info = meta.get("io.modelcontextprotocol/clientInfo")
            if not isinstance(client_caps, dict) or (
                client_info is not None and not isinstance(client_info, dict)
            ):
                return JSONResponse(
                    status_code=400,
                    content=_error(
                        id_,
                        -32602,
                        "params._meta must include an object io.modelcontextprotocol/clientCapabilities.",
                        {"code": "invalid_meta"},
                    ),
                )
        else:
            if id_ is None:
                return Response(status_code=202)  # notifications (for example notifications/initialized)
            if method == "initialize":
                requested = params.get("protocolVersion") if isinstance(params, dict) else None
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                return _result(
                    id_,
                    {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                        "instructions": INSTRUCTIONS,
                    },
                )

        if method == "ping":
            return _result(id_, {"resultType": "complete"} if is_2026 else {})

        if method == "tools/list":
            if not isinstance(params, dict):
                return JSONResponse(
                    status_code=400 if is_2026 else 200,
                    content=_error(
                        id_, -32602, "Invalid params", {"code": "invalid_input"} if is_2026 else None
                    ),
                )
            try:
                offset = _decode_cursor(params.get("cursor"))
            except _CursorError as err:
                return JSONResponse(
                    status_code=400 if is_2026 else 200,
                    content=_error(id_, -32602, err.message, {"code": err.code}),
                )
            catalog = TOOLS_2026 if is_2026 else TOOLS_2025
            page = catalog[offset : offset + page_size]
            next_offset = offset + page_size
            next_cursor = _encode_cursor(next_offset) if next_offset < len(catalog) else None
            if is_2026:
                body_26: dict = {
                    "resultType": "complete",
                    "tools": page,
                    "ttlMs": 0,
                    "cacheScope": "public",
                }
                if next_cursor is not None:
                    body_26["nextCursor"] = next_cursor
                return _result(id_, body_26)
            body: dict = {"tools": page}
            if next_cursor is not None:
                body["nextCursor"] = next_cursor
            return _result(id_, body)

        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return JSONResponse(
                    status_code=400 if is_2026 else 200,
                    content=_error(
                        id_, -32602, "Invalid params", {"code": "invalid_input"} if is_2026 else None
                    ),
                )
            access = getattr(request.state, "access", None)
            tier = (access or {}).get("tier") or {}
            if getattr(request.state, "law_check_internal", False) and tier.get("id") != "law-check":
                return JSONResponse(
                    status_code=403,
                    content={
                        "jsonrpc": "2.0",
                        "id": id_,
                        "error": {
                            "code": 10002,
                            "message": "Law check plan required.",
                            "data": {"code": "plan_mismatch"},
                        },
                    },
                    headers={"Cache-Control": "no-store"},
                )
            allowance = tier.get("allowance") or {}
            keyed = access is not None and allowance.get("withdrawals") != "not_told"
            raw_source_ids = allowance.get("source_ids")
            allowed_source_ids = (
                frozenset(raw_source_ids)
                if "source_ids" in allowance and raw_source_ids is not None
                else None
            )
            raw_anchor_bytes = allowance.get("anchor_text_bytes")
            anchor_text_bytes = (
                raw_anchor_bytes
                if isinstance(raw_anchor_bytes, int)
                and not isinstance(raw_anchor_bytes, bool)
                and raw_anchor_bytes > 0
                else None
            )
            try:
                # Database work runs in a worker thread, never on the event loop.
                payload = await asyncio.wait_for(
                    run_in_threadpool(
                        call_tool,
                        params["name"],
                        params.get("arguments", {}),
                        reach(request),
                        keyed,
                        allowed_source_ids,
                        anchor_text_bytes,
                    ),
                    timeout=tool_timeout,
                )
            except TimeoutError:
                return JSONResponse(
                    status_code=503,
                    content=_error(
                        id_,
                        10004,
                        "Tool execution timed out. Try again shortly.",
                        {
                            "code": "temporarily_unavailable",
                            "retryable": True,
                            "request_id": request_id,
                            "schema_version": "1",
                        },
                    ),
                    headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
                )
            except _VerifyError as refused:
                return _result(
                    id_,
                    _tool_error(
                        refused.code,
                        refused.message,
                        extra=refused.extra,
                        include_result_type=is_2026,
                    ),
                )
            except TooLarge as refused:
                return _result(
                    id_, _tool_error("result_too_large", str(refused), include_result_type=is_2026)
                )
            except unavailable:
                return _result(
                    id_,
                    _tool_error(
                        "temporarily_unavailable",
                        "Catalogue authority unavailable",
                        include_result_type=is_2026,
                    ),
                )
            except FeedError as refused:
                return _result(
                    id_,
                    _tool_error(
                        refused.code,
                        f"{refused.code}: {refused.detail}",
                        refused.retry_after,
                        include_result_type=is_2026,
                    ),
                )
            except OutsidePlanLimited as outside:
                request.state.refusal = "rate_limited"
                message = f"{outside} Retry after {outside.retry_after} seconds."
                return _result(
                    id_,
                    _tool_error(
                        "rate_limited",
                        message,
                        outside.retry_after,
                        extra={"retryable": True, "manage_url": MANAGE_URL},
                        include_result_type=is_2026,
                    ),
                )
            except OutsidePlan as outside:
                request.state.refusal = "jurisdiction_not_included"
                return _result(
                    id_,
                    _tool_error(
                        "jurisdiction_not_included",
                        str(outside),
                        extra={"retryable": False, "manage_url": MANAGE_URL},
                        include_result_type=is_2026,
                    ),
                )
            except ToldWithdrawn as told:
                return _result(
                    id_, _tool_error("withdrawn", WITHDRAWN[told.argument], include_result_type=is_2026)
                )
            except LookupError:
                return _result(
                    id_, _tool_error("unavailable", "Source not available", include_result_type=is_2026)
                )
            except ResultTooLarge:
                message = "result_too_large: the answer would be larger than one response may be."
                return _result(id_, _tool_error("result_too_large", message, include_result_type=is_2026))
            except ValueError as invalid:
                # check_claim says which argument is wrong, so a model can correct its next call.
                from retrieval.verify import InvalidClaimArguments

                message = str(invalid) if isinstance(invalid, InvalidClaimArguments) else "Invalid arguments"
                return JSONResponse(
                    status_code=400 if is_2026 else 200,
                    content=_error(id_, -32602, message, {"code": "invalid_input"} if is_2026 else None),
                )
            if payload is None:
                return JSONResponse(
                    status_code=400 if is_2026 else 200,
                    content=_error(
                        id_,
                        -32602,
                        "Unknown tool or invalid arguments",
                        {"code": "invalid_input"} if is_2026 else None,
                    ),
                )
            if is_2026:
                return _result(
                    id_,
                    {
                        "resultType": "complete",
                        "content": [{"type": "text", "text": _summary_text(params["name"], payload)}],
                        "structuredContent": payload,
                        "isError": False,
                    },
                )
            return _result(id_, _tool_result(payload))

        if is_2026:
            return JSONResponse(
                status_code=404,
                content=_error(id_, -32601, "Method not found", {"code": "method_not_found"}),
            )
        return _error(id_, -32601, "Method not found")

    return router
