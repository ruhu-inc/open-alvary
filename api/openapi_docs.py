"""The words of REST v1's OpenAPI document: what each route, parameter and field means, which
refusals each route can give, and the shapes of the routes without a response model (changes, plans,
releases). api.v1 applies them to the generated document, which the developer documentation's API
reference is built from (scripts/docs_reference.py), so the two never differ.
"""

import json
from copy import deepcopy

PROBLEM = {"$ref": "#/components/schemas/Problem"}

# Refusals any route can give through the credential gate and admission.
GATE = {
    "401": "No key or token, or one that is unknown, revoked, expired or for another resource "
    "(`credential_required`, `invalid_credentials`). WWW-Authenticate says where to get one.",
    "403": "The organisation has no access, or the record is outside it (`entitlement_required`, "
    "`entitlement_lapsed`, `jurisdiction_not_included`, `insufficient_scope`, `account_inactive`, "
    "`organisation_suspended`).",
    "429": "An allowance is used up for now (`rate_limited`). Retry-After says when to try again.",
    "503": "Access or the corpus cannot be checked right now (`temporarily_unavailable`, "
    "`capacity_exhausted`). Try again after Retry-After.",
}
BAD = {"400": "A parameter is unknown, malformed or out of range (`invalid_input`)."}
MISSING = {"404": "There is no such record, or it is not public (`unavailable`)."}
TOO_LARGE = {"413": "The answer would be larger than one response may be (`result_too_large`)."}
FEED = {
    "400": "A parameter, filter, cursor or token is not valid (`invalid_input`, `invalid_filter`, "
    "`unsupported_filter`, `invalid_cursor`).",
    "409": "The cursor expired, or the feed was reset (`stale_cursor`, `feed_reset`).",
    "410": "Changes after this position are no longer kept (`replay_window_expired`).",
}

# Filters shared by the change routes: repeat a parameter for several values.
FILTERS = [
    (
        "jurisdiction",
        "Jurisdiction IDs, such as `ng`; a country includes its states and local law. Up to 50.",
    ),
    ("work", "Record IDs, such as `ng-foi-act-2011`. Up to 50."),
    (
        "type",
        "Kinds of change: `catalogue_added`, `metadata_corrected`, `text_version_published`, "
        "`withdrawal`, `coverage_changed`.",
    ),
    ("class", "Document types, such as `act` or `regulation`. Up to 50."),
    ("language", "Languages of the affected text, such as `en`. Up to 50."),
    ("q", "A keyword: whole words in the title, citation or summary, ignoring case. Up to 200 characters."),
]


def _query(name: str, description: str, schema: dict | None = None, repeat: bool = False) -> dict:
    schema = schema or {"type": "string"}
    if repeat:
        schema = {"type": "array", "items": schema}
    parameter = {"name": name, "in": "query", "required": False, "description": description, "schema": schema}
    if repeat:
        parameter |= {"style": "form", "explode": True}
    return parameter


FILTER_PARAMETERS = [_query(name, text, repeat=name != "q") for name, text in FILTERS]
PAGING = [
    _query(
        "limit", "Changes per page, 1 to 100. Default 25.", {"type": "integer", "minimum": 1, "maximum": 100}
    ),
    _query("cursor", "The `next_cursor` of the previous page, to continue the same window (15 minutes)."),
    _query("resume_token", "The `resume_token` of an earlier answer: changes after it, in a new window."),
    _query("start", "`now`: no changes, only a resume token at the latest position.", {"enum": ["now"]}),
]

# Path → the operation's ID, tag, summary, description, parameters, refusals and, for routes
# without a response model, its answer's schema (a component name, or a content type and schema).
OPERATIONS: dict[str, dict] = {
    "/search": {
        "operationId": "search",
        "tag": "Catalogue",
        "summary": "Search the catalogue",
        "description": "Records whose title or citation contains the words, or whose cleared full text "
        "does, a page at a time. Without `jurisdiction`, a search covers your plan's countries.",
        "parameters": {
            "q": "Words to find, 1 to 200 characters. Case and spacing don't matter.",
            "jurisdiction": "One jurisdiction ID, such as `ng` or `rw`. A country outside your plan "
            "answers at the Starter rate.",
            "access": "`all` (default), `open` (records with cleared full text) or `metadata` (records "
            "without).",
            "limit": "Records per page, 1 to 100. Default 25.",
            "offset": "How many records to skip: 0 for the first page, then the previous offset plus limit.",
        },
        "errors": BAD | TOO_LARGE,
    },
    "/sources": {
        "operationId": "list_sources",
        "tag": "Catalogue",
        "summary": "List records",
        "description": "Catalogued records a page at a time, with the same filters as search. Without "
        "`jurisdiction`, the list covers your plan's countries.",
        "parameters": {
            "jurisdiction": "One jurisdiction ID, such as `ng` or `rw`.",
            "access": "`all` (default), `open` (records with cleared full text) or `metadata` (records "
            "without).",
            "limit": "Records per page, 1 to 100. Default 50.",
            "offset": "How many records to skip: 0 for the first page, then the previous offset plus limit.",
        },
        "errors": BAD | TOO_LARGE,
    },
    "/sources/{source_id}": {
        "operationId": "get_source",
        "tag": "Catalogue",
        "summary": "Get a record",
        "description": "One record: its title, citation, type, legal status, official link and rights "
        "status.",
        "parameters": {"source_id": "The record's ID, such as `ng-foi-act-2011`."},
        "errors": MISSING,
    },
    "/sources/{source_id}/versions": {
        "operationId": "get_versions",
        "tag": "Catalogue",
        "summary": "Get a record's texts",
        "description": "The record's text versions cleared for reuse, each with its full text; the "
        "parts, chapters and sections located in that text; and its rights record. A record whose text "
        "isn't cleared answers no versions and a notice saying why. With `as_of` or `language`, only the "
        "text they select, or the alternatives and why (`selection`): a text with no effective date is "
        "never taken to be in force on a date.",
        "parameters": {
            "source_id": "The record's ID.",
            "as_of": "A date, such as `2024-01-01`: the text in force on it, from each text's effective dates.",
            "language": "A language, such as `en`; `en` also matches `en-NG`.",
            "expression_id": "One language edition, such as `ng-foi-act-2011:en`, from Get a record's editions.",
        },
        "errors": MISSING | BAD | TOO_LARGE,
    },
    "/sources/{source_id}/expressions": {
        "operationId": "get_expressions",
        "tag": "Catalogue",
        "summary": "Get a record's editions",
        "description": "The record's language editions with a public text: for each, its texts and the one to "
        "read when no date is given (`preferred_version_id`), with the reason. A preferred text is a reading "
        "default, not a claim that the edition is authoritative. An edition's ID is `{source_id}:{language}`; "
        "pass it as `expression_id` to Get a record's texts.",
        "parameters": {"source_id": "The record's ID."},
        "errors": MISSING,
    },
    "/versions/{version_id}/text": {
        "operationId": "get_version_text",
        "tag": "Catalogue",
        "summary": "Get a text",
        "description": "One text version's full text, exactly: `text/plain; charset=utf-8`, the bytes whose "
        "SHA-256 is the version's `content_hash` and which structure offsets refer to. The `ETag` is that "
        "hash: send it back as `If-None-Match` and an unchanged text answers `304` with no body, after the "
        "same check that it is still public. A version that is not public answers like one that does "
        "not exist.",
        "path": {"version_id": "The version's ID, from a record's texts."},
        "answer": ("text/plain", {"type": "string"}),
        "errors": MISSING | TOO_LARGE,
    },
    "/versions/{version_id}/structure": {
        "operationId": "get_version_structure",
        "tag": "Catalogue",
        "summary": "Get a text's structure",
        "description": "The parts, sections, articles and other parts of one text version, in document "
        "order, a page at a time, each with its span in the text and its locator. Each part's `id` is "
        "also an anchor: get it at `/anchors/{anchor_id}`. Answers carry an `ETag` for `If-None-Match`.",
        "path": {"version_id": "The version's ID, from a record's texts."},
        "parameters": {
            "limit": "Parts per page, 1 to 500. The default is 100.",
            "cursor": "The `next_cursor` of the previous page, to continue it.",
        },
        "errors": MISSING | BAD,
    },
    "/anchors/{anchor_id}": {
        "operationId": "get_anchor",
        "tag": "Catalogue",
        "summary": "Get one part of a text exactly",
        "description": "One part of a text, such as a section: its words, its span in the text and the "
        "unit of the offsets, its locator and printed number, the hash of its own bytes for checking a "
        "quotation, a citation, the official file and the attribution the rights record asks for. The "
        "words are never shortened. A part of a text that is not public answers like one that does "
        "not exist.",
        "path": {"anchor_id": "The part's ID, from a text's structure."},
        "errors": MISSING | TOO_LARGE,
    },
    "/sources/{source_id}/relationships": {
        "operationId": "get_relationships",
        "tag": "Catalogue",
        "summary": "Get a record's history",
        "description": "Whether the record is current, and how it relates to other records, a page at a "
        "time: the laws it repeals or amends and the law it is made under; the later laws that repealed or "
        "amended it; the instruments made under it; and, when it has been repealed or replaced, the record "
        "in force in its place now. Links one step away only (depth 1), between public records that a "
        "reviewer has checked against the official text.",
        "parameters": {
            "source_id": "The record's ID.",
            "kind": "Only links of this kind, such as `repealed_by` or `enables`.",
            "limit": "Links per page, 1 to 100. The default is 25.",
            "cursor": "The `next_cursor` of the previous page, to continue it.",
        },
        "errors": MISSING | BAD,
    },
    "/rights/{source_id}": {
        "operationId": "get_rights",
        "tag": "Catalogue",
        "summary": "Get a record's rights",
        "description": "The rights record behind a record (licence, permissions, evidence, review) and "
        "the decision taken from it: whether the catalogue record and the text may be shown, and why.",
        "parameters": {"source_id": "The record's ID."},
        "errors": MISSING,
    },
    "/rights/{source_id}/history": {
        "operationId": "get_rights_history",
        "tag": "Catalogue",
        "summary": "Get a record's rights history",
        "description": "Every revision of the record's rights record, newest first, as recorded when the "
        "catalogue changed; revisions are never edited. Text and withdrawal events name the revision in "
        "force (`rights_revision`), so a copy of a text can say which rights record it was published "
        "under.",
        "parameters": {"source_id": "The record's ID."},
        "errors": MISSING,
    },
    "/search/text": {
        "operationId": "search_text",
        "tag": "Catalogue",
        "summary": "Search published texts",
        "description": "Passages of published texts that match the words, best first: each hit is a section, "
        "article or schedule paragraph, with a snippet of the exact text, its byte span and the anchor to "
        "get it whole. Only published texts are searched; `coverage` says how many records in the scope "
        "have one, and `status` tells no result (`no_result`) apart from a scope with no published text "
        "(`unsupported_scope`) and a search that could not run (`degraded`). A query that reads as a "
        "citation of a part, such as `NDPA s. 23(1)(a)`, puts that part first. Without `jurisdiction`, "
        "a search covers your plan's countries.",
        "parameters": {
            "q": 'Words to find, 1 to 2,000 characters: `"exact phrase"`, `-without`, `this OR that`.',
            "jurisdiction": "One jurisdiction ID, such as `ng` or `rw`. A country outside your plan "
            "answers at the Starter rate.",
            "language": "Texts in this language, such as `en`; `en` also matches `en-NG`.",
            "type": "Document types, such as `act` or `regulation`. Repeat for several.",
            "status": "Legal statuses: `current`, `historical`, `repealed`, `superseded` or `unknown`. "
            "Repeat for several.",
            "as_of": "A date, such as `2024-01-01`: only texts in force on it, from their effective dates. "
            "A text with no effective date is left out.",
            "year_from": "Only laws made in this year or later.",
            "year_to": "Only laws made in this year or earlier.",
            "source_id": "Only this record's texts.",
            "version_id": "Only this text version.",
            "limit": "Passages per page, 1 to 100. Default 25.",
            "cursor": "The `next_cursor` of the previous page, to continue it.",
        },
        "errors": BAD,
    },
    "/retrieve": {
        "operationId": "retrieve_evidence",
        "tag": "Retrieval",
        "summary": "Retrieve evidence",
        "description": "Ranked legal evidence for a question, for an application that writes answers: each "
        "unit is a provision (or the text a part holds between its provisions, or a whole law's title and "
        "preamble), with its exact words or an excerpt within `max_evidence_bytes`, its byte span and "
        "hashes to check it, what quoting it asks for, whether it was in force, the definitions it relies "
        "on, and why it ranks where it does. A query that cites a part, names a law or asks what a term "
        "means is routed to that first (`query.routes`). The same request over the same revisions gives "
        "the same answer. Nothing is generated: only published texts' words. Without `jurisdiction`, it "
        "covers your plan's countries.",
        "parameters": {
            "q": "The question or words, 1 to 2,000 characters.",
            "jurisdiction": "Jurisdiction IDs, such as `ng`. Repeat for several, up to 10; with two or more, "
            "results alternate between them. A country outside your plan answers at the Starter rate.",
            "language": "Texts in this language, such as `en`; `en` also matches `en-NG`.",
            "type": "Document types, such as `act` or `regulation`. Repeat for several.",
            "status": "Legal statuses: `current`, `historical`, `repealed`, `superseded` or `unknown`. "
            "Repeat for several.",
            "as_of": "A date, such as `2024-01-01`: only texts in force on it, and currentness assessed at "
            "it. Without it, nothing is assumed about the date.",
            "year_from": "Only laws made in this year or later.",
            "year_to": "Only laws made in this year or earlier.",
            "source_id": "Only this record's texts.",
            "version_id": "Only this text version.",
            "max_units": "How many evidence units, 1 to 25. Default 10.",
            "max_evidence_bytes": "The most bytes of words (evidence and definitions, each counted once), "
            "4,096 to 262,144. Default 49,152. A unit that does not fit is an excerpt, then units stop.",
            "include": "What to add to each unit: `context` (headings, qualifications), `expansion` "
            "(definitions, references) and `ranking` (why it ranks there). Repeat for several; all by "
            "default.",
        },
        "errors": BAD,
    },
    "/currentness": {
        "operationId": "check_currentness",
        "tag": "Retrieval",
        "summary": "Check cited parts",
        "description": "Before showing an answer that cites the corpus: for each cited part (up to 100), "
        "whether it is still published with the same words, whether a newer text of the same edition "
        "exists (with the same part's anchor there and whether its words changed), and whether it was in "
        "force on a date. A part that is not public now and one that never was get the same answer, "
        "`unavailable`.",
        "body": "The parts to check: `items`, each an `anchor_id`, or a `version_id` with a `locator`, "
        "with the `span_hash` you hold and an optional `as_of` date.",
        "errors": BAD,
    },
    "/citations/{citation}": {
        "operationId": "resolve_citation",
        "tag": "Catalogue",
        "summary": "Resolve a citation",
        "description": "The records a citation refers to. Case and spacing don't matter; encode the "
        "citation for a URL (spaces as `%20`, `/` stays as it is). A citation of a part, such as "
        "`NDPA s. 23(1)(a)`, `art. 12` or `First Schedule, para. 3` after the record's citation, also "
        "answers `provision`: the part in the record's public text, with its anchor and exact span, or "
        "the alternatives and why. Nothing is chosen between alternatives.",
        "parameters": {
            "citation": "The citation, such as `Act No. 4 of 2011` or `FOI Act 2011, s. 1(1)`.",
            "as_of": "A date, such as `2024-01-01`: look for the part only in texts in force on it.",
            "language": "Look for the part only in texts in this language, such as `en`.",
        },
        "errors": BAD,
    },
    "/jurisdictions": {
        "operationId": "list_jurisdictions",
        "tag": "Catalogue",
        "summary": "List jurisdictions",
        "description": "The jurisdictions the catalogue holds: countries and their states or regions, "
        "with their legal systems and languages.",
        "errors": {},
    },
    "/coverage": {
        "operationId": "get_coverage",
        "tag": "Catalogue",
        "summary": "Get coverage",
        "description": "How many records the catalogue holds, how many have cleared full text, and how "
        "many in each jurisdiction.",
        "errors": {},
    },
    "/releases": {
        "operationId": "list_releases",
        "tag": "Releases",
        "summary": "List data releases",
        "description": "The versioned data releases still available: their scope, files and checksums. "
        "Download a file from `download_base` + `/` + its name; no key is needed for downloads.",
        "errors": {},
        "answer": {"type": "array", "items": {"$ref": "#/components/schemas/Release"}},
    },
    "/tiers": {
        "operationId": "list_plans",
        "tag": "Plans",
        "summary": "List plans",
        "description": "The plans and their limits. Needs no key.",
        "errors": {},
        "public": True,
        "answer": "PlanCatalogue",
    },
    "/quota-policy": {
        "operationId": "get_quota_policy",
        "tag": "Plans",
        "summary": "Get the quota policy",
        "description": "How limits are counted. Needs no key.",
        "errors": {},
        "public": True,
        "answer": "QuotaPolicy",
    },
    "/changes": {
        "operationId": "list_changes",
        "tag": "Changes",
        "summary": "List changes",
        "description": "Changes in the order they were made, oldest first: records added, corrected or "
        "withdrawn, texts published and coverage changes. Keep the `resume_token` and send it next time "
        "for only newer changes. Send at most one of `cursor`, `resume_token` and `start`. The same "
        "route answers without a key at https://open.alvary.ai/api/changes.",
        "extra": FILTER_PARAMETERS + PAGING,
        "errors": FEED,
        "answer": "ChangePage",
    },
    "/changes/recent": {
        "operationId": "list_recent_changes",
        "tag": "Changes",
        "summary": "List recent changes",
        "description": "The latest changes of the last 90 days, newest first (up to 50), with the same "
        "filters. For showing what's new, not for keeping up: use List changes for that.",
        "extra": FILTER_PARAMETERS,
        "errors": FEED,
        "answer": "RecentChanges",
    },
    "/changes/feed.atom": {
        "operationId": "get_atom_feed",
        "tag": "Changes",
        "summary": "Atom feed of changes",
        "description": "The latest changes of the last 90 days as an Atom feed, newest first, with the "
        "same filters. Public at https://open.alvary.ai/api/changes/feed.atom.",
        "extra": FILTER_PARAMETERS,
        "errors": FEED,
        "answer": ("application/atom+xml", {"type": "string"}),
    },
    "/changes/feed.rss": {
        "operationId": "get_rss_feed",
        "tag": "Changes",
        "summary": "RSS feed of changes",
        "description": "The same as the Atom feed, as RSS 2.0.",
        "extra": FILTER_PARAMETERS,
        "errors": FEED,
        "answer": ("application/rss+xml", {"type": "string"}),
    },
    "/changes/{event_id}": {
        "operationId": "get_change",
        "tag": "Changes",
        "summary": "Get a change",
        "description": "One change, at its latest revision.",
        "path": {"event_id": "The change's `event_id`."},
        "errors": MISSING,
        "answer": "ChangeEvent",
    },
    "/change-coverage": {
        "operationId": "get_change_coverage",
        "tag": "Changes",
        "summary": "Get change coverage",
        "description": "When recording changes began, how far back they can be replayed, and what is "
        "held for each jurisdiction. Changes cover what Open Alvary holds, not every change in a "
        "country's law.",
        "errors": {},
        "answer": "ChangeCoverage",
    },
}

TAGS = [
    {"name": "Catalogue", "description": "Records, their texts and rights, citations and coverage."},
    {
        "name": "Retrieval",
        "description": "Evidence for applications that write answers: ranked parts of published texts with "
        "everything needed to cite and check them.",
    },
    {"name": "Changes", "description": "What changed in the catalogue, in order, and feeds."},
    {"name": "Plans", "description": "Plans, limits and how limits are counted."},
    {"name": "Releases", "description": "Versioned data releases to download."},
]

S = "string"
NULL_S = {"type": [S, "null"]}

# Shapes of the answers that have no response model.
SCHEMAS: dict[str, dict] = {
    "Problem": {
        "type": "object",
        "description": "A refusal (RFC 9457). Its `code` is explained at https://open.alvary.ai/docs/errors.",
        "properties": {
            "type": {"type": S, "description": "A link to the code's explanation."},
            "title": {"type": S, "description": "Always `Request refused`."},
            "status": {"type": "integer", "description": "The HTTP status."},
            "detail": {"type": S, "description": "What happened and what to do, in a sentence."},
            "code": {"type": S, "description": "The error code, such as `rate_limited`."},
            "retryable": {"type": "boolean", "description": "Whether the same request may succeed later."},
            "request_id": {"type": S, "description": "Quote it when you ask us about this request."},
            "retry_after_seconds": {"type": "integer", "description": "When to try again, as Retry-After."},
        },
        "required": ["type", "title", "status", "detail", "code"],
    },
    "ChangeWork": {
        "type": "object",
        "description": "The record a change is about.",
        "properties": {
            "id": {"type": S, "description": "The record's ID."},
            "title": {"type": S, "description": "Its title."},
            "citation": {"type": S, "description": "Its citation."},
            "legal_status": {
                "type": S,
                "description": "Whether it is in force now, as the catalogue has it: `current`, `historical`, "
                "`repealed`, `superseded` or `unknown`.",
            },
            "jurisdiction_id": {"type": S, "description": "Its jurisdiction."},
            "document_class": {"type": S, "description": "Its document type, such as `act`."},
            "language": {"type": S, "description": "Its language."},
            "url": {"type": S, "description": "Its page on the website."},
            "api_url": {"type": S, "description": "The record in this API: Get a record."},
        },
    },
    "ChangeEvent": {
        "type": "object",
        "description": "One change. Deduplicate by `feed_epoch`, `event_id` and `event_revision`.",
        "properties": {
            "schema_version": {
                "type": S,
                "description": "The version of this shape, `change-event-1`. New event types and fields may "
                "appear within a version: ignore the ones you do not know.",
            },
            "event_id": {"type": S, "description": "The change's ID."},
            "event_revision": {
                "type": "integer",
                "description": "1, or higher when we corrected the change itself (see `supersedes`).",
            },
            "event_type": {
                "type": S,
                "description": "What changed: `catalogue_added` (a record added: an old law newly catalogued, "
                "not a new law), `metadata_corrected` (its catalogue details corrected), "
                "`text_version_published`, `legal_effect_recorded` (a checked repeal, amendment or "
                "supersession of the record by another public record), `withdrawal` (a record or text) or "
                "`coverage_changed` (a jurisdiction's coverage). More types may be added: ignore those you "
                "do not know; `/change-coverage` lists the ones recorded.",
            },
            "feed_epoch": {
                "type": S,
                "description": "The feed's epoch; it changes only when the feed is reset.",
            },
            "publication_revision": {
                "type": "integer",
                "description": "The catalogue's revision it was made in.",
            },
            "committed_at": {"type": S, "format": "date-time", "description": "When it was recorded."},
            "observed_at": NULL_S
            | {"description": "When the change was seen at the official source, if known."},
            "publisher_published_at": NULL_S | {"description": "When the publisher published it, if known."},
            "legal_effective": NULL_S | {"description": "When it takes legal effect, if known."},
            "jurisdiction": {
                "type": ["object", "null"],
                "description": "The jurisdiction: {id, name}.",
                "properties": {"id": {"type": S}, "name": {"type": S}},
            },
            "work": {
                "oneOf": [{"$ref": "#/components/schemas/ChangeWork"}, {"type": "null"}],
                "description": "The record, or null when it may no longer be named.",
            },
            "version": {
                "type": ["object", "null"],
                "description": "For a published or withdrawn text: {id, content_hash, language}.",
                "properties": {"id": {"type": S}, "content_hash": {"type": S}, "language": {"type": S}},
            },
            "rights_revision": {
                "type": ["integer", "null"],
                "description": "For a published or withdrawn text: the revision of the record's rights record "
                "in force (see `/rights/{source_id}/history`).",
            },
            "changed_fields": {
                "type": "array",
                "items": {"type": S},
                "description": "For a correction: the fields that changed. A correction never changes legal text.",
            },
            "change_reason": NULL_S
            | {
                "description": "Why, when known: `restored`, `first_text`, `source_revision`, "
                "`extraction_correction`, `redaction`, `corrected` or `unknown`."
            },
            "supersedes": {
                "type": ["object", "null"],
                "description": "For a corrected change: the {event_id, event_revision} it replaces.",
            },
            "summary": {"type": S, "description": "One plain sentence saying what changed."},
            "sources": {
                "type": "array",
                "items": {"type": S},
                "description": "Official links for the record.",
            },
            "withdrawal": {
                "type": ["object", "null"],
                "description": "For a withdrawal: {scope_disclosure, action, reason_code}. A text version "
                "withdrawn from a record that stays public is `named`, with the version in `version`, `action` "
                "`stop_serving` (stop serving copies of that text) and `reason_code` `rights_changed` (its "
                "rights record changed or lapsed) or `correction` (its approval was withdrawn). When "
                "`scope_disclosure` is `revalidate_releases`, check your copies against a current release.",
            },
            "legal_effect": {
                "type": ["object", "null"],
                "description": "For a legal effect: {kind, by, effective_date, evidence_url}. `kind` is `repeal`, "
                "`amendment` or `supersession` of the record in `work`; `by` is the record that had the effect "
                "{id, title, citation}; the date is when it took effect, when known. A reviewer checked it "
                "against the official text. A later correction of it has the same `event_id`. For a reviewed "
                "event on provisions, `kind` is the event (`commenced`, `repealed`, `suspended`, `revived`, "
                "…), with `scope` (`whole` or `explicit_nodes`), `provisions` (their locators), `precision` "
                "of the date and `review_state` (`withdrawn` when the event was withdrawn); `by` is present "
                "when another law asserted it.",
            },
            "previous": {
                "type": "object",
                "description": "For a correction: the previous values of the fields that changed, among "
                "`legal_status`, `rights_status`, `document_type` and `year`.",
            },
            "version_diff": {
                "type": ["object", "null"],
                "description": "For a text that replaces an earlier one: {prior_version_id, added, removed, "
                "changed, truncated}, the locators of the provisions added, removed and changed (their words "
                "differ), in document order, at most 200 each.",
            },
        },
    },
    "ChangePage": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/ChangeEvent"},
                "description": "This page's changes, oldest first. A page may be short or empty.",
            },
            "has_more": {"type": "boolean", "description": "Whether to ask again with `next_cursor` now."},
            "next_cursor": {
                "type": S,
                "description": "Send as `cursor` for the next page (within 15 minutes).",
            },
            "resume_token": {"type": S, "description": "Keep it; send it later for only newer changes."},
            "feed_epoch": {"type": S, "description": "The feed's epoch."},
        },
    },
    "RecentChanges": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/ChangeEvent"},
                "description": "The latest changes, newest first.",
            }
        },
    },
    "ChangeCoverage": {
        "type": "object",
        "properties": {
            "feed_started_at": NULL_S | {"description": "When recording began."},
            "feed_epoch": {"type": S, "description": "The feed's epoch."},
            "earliest_replayable_at": NULL_S | {"description": "The oldest change still kept."},
            "supported_event_types": {
                "type": "array",
                "items": {"type": S},
                "description": "Kinds of change.",
            },
            "supported_filters": {
                "type": "array",
                "items": {"type": S},
                "description": "Filters you can use.",
            },
            "jurisdictions": {
                "type": "array",
                "description": "Per jurisdiction: {id, name, works, readable_works, last_change_at, "
                "last_checked_at, expected_cadence, known_gaps}.",
                "items": {"type": "object"},
            },
            "notice": {"type": S, "description": "What the feed covers."},
        },
    },
    "Plan": {
        "type": "object",
        "properties": {
            "id": {"type": S, "description": "The plan's ID, such as `starter`."},
            "name": {"type": S, "description": "Its name."},
            "description": {"type": S, "description": "Who it is for."},
            "includes": {"type": "array", "items": {"type": S}, "description": "`rest`, `mcp`."},
            "allowance": {
                "type": "object",
                "description": "Its limits: requests_per_minute, burst, concurrency, bytes_per_day, "
                "jurisdictions (countries of your choice, when limited), watches (monitors), digest, "
                "shared_watches, webhook_endpoints.",
            },
            "listing": {
                "type": "object",
                "description": "How the console shows it: order, for, price, action, note.",
            },
        },
    },
    "PlanCatalogue": {
        "type": "object",
        "properties": {
            "tiers": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/Plan"},
                "description": "The plans.",
            }
        },
    },
    "QuotaPolicy": {
        "type": "object",
        "properties": {
            "enforcement": {"type": S, "description": "`organisation`: limits are per organisation."},
            "algorithm": {"type": S, "description": "`token_bucket`."},
            "grouping": {"type": S, "description": "What shares a limit."},
            "bytes": {
                "type": "object",
                "description": "How data a day is counted: a token bucket holding one day's `bytes_per_day`, "
                "refilled evenly over 24 hours, charged with each response body's bytes as sent.",
                "additionalProperties": {"type": S},
            },
            "note": {"type": S, "description": "The policy, in words."},
        },
    },
    "Release": {
        "type": "object",
        "description": "A release's manifest.",
        "properties": {
            "download_base": {"type": S, "description": "Where its files are; append `/` and a file name."},
            "files": {"type": "object", "description": "Its files, with their SHA-256 checksums."},
        },
        "additionalProperties": True,
    },
}

# Field descriptions of the response models, by schema.
FIELDS: dict[str, dict[str, str]] = {
    "EvidencePack": {
        "schema_version": "`oa-evidence-1`.",
        "status": "`results`; `no_result` (published texts were searched and none matched); "
        "`unsupported_scope` (no published text in this scope); or `degraded` (the search could not run).",
        "query": "The question as searched, the routes taken and the filters applied.",
        "revisions": "Everything the answer depends on; the same request and revisions give the same answer.",
        "coverage": "What was searched, what could not run and what was left out.",
        "evidence": "The evidence units, best first.",
        "catalogue": "Records that match but have no published text: identity and official link only.",
        "next_steps": "Hints for a follow-up request, such as an ambiguous citation's candidates.",
        "bytes": "The words sent: unique bytes (each span once), bytes as transmitted, and the budget.",
    },
    "EvidenceQuery": {
        "text": "The question, with spacing normalised.",
        "routes": "The routes taken, in order: `citation`, `instrument`, `definition`, `topic`, and the "
        "modifiers `historical` and `comparative`.",
        "filters": "The filters applied, as given or as your plan set them.",
        "as_of": "The date asked about, when given.",
    },
    "EvidenceRevisions": {
        "dataset": "The change log's epoch and latest publication revision: any published change moves it.",
        "router": "The routing rules, such as `oa-route-1`.",
        "ranking": "The ranking profile, such as `oa-rank-2`.",
        "analyzer": "The text analysis, such as `oa-search-3`.",
        "engine": "The search engine that ran the word and meaning searches (`vespa`), when one is configured; "
        "otherwise the system of record's own search ran them.",
        "vector": "The semantic index profile, when one is active.",
        "reranker": "The reranker profile, when one is active.",
    },
    "EvidenceCoverage": {
        "records_in_scope": "Records the filters cover.",
        "records_with_text": "Of those, records with a published text that was searched.",
        "texts_searched": "Published texts searched.",
        "dense_coverage": "The share of searched texts in the semantic index, when one is active.",
        "degraded": "Parts of the search that could not run or are not configured, such as "
        "`dense:not_configured`.",
        "unsupported": "Filters that could not be applied.",
        "incomplete_evidence": "Whether units were left out because the byte budget ran out.",
        "omitted_units": "How many units were left out for the byte budget.",
        "notices": "Plain-language notes on the answer.",
    },
    "EvidenceUnit": {
        "anchor_id": "The part: `/anchors/{anchor_id}` gives it whole; quote it with this ID.",
        "source_id": "The record.",
        "version_id": "The text version.",
        "expression_id": "The edition (`{source_id}:{language}`).",
        "unit": "`provision`; `container_run` (words a part holds between its provisions); or `document` "
        "(a law's title and preamble: evidence of which law is relevant, never of a provision).",
        "kind": "What the part is, such as `section`, `definition` or `article`.",
        "locator": "The part's stable key within the text, such as `sec_23__subsec_1`; the same in a later "
        "text of the same edition.",
        "pinpoint": "How the part is cited within the text, such as `s. 23(1)`.",
        "heading": "The part's printed heading, when it has one.",
        "citation": "The record's citation and the pinpoint.",
        "title": "The record's title.",
        "language": "The text's language.",
        "jurisdiction_id": "The record's jurisdiction.",
        "document_type": "The record's document type.",
        "official_url": "The official file the text was read from.",
        "text": "The words: exactly the text's bytes from `text_start_byte` to `text_end_byte`.",
        "text_start_byte": "Where `text` starts: a byte offset into the text in UTF-8.",
        "text_end_byte": "Where `text` ends: a byte offset into the text in UTF-8.",
        "complete": "Whether `text` is the whole unit; when false it is an excerpt (see `warnings`).",
        "start_byte": "Where the whole unit starts: a byte offset into the text in UTF-8.",
        "end_byte": "Where the whole unit ends: a byte offset into the text in UTF-8.",
        "offset_unit": "The unit of the offsets: `utf-8-bytes`.",
        "span_hash": "SHA-256 of the whole unit's bytes, `start_byte` to `end_byte`: send it to Check cited "
        "parts.",
        "content_hash": "The `content_hash` of the whole text.",
        "licence": "The terms the text is published under.",
        "attribution_required": "Whether reuse must credit the source.",
        "attribution_text": "The credit to give, when one is required.",
        "currentness": "Whether the part was in force, and what that rests on.",
        "authority": "The inputs to the authority signals.",
        "ranking": "Why the unit ranks where it does.",
        "context": "What travels with the unit and is never part of its words.",
        "warnings": "Notes on the unit, such as an excerpt or a newer text.",
    },
    "Currentness": {
        "state": "`in_force`, `not_in_force`, `partly_in_force`, `unknown` or `contested`.",
        "basis": "`provision` (reviewed events for this part), `record` (only the record's status for the "
        "whole text: never a claim about one part) or `none`.",
        "assessed_at": "The date assessed: `as_of`, or today.",
        "algorithm": "How the state was computed, such as `record-status-1`.",
        "profile": "The jurisdiction's rule profile, when one applied.",
        "evidence": "The events relied on.",
        "reasons": "Why the state is what it is, in plain language.",
    },
    "EvidenceAuthority": {
        "document_type": "The record's document type.",
        "instrument_rank": "The instrument's place in the hierarchy, 0 highest (constitution 0, Act 1, "
        "subsidiary legislation 2, guidance 3).",
        "court_relation": "For a decision, the court's relation to the jurisdiction asked about: `binding`, "
        "`persuasive` or `unknown`; `not_applicable` otherwise.",
    },
    "RankingExplanation": {
        "position": "The unit's position, from 1.",
        "router": "The routing rules.",
        "routes": "The routes taken.",
        "profile": "The ranking profile.",
        "arms": "Each search the unit came from and its rank there: `citation`, `instrument`, `definition`, "
        "`lexical` (every word), `lexical_any` (any word, rarer words weighing more; run when few units have "
        "every word), `document` or `dense` (meaning, where a semantic index is active).",
        "fused_score": "The fused score after the signals: relevance, not legal authority.",
        "rerank_score": "The reranker's score, when one ran.",
        "features": "The search engine's ranking features for each search the unit came from, such as "
        "`bm25(words)` or `closeness(field, emb_…)`: the inputs to its rank there.",
        "band": "The band of positions it was ordered within.",
        "signals": "Each documented signal applied, with its value and effect.",
        "diversification": "Anything diversification did, such as collapsing a duplicate into it.",
    },
    "RankingSignal": {
        "name": "`legal_status`, `replaced`, `route_match`, `instrument_match`, `instrument_authority` or "
        "`court_authority`.",
        "value": "The value that triggered it.",
        "effect": "What it did, such as `x0.8` or `first`.",
    },
    "EvidenceContext": {
        "heading_path": "The headings above the part, outermost first.",
        "definitions": "Definitions in the same text for terms the part uses.",
        "references": "Provisions of the same text the part refers to by number.",
        "qualifications": "Checked links qualifying the record: what amended, repealed or superseded it.",
        "also_in": "Other texts of the same edition with this part, collapsed into this one.",
        "expanded_from": "When the part joined because a ranked part refers to it by number (with a "
        "reranker only): that part's anchor.",
    },
    "EvidenceDefinition": {
        "term": "The term defined.",
        "anchor_id": "The definition's part.",
        "locator": "The definition's locator.",
        "text": "The definition's words, exactly.",
        "start_byte": "Where it starts: a byte offset into the text in UTF-8.",
        "end_byte": "Where it ends: a byte offset into the text in UTF-8.",
        "span_hash": "SHA-256 of its bytes.",
    },
    "EvidenceReference": {
        "pinpoint": "The provision referred to, such as `s. 23`.",
        "anchor_id": "Its part.",
        "locator": "Its locator.",
    },
    "EvidenceQualification": {
        "kind": "`amended_by`, `repealed_by` or `superseded_by`.",
        "source_id": "The other record.",
        "title": "Its title.",
        "citation": "How it is cited.",
        "effective_date": "When the link took effect, when known.",
        "scope": "What it applies to: `work`, the whole record.",
    },
    "EvidenceBytes": {
        "unique": "Bytes of words, each span counted once.",
        "transmitted": "Bytes of words as sent.",
        "budget": "The `max_evidence_bytes` asked for.",
    },
    "NextStep": {
        "kind": "`ambiguous_citation`, `metadata_only` or `narrow_query`.",
        "detail": "What to do, in plain language.",
        "candidates": "The IDs it refers to.",
    },
    "CatalogueMatch": {
        "source_id": "The record.",
        "title": "Its title.",
        "citation": "How it is cited.",
        "jurisdiction_id": "Its jurisdiction.",
        "document_type": "Its document type.",
        "official_url": "Its official source.",
    },
    "CheckRequest": {"items": "The parts to check, 1 to 100."},
    "CheckItem": {
        "anchor_id": "The part's anchor, from an evidence unit or a search.",
        "version_id": "Instead of `anchor_id`: the text version, with `locator`.",
        "locator": "With `version_id`: the part's locator.",
        "span_hash": "The `span_hash` you hold; a different one answers `span_mismatch`.",
        "as_of": "The date to assess currentness at; today when absent.",
    },
    "CheckPage": {
        "schema_version": "`oa-check-1`.",
        "dataset": "The change log's epoch and latest publication revision.",
        "results": "One per item, in order.",
    },
    "CheckResult": {
        "index": "The item's position in the request, from 0.",
        "result": "`unchanged`; `newer_version` (a later text of the same edition exists); `span_mismatch` "
        "(the hash sent is not this part's); or `unavailable` (not public now, or never).",
        "anchor_id": "The part.",
        "version_id": "The text version checked.",
        "locator": "The part's locator.",
        "newer_version_id": "The newer text, for `newer_version`.",
        "newer_anchor_id": "The same locator's part in the newer text, when it has one.",
        "same_words": "Whether the part's words are identical in the newer text.",
        "currentness": "Whether the part (or, for `newer_version`, the newer text's part) was in force.",
    },
    "SourceView": {
        "id": "The record's ID, such as `ng-foi-act-2011`. It doesn't change.",
        "jurisdiction_id": "Its jurisdiction, such as `ng` or `rw`.",
        "authority_id": "The authority that issued it, such as a legislature or ministry.",
        "title": "The title as published.",
        "document_type": "`constitution`, `act`, `regulation`, `judgment`, `gazette`, `guidance` or `other`.",
        "canonical_url": "The official source (see `url_kind`).",
        "url_kind": "What `canonical_url` is: `document` (the file itself), `landing_page` (the official "
        "page for it) or `discovery` (an official listing where it can be found).",
        "citation": "How it is cited.",
        "language": "Its language, such as `en`, `fr` or `rw`.",
        "legal_status": "Whether it is in force, as far as the catalogue has checked: `current`, "
        "`historical`, `repealed`, `superseded` or `unknown`.",
        "status_evidence": "An official link supporting the legal status, when there is one.",
        "catalogue_note": "A public caveat about this record, when there is one.",
        "summary": "One sentence on what it is.",
        "year": "The year it was made, when known.",
        "access_mode": "`open_full_text` when a text is cleared for reuse here; `metadata_link_only` when "
        "only the record and the official link are.",
        "rights_status": "The rights decision for its text: `OPEN`, `OPEN_WITH_CONDITIONS`, `LINK_ONLY`, "
        "`RESTRICTED` or `RIGHTS_UNDER_REVIEW`.",
        "has_full_text": "Whether Get a record's texts returns a text for it.",
    },
    "SourcePage": {"total": "Records matching, across all pages.", "items": "This page's records."},
    "StructurePage": {
        "version_id": "The text version.",
        "content_hash": "The `content_hash` of the text the offsets refer to.",
        "total": "How many parts the text has.",
        "items": "This page's parts, in document order.",
        "next_cursor": "Pass as `cursor` for the next page; absent on the last page.",
    },
    "AnchorView": {
        "anchor_id": "The part's ID.",
        "source_id": "The record.",
        "version_id": "The text version it is in.",
        "kind": "What it is, such as `section` or `article`.",
        "locator": "Its stable key within the text, such as `sec_23__subsec_1`.",
        "label": "Its printed number, such as `23.` or `(1)`, when it has one.",
        "heading": "Its printed heading, when it has one.",
        "start_byte": "Where it starts: a byte offset into the text in UTF-8.",
        "end_byte": "Where it ends: a byte offset into the text in UTF-8.",
        "offset_unit": "The unit of the offsets: `utf-8-bytes`. Convert explicitly for other units.",
        "span_hash": "SHA-256 of the text's bytes from `start_byte` to `end_byte`.",
        "content_hash": "The `content_hash` of the whole text.",
        "serialization_profile": "How the text was built from the reviewed pages, when recorded.",
        "page": "The page of the official file it starts on, when known.",
        "page_end": "The last page of the official file it reaches, when known.",
        "citation": "A citation for it: the record's citation and its number or heading.",
        "official_url": "The official file the text was read from.",
        "licence": "The terms the text is published under, from the rights record.",
        "attribution_required": "Whether reuse must credit the source, from the rights record.",
        "attribution_text": "The credit to give, when one is required.",
        "text": "Its words, exactly: the text's bytes from `start_byte` to `end_byte`.",
    },
    "SourceRelationships": {
        "source_id": "The record asked about.",
        "legal_status": "Whether it is in force, as far as the catalogue has checked (as on the record).",
        "current": "When it has been repealed or replaced: the record in force in its place now, at the "
        "end of its line of repeals (up to three steps). Absent when nothing in the catalogue repealed it.",
        "total": "Links matching, across all pages.",
        "items": "This page's links, from the record's side: it `repeals`, `amends` or `supersedes` an "
        "earlier record, is `repealed_by`, `amended_by` or `superseded_by` a later one, is `made_under` a "
        "parent law, or `enables` an instrument made under it. Ordered by kind in that order, then by ID.",
        "next_cursor": "Pass it as `cursor` for the next page; absent on the last.",
    },
    "RelatedSource": {
        "kind": "How the other record relates to this one, from this one's side: `repeals`, "
        "`repealed_by`, `amends`, `amended_by`, `supersedes`, `superseded_by`, `made_under`, `enables` "
        "or `cross_reference`.",
        "source_id": "The other record's ID.",
        "title": "The other record's title.",
        "citation": "How the other record is cited.",
        "legal_status": "The other record's legal status.",
        "year": "The year the other record was made, when known.",
        "effective_date": "When the repeal or amendment took effect, when known.",
        "evidence_url": "The official text that makes the link (the repealing or amending law).",
        "verified": "Whether a reviewer checked the link against that text.",
    },
    "CurrentSource": {
        "source_id": "The record in force now.",
        "title": "Its title.",
        "citation": "How it is cited.",
    },
    "SearchPage": {
        "total": "Records matching, across all pages.",
        "items": "This page's records.",
        "scope": "What a search covers: catalogue metadata and cleared full text only.",
    },
    "VersionPage": {
        "items": "The text versions cleared for reuse, each with its full text.",
        "rights": "The record's rights record.",
        "structure": "The parts, chapters and sections of these texts.",
        "notice": "Why no text is shown, when none is.",
        "selection": "When `as_of` or `language` was given: which text they select, or the alternatives "
        "and why.",
    },
    "VersionSelection": {
        "status": "`selected` (one text), `ambiguous` (several: see `alternatives`) or `none`.",
        "as_of": "The date asked about.",
        "language": "The language asked about.",
        "version_id": "The selected text, when there is one.",
        "alternatives": "The texts that could each be the one, when several could.",
        "reason": "Why there is not one text: `several_languages`, `several_texts`, `effective_dates_unknown` "
        "(a text has no effective date, so whether it is in force can't be said), `none_in_language` or "
        "`none_in_force`.",
    },
    "VersionViewRecord": {
        "id": "The version's ID.",
        "source_id": "The record it belongs to.",
        "canonical_url": "The official file the text was read from.",
        "retrieved_at": "When the file was fetched from the official source.",
        "raw_hash": "SHA-256 of the file as fetched.",
        "content_hash": "SHA-256 of `canonical_text` in UTF-8. Check your copy against it.",
        "canonical_text": "The full text.",
        "parser": "The software that read the file into text.",
        "language": "The text's language.",
        "effective_from": "The date this text applies from, when known.",
        "effective_until": "The date this text applies until, when known.",
        "verification_status": "Whether the text was checked against the official file: `pending`, "
        "`verified` or `rejected`.",
        "content_verifier": "Who checked it.",
        "content_verified_at": "When it was checked.",
        "serialization_profile": "How the text was built from the reviewed pages, such as "
        "`oa-text-1/workbench-units-1`, when recorded.",
        "prior_version_id": "The earlier version of the same record this one corrects or replaces, if any.",
        "expression_id": "The language edition the text belongs to: `{source_id}:{language}`.",
    },
    "ExpressionPage": {"source_id": "The record.", "items": "Its language editions with a public text."},
    "ExpressionView": {
        "expression_id": "The edition's ID: `{source_id}:{language}`.",
        "source_id": "The record.",
        "language": "The edition's language.",
        "authority": "Whether the edition is authentic or authoritative: `unknown` until the catalogue records it.",
        "version_ids": "The edition's public texts.",
        "preferred_version_id": "The text to read when no date is given, when one can be named; else absent.",
        "preferred_reason": "Why: `only_text`, `in_force_today` (the one text in force today, every text "
        "dated), or, when none is preferred, `several_texts` or `dates_unknown`.",
    },
    "NodeViewRecord": {
        "id": "The part's ID.",
        "source_id": "The record.",
        "version_id": "The text version it belongs to.",
        "parent_id": "The part it sits in, if any.",
        "kind": "What it is: `document`, `preamble`, `contents`, `contents_entry`, `notice`, `title`, `part`, "
        "`chapter`, `division`, `article`, `section`, `subsection`, `definition`, `paragraph`, "
        "`subparagraph`, `schedule`, `annex`, `table`, or in a judgment `opinion` or `order`.",
        "locator": "A stable key for it within the text, such as `sec_23__subsec_1`. The same provision keeps "
        "its locator in later versions of the text.",
        "order": "Its position among the parts of the same parent.",
        "start_byte": "Where it starts: a byte offset into `canonical_text` in UTF-8.",
        "end_byte": "Where it ends: a byte offset into `canonical_text` in UTF-8.",
        "page": "The page of the official file it starts on, when known.",
        "source_hash": "The `content_hash` of the text the offsets refer to.",
        "label": "Its printed number, such as `23.` or `(1)`, when it has one.",
        "heading": "Its printed heading, such as a section's margin note, when it has one.",
        "page_end": "The last page of the official file it reaches, when known.",
        "span_hash": "SHA-256 of its own bytes from `start_byte` to `end_byte`, to check an exact quotation.",
        "target_node_id": "For a contents entry, the part of the text it lists, when found.",
    },
    "RightsViewRecord": {
        "id": "The rights record's ID.",
        "source_id": "The record it is for.",
        "status": "The decision: `OPEN`, `OPEN_WITH_CONDITIONS`, `LINK_ONLY`, `RESTRICTED` or "
        "`RIGHTS_UNDER_REVIEW`.",
        "rights_holder": "Who holds the rights, when known.",
        "licence": "The licence, when there is one.",
        "licence_url": "The licence's text.",
        "redistribution_allowed": "Whether the text may be redistributed: `yes`, `no`, `conditional` or `unknown`.",
        "commercial_reuse_allowed": "Whether it may be reused commercially: `yes`, `no`, `conditional` or `unknown`.",
        "derivatives_allowed": "Whether it may be adapted: `yes`, `no`, `conditional` or `unknown`.",
        "attribution_required": "Whether reuse must credit the source.",
        "attribution_text": "The credit to give, when one is required.",
        "database_rights_notes": "Notes on database rights, when relevant.",
        "privacy_status": "Whether personal data was checked: `pending`, `cleared`, `redacted` or `blocked`.",
        "reviewer": "Who reviewed the rights.",
        "reviewed_at": "When.",
        "expires_at": "When the decision must be reviewed again, if it lapses.",
        "evidence": "Links to the evidence for the decision.",
        "legal_note": "A note on the decision.",
        "conditions": "Conditions of reuse.",
        "conditions_satisfied": "Whether the conditions are met here.",
        "approved_content_hashes": "The `content_hash` of each text the decision covers.",
        "metadata_public": "Whether the catalogue record may be shown.",
        "metadata_basis": "Why the catalogue record may be shown.",
    },
    "DecisionView": {
        "metadata": "Whether the catalogue record may be shown.",
        "full_text": "Whether a text may be served.",
        "reasons": "Why not, in sentences, when either is false.",
    },
    "RightsView": {
        "record": "The rights record.",
        "decision": "What it allows here.",
        "revision": "The rights record's revision in its history, where a history is kept.",
    },
    "RightsHistory": {
        "source_id": "The record.",
        "revisions": "Every revision of its rights record, newest first.",
    },
    "RightsRevision": {
        "revision": "The revision's number, from 1.",
        "recorded_at": "When the revision was recorded.",
        "publication_revision": "The catalogue publication it was recorded with (0: the first baseline).",
        "record": "The rights record as it was.",
    },
    "CitationView": {
        "status": "`resolved` (one record), `ambiguous` (several) or `unresolved` (none).",
        "matches": "The records the citation refers to.",
        "provision": "When the citation names a part, such as `s. 23(1)(a)`: what it names in the records' "
        "public texts.",
    },
    "ProvisionLookup": {
        "pinpoint": "The part the citation names, as read, such as `s. 23(1)(a)`.",
        "status": "`resolved` (one part), `ambiguous` (several: see `candidates`) or `unresolved` (none).",
        "reason": "Why not one part: `several_works`, `several_texts` (the part is in several texts, such as "
        "two languages: give `language` or `as_of`), `repeated_number` (the text prints the number twice), "
        "`not_found`, `no_public_text` or `no_text_in_scope` (no public text matches `as_of` or `language`).",
        "candidates": "The part, or the parts that could each be the one.",
        "nearest": "When the part is not found: the nearest part enclosing it that is, such as the section "
        "of a paragraph the text doesn't divide.",
    },
    "ProvisionMatch": {
        "anchor_id": "The part's ID: `/anchors/{anchor_id}` gives its words.",
        "source_id": "The record.",
        "version_id": "The text version it is in.",
        "language": "The text's language.",
        "kind": "What it is, such as `section`, `paragraph` or `definition`.",
        "locator": "Its stable key within the text, such as `sec_23__subsec_1__para_a`.",
        "label": "Its printed number, such as `(a)`, when it has one.",
        "heading": "Its printed heading, when it has one.",
        "pinpoint": "How it is cited within the text, such as `s. 23(1)(a)`.",
        "citation": "The record's citation and the pinpoint.",
        "start_byte": "Where it starts: a byte offset into the text in UTF-8.",
        "end_byte": "Where it ends: a byte offset into the text in UTF-8.",
        "offset_unit": "The unit of the offsets: `utf-8-bytes`.",
        "span_hash": "SHA-256 of the text's bytes from `start_byte` to `end_byte`.",
        "content_hash": "The `content_hash` of the whole text.",
        "effective_from": "The date the text applies from, when known.",
        "effective_until": "The date the text applies until, when known.",
    },
    "TextSearchPage": {
        "status": "`results`; `no_result` (published texts were searched and none matched); "
        "`unsupported_scope` (no published text in this scope); or `degraded` (the search could not run: "
        "see `notices`).",
        "query": "The words searched for.",
        "total": "Passages matching, across all pages (see `total_exact`).",
        "total_exact": "Whether `total` is exact; counting stops at 10,000.",
        "items": "This page's passages, best first.",
        "next_cursor": "Pass as `cursor` for the next page; absent on the last. The first 1,000 passages "
        "are served.",
        "citation": "When the words read as a citation of a public record: what it resolves to.",
        "coverage": "What was searched.",
        "analysis": "How each language's texts are matched: `english` (stemming and common words) or "
        "`simple` (word for word).",
        "analyzer": "The version of the index's analysis.",
        "ranking": "The ranking used: `oa-rank-1`, as the search guide describes.",
        "index_built_at": "When the index was built.",
        "notices": "Anything that limits these results, in sentences.",
    },
    "TextHit": {
        "anchor_id": "The part the passage is in: `/anchors/{anchor_id}` gives it whole.",
        "source_id": "The record.",
        "version_id": "The text version.",
        "title": "The record's title.",
        "citation": "The record's citation and the part's pinpoint.",
        "pinpoint": "How the part is cited within the text, such as `s. 23`, when it has a number.",
        "kind": "What the part is, such as `section` or `article`.",
        "locator": "The part's stable key within the text.",
        "heading": "The part's printed heading, when it has one.",
        "language": "The text's language.",
        "jurisdiction_id": "The record's jurisdiction.",
        "document_type": "The record's document type.",
        "legal_status": "The record's legal status.",
        "year": "The year the law was made, when known.",
        "effective_from": "The date the text applies from, when known.",
        "effective_until": "The date the text applies until, when known.",
        "start_byte": "Where the searched passage starts: a byte offset into the text in UTF-8.",
        "end_byte": "Where the searched passage ends.",
        "snippet": "Words from the passage, exactly as in the text: its bytes from `snippet_start_byte` to "
        "`snippet_end_byte`.",
        "snippet_start_byte": "Where the snippet starts in the text.",
        "snippet_end_byte": "Where the snippet ends in the text.",
        "highlights": "The matching words, as [start, end] byte offsets into the text.",
        "offset_unit": "The unit of the offsets: `utf-8-bytes`.",
        "content_hash": "The `content_hash` of the whole text.",
        "matched_by": "`citation` (the words cite this part) or `text` (its words match).",
        "score": "Its relevance under `ranking`, from 0 to 1; absent for a citation match.",
        "official_url": "The official file the text was read from.",
        "licence": "The terms the text is published under.",
        "attribution_required": "Whether quoting it must credit the source.",
        "attribution_text": "The credit to give, when one is required.",
    },
    "SearchCoverage": {
        "records_in_scope": "Public records in the search's scope.",
        "records_with_text": "Of those, records with a published text, which is all that is searched.",
        "texts_searched": "Published text versions searched.",
        "notice": "What the search covered, in a sentence.",
    },
    "CoverageView": {
        "catalogued_sources": "Records in the catalogue.",
        "open_full_text_sources": "Records with a text cleared for reuse.",
        "metadata_only_sources": "Records with only the record and the official link.",
        "published_versions": "Text versions published.",
        "jurisdictions": "Records per jurisdiction, as {ID: count}.",
        "notice": "What the counts cover.",
    },
    "JurisdictionView": {
        "id": "The jurisdiction's ID, such as `ng`.",
        "name": "Its name.",
        "country_code": "Its ISO country code, such as `NG`.",
        "parent_id": "The country a state or region belongs to.",
        "legal_systems": "Its legal systems.",
        "languages": "Its official languages.",
    },
}


def _responses(found: dict, answer, errors: dict) -> dict:
    responses = {"200": found.get("200", {"description": "OK"})}
    responses["200"]["description"] = "OK"
    if isinstance(answer, str):
        responses["200"]["content"] = {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{answer}"}}
        }
    elif isinstance(answer, tuple):
        responses["200"]["content"] = {answer[0]: {"schema": answer[1]}}
    elif isinstance(answer, dict):
        responses["200"]["content"] = {"application/json": {"schema": answer}}
    for status, text in sorted(errors.items()):
        responses[status] = {
            "description": text,
            "content": {"application/problem+json": {"schema": PROBLEM}},
        }
    return responses


def describe(generated: dict) -> dict:
    """The generated document, with every route, parameter and field described."""
    spec = deepcopy(generated)
    spec["tags"] = TAGS
    spec["servers"] = [{"url": "/api/v1", "description": "https://api.open.alvary.ai/api/v1"}]
    spec["info"]["contact"] = {
        "name": "Open Alvary",
        "email": "hello@ruhu.ai",
        "url": "https://open.alvary.ai/docs",
    }
    spec["externalDocs"] = {"description": "Documentation", "url": "https://open.alvary.ai/docs"}
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    schemas.update(deepcopy(SCHEMAS))
    components["securitySchemes"] = {
        "bearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "An API key (`oa_live_…`) from the console, or an access token from a connected app.",
        }
    }
    spec["security"] = [{"bearer": []}]
    for path, methods in spec["paths"].items():
        words = OPERATIONS[path]
        [operation] = methods.values()
        if "requestBody" in operation:
            operation["requestBody"]["description"] = words["body"]
        operation["operationId"] = words["operationId"]
        operation["summary"] = words["summary"]
        operation["description"] = words["description"]
        operation["tags"] = [words["tag"]]
        if words.get("public"):
            operation["security"] = []
        described = words.get("parameters", {}) | words.get("path", {})
        parameters = [p for p in operation.get("parameters", [])]
        for parameter in parameters:
            parameter["description"] = described[parameter["name"]]
        for name, text in words.get("path", {}).items():
            if not any(p["name"] == name for p in parameters):
                parameters.append(
                    {
                        "name": name,
                        "in": "path",
                        "required": True,
                        "description": text,
                        "schema": {"type": "string"},
                    }
                )
        parameters += deepcopy(words.get("extra", []))
        if parameters:
            operation["parameters"] = parameters
        errors = dict(words["errors"]) | ({} if words.get("public") else GATE)
        operation["responses"] = _responses(operation.get("responses", {}), words.get("answer"), errors)
    for name, fields in FIELDS.items():
        for field, text in fields.items():
            schemas[name]["properties"][field]["description"] = text
    # Schemas nothing refers to any more (the coverage counts are an object, not a list).
    while True:
        text = json.dumps({key: value for key, value in spec.items() if key != "components"}) + json.dumps(
            schemas
        )
        unused = [name for name in schemas if f'"#/components/schemas/{name}"' not in text]
        if not unused:
            return spec
        for name in unused:
            del schemas[name]
