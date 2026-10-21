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
    "403": "The organisation has no access, or its access does not include this (`entitlement_required`, "
    "`entitlement_lapsed`, `insufficient_scope`, `account_inactive`, `organisation_suspended`).",
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
        "Jurisdiction IDs, such as `ng`; a country includes its states and local law, and a body such as "
        "`aa-ecowas` includes changes to its membership and its members' suspensions. Up to 50.",
    ),
    ("work", "Record IDs, such as `ng-foi-act-2011`. Up to 50."),
    (
        "type",
        "Kinds of change: `catalogue_added`, `metadata_corrected`, `text_version_published`, "
        "`legal_effect_recorded`, `treatment_recorded`, `withdrawal`, `coverage_changed`, "
        "`membership_recorded`, `suspension_recorded`.",
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
        "description": "Records with every one of the words in their title, citation or summary, or in one "
        "part of their cleared full text, a page at a time in record ID order. Whole words only, in any "
        "order: `invest` doesn't find the Investments and Securities Act. Without `jurisdiction`, a search "
        "covers your plan's countries.",
        "parameters": {
            "q": "Words to find, 1 to 200 characters. Case, punctuation and spacing don't matter.",
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
        "description": "The record's language editions with a public text: for each, its texts, the one to "
        "read when no date is given (`preferred_version_id`) with the reason, and what the edition is worth "
        "as law as the record states it (`authority`, with `authority_evidence`; `unknown` when nothing is "
        "recorded); and the instrument's own rule for which version prevails where its language versions "
        "differ (`language_rule`). A preferred text is a reading default, never a claim that the edition is "
        "authoritative. An edition's ID is `{source_id}:{language}`; pass it as `expression_id` to Get a "
        "record's texts.",
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
        "time: the laws it repeals, amends or commences and the law it is made under; the later laws that "
        "repealed, amended or commenced it; the treaty or directive it transposes, domesticates, implements "
        "or ratifies, and the laws that do so for it; the records it cites and that cite it; the "
        "instruments made under it; and, when it has been repealed or replaced, the record in force in its "
        "place now. For case law, each judgment paragraph that cites a provision or another judgment, with "
        "the part it cites, and how a judgment follows, applies, distinguishes, doubts or overrules another, "
        "or was so treated, once a legally qualified reviewer has recorded it; no treatment listed means "
        "none is recorded, never that the judgment is good law. Links one step away only (depth 1), between "
        "public records that a reviewer has checked against the official text.",
        "parameters": {
            "source_id": "The record's ID.",
            "kind": "Only links of this kind, such as `repealed_by`, `transposed_by`, `enables`, `cited_by` "
            "or `overruled_by`.",
            "locator": "Only the judgment citations and treatments of this part of the record, or of a part "
            "within it: a provision's locator, such as `sec_2__subsec_1`, for the judgment paragraphs that "
            "cite it (with `kind=cited_by`), or a judgment paragraph's, such as `para_12`, for what it cites. "
            "Links between whole records are left out.",
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
            "scope": "`applicable`, with one `jurisdiction`: also the law that binds there on `as_of` (today without it): the "
            "instruments of the African Union and regional bodies it belonged to on that date, from reviewed "
            "memberships only, and the treaties it had ratified or acceded to. Records that do not bind there "
            "are left out and counted; those whose relation is unknown are kept. Each passage says how it "
            "relates to the jurisdiction (`applicability`) and the answer states the widening. For a place "
            "within a country, such as `ng-la`, the country's own law is added too, and its memberships and "
            "treaties are the country's. Without it, nothing is added.",
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
        "covers your plan's countries. With `scope=applicable`, it also covers the regional and treaty law "
        "that binds in the one jurisdiction asked about, and says how each unit relates to it.",
        "parameters": {
            "q": "The question or words, 1 to 2,000 characters. It may be a citation (`NDPA s. 24(1)(a)`, "
            "`section 24 of the Nigeria Data Protection Act`, `Part V of the NDPA`), a page of one law "
            "(`page 18 of the NDPA`), or an address: an Open Alvary page or API address of a record or part, "
            "or a text's official address. A pinpoint alone (`s. 24`) is read within `source_id`.",
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
            "source_id": "Only this record's texts. With `scope=applicable`, this record is answered whatever "
            "its relation to the jurisdiction.",
            "version_id": "Only this text version.",
            "scope": "`applicable`, with one `jurisdiction`: also the law that binds there on `as_of` (today without it): the "
            "instruments of the African Union and regional bodies it belonged to on that date, from reviewed "
            "memberships only, and the treaties it had ratified or acceded to. Records that do not bind there "
            "are left out and counted; those whose relation is unknown are kept. Each unit says how it "
            "relates to the jurisdiction (`applicability`) and the answer states the widening. For a place "
            "within a country, such as `ng-la`, the country's own law is added too, and its memberships and "
            "treaties are the country's. Without it, nothing is added.",
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
        "whether it is still published with the same words (`span_mismatch` when its words don't hash to the "
        "`span_hash` sent), whether a newer text of the same edition "
        "exists (with the same part's anchor there and whether its words changed), and whether it was in "
        "force on a date. Given a `jurisdiction`, also whether the record binds there on that date "
        "(`applicability`): a regional instrument through the country's reviewed membership and the "
        "instrument's legal effect, a treaty through the country's own ratification or accession. A part that "
        "is not public now and one that never was get the same answer, `unavailable`.",
        "body": "The parts to check: `items`, each an `anchor_id`, or a `version_id` with a `locator`, "
        "with the `span_hash` you hold, an optional `as_of` date and an optional `jurisdiction`.",
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
        "description": "The jurisdictions the catalogue holds: countries and their states or regions, the "
        "African Union and the regional bodies, with their legal systems and languages, and for each country "
        "`member_of`, the bodies it belongs to on `as_of` (today by default). Only reviewed memberships are "
        "listed; `status` is `unknown` when none is reviewed.",
        "parameters": {"as_of": "A date, such as `2026-01-01`: the memberships held on it. Default today."},
        "errors": BAD,
    },
    "/jurisdictions/{jurisdiction_id}": {
        "operationId": "get_jurisdiction",
        "tag": "Catalogue",
        "summary": "Get a jurisdiction and its memberships",
        "description": "One jurisdiction on a date. For a country, `member_of`: the African Union and the "
        "regional bodies it belonged to on `as_of`, in `uncertain` those it may or may not have belonged to "
        "(the date falls inside an uncertain first or last day), and in `ended` the memberships that had "
        "ended by then, each with its first and last day (as precisely as the evidence gives them, with "
        "their bounds), its basis, evidence and review, and its suspensions from participation as context. "
        "For the African Union or a regional body, `members`: its member states on that date, and those that "
        "had left. Only reviewed memberships are listed; `status` is `unknown`, never an empty list, when "
        "none is reviewed. A suspension never ends a membership. Membership alone never means a body's "
        "treaties apply to a member.",
        "parameters": {
            "jurisdiction_id": "The jurisdiction's ID, such as `sn` (Senegal) or `aa-ecowas` (ECOWAS).",
            "as_of": "A date, such as `2026-01-01`: the memberships held on it. Default today.",
        },
        "errors": BAD | {"404": "There is no such jurisdiction (`unavailable`)."},
    },
    "/courts": {
        "operationId": "list_court_registers",
        "tag": "Courts",
        "summary": "List court registers",
        "description": "The jurisdictions with a court register, each with how many courts, relations and "
        "entries it records and how many entries have been reviewed. A jurisdiction not listed has no "
        "register: every court relation there is `unknown`.",
        "errors": {},
    },
    "/courts/{jurisdiction}": {
        "operationId": "get_court_register",
        "tag": "Courts",
        "summary": "Get a court register",
        "description": "A jurisdiction's courts and tribunals with their levels, appeal routes and the dates "
        "their roles apply; how each court's decisions bear on the others (`binding`, `prevails_on_matter`, "
        "`binding_in_referred_case`, `persuasive` or `not_applicable`), each in the words of its source; and "
        "whether precedent binds there at all. A regional or continental body's register also gives the "
        "states its courts reach, the routes from their courts to its own and declarations admitting cases "
        "from individuals; its bearing on a member state's courts is limited to the subject matter its "
        "sources give. Every entry has its sources and its own review: `structural` for courts, matters, "
        "members, routes and declarations, `doctrinal` (by a legally qualified reviewer) for relations and "
        "the precedent rule. Until a relation has its own doctrinal review its `relation` is `unknown`, and "
        "`recorded` shows what awaits review. A jurisdiction with no register answers an empty one.",
        "path": {
            "jurisdiction": "The jurisdiction ID: a country such as `ke`, a state such as `ng-la`, or a "
            "regional or continental body such as `aa-ohada`, `aa-eac`, `aa-ecowas` or `aa-au`.",
        },
        "errors": BAD,
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
        "Download a file from `download_base` + `/` + its name. Catalogue releases download from "
        "`/downloads/…` with no key. Releases with full text are listed only when your organisation's "
        "access includes bulk releases, for the countries of its plan, and download from `/api/v1/bulk/…` "
        "with your key.",
        "errors": {},
        "answer": {"type": "array", "items": {"$ref": "#/components/schemas/Release"}},
    },
    "/bulk/{release_id}/{jurisdiction}/{mode}/{filename}": {
        "operationId": "get_bulk_file",
        "tag": "Releases",
        "summary": "Download a bulk release file",
        "description": "One file of a release, exactly as its manifest's SHA-256 says, for organisations "
        "whose access includes bulk releases (granted by agreement, not a listed plan). Releases with "
        "full text download only here, and only for the countries of your plan. Each file is checked "
        "against the current catalogue just before it is sent: a release with a withdrawn or changed "
        "record is no longer served, so download a newer one. Downloads count as bulk use and draw on "
        "your organisation's data a day.",
        "path": {
            "release_id": "The release's ID, such as `2026-10-05`.",
            "jurisdiction": "The release's jurisdiction ID, such as `ng`.",
            "mode": "`corpus` (records with their cleared texts) or `metadata` (records only).",
            "filename": "A file the manifest lists, such as `versions.jsonl`, or `manifest.json`.",
        },
        "errors": {
            "403": "Or the release's country is outside your plan (`jurisdiction_not_included`).",
            "404": "There is no such release or file (`unavailable`).",
            "410": "The release was withdrawn: a record in it changed or stopped being public (`withdrawn`).",
        },
        "answer": ("application/octet-stream", {"type": "string", "format": "binary"}),
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
        "withdrawn, texts published, legal effects recorded, coverage changes, and reviewed memberships and "
        "suspensions recorded. Keep the `resume_token` "
        "and send it next time for only newer changes. Send at most one of `cursor`, `resume_token` and "
        "`start`. The same route answers without a key at https://open.alvary.ai/api/changes.",
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
    {
        "name": "Courts",
        "description": "Court registers: courts and their appeal routes, and how each court's decisions bear "
        "on the others, with their sources.",
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
                "`text_version_published`, `legal_effect_recorded` (a checked link with another public "
                "record that changes what law applies: a repeal, amendment, supersession, commencement, "
                "transposition, domestication or ratification; or a reviewed event on its provisions), "
                "`treatment_recorded` (a reviewed judicial treatment: how a judgment's paragraph follows, "
                "applies, distinguishes, doubts or overrules another judgment), "
                "`withdrawal` (a record or text), `coverage_changed` (a jurisdiction's coverage), "
                "`membership_recorded` (a reviewed membership of the African Union or a regional body "
                "recorded, ended or corrected) or `suspension_recorded` (a reviewed suspension of a member from "
                "participation in a body's activities recorded, lifted or corrected; it remains a member). "
                "More types "
                "may be added: ignore those you do not know; `/change-coverage` lists the ones recorded.",
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
            "legal_effective": NULL_S
            | {
                "description": "When it takes legal effect, if known: {from, until, precision}. A membership's or "
                "suspension's dates are EDTF dates, each stating its own precision (a window `[FIRST..LAST]` "
                "holds one day); `precision` is that of `from`."
            },
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
                "`extraction_correction`, `redaction`, `corrected`, `ended` (a membership ended), `lifted` (a "
                "suspension lifted) or `unknown`."
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
                "`amendment`, `supersession`, `commencement`, `transposition`, `domestication` or "
                "`ratification` of the record in `work`; `by` is the record that had the effect {id, title, "
                "citation}; the date is when it took effect, when known. A reviewer checked it against the "
                "official text. A transposition, domestication or ratification changes the law on both sides, "
                "so the law that gives effect to the instrument has its own change, naming the instrument in "
                "`of` {id, title, citation} instead of `by`. A later correction of it has the same `event_id`; "
                "when the link was withdrawn, the correction says `review_state` `withdrawn` and names the "
                "link it withdraws. For a reviewed event on provisions, `kind` is the event (`commenced`, "
                "`repealed`, `suspended`, `revived`, …), with `scope` (`whole` or `explicit_nodes`), "
                "`provisions` (their locators), `precision` of the date and `review_state` (`withdrawn` when "
                "the event was withdrawn, or is no longer public); `by` is present when another law asserted it.",
            },
            "treatment": {
                "type": ["object", "null"],
                "description": "For a judicial treatment: {id, kind, assertion, of or by, citing_locator, "
                "citing_pinpoint, cited_locator, cited_pinpoint, opinion, review_level, evidence_url}. `kind` is "
                "`follows`, `applies`, `distinguishes`, `doubts` or `overrules`; `assertion` is "
                "`direct_treatment`, a reviewer's reading of the treating paragraph, never an inferred risk. The "
                "treating judgment's change names the judgment treated in `of` {id, title, citation}; an "
                "overruling has a change on the overruled judgment too, naming the overruling one in `by`. "
                "`citing_pinpoint` is the treating paragraph and `cited_pinpoint` the part treated; `opinion` is "
                "the one the paragraph is in (`majority`, `concurrence`, `dissent`, `plurality`, `order` or "
                "`unknown`); `review_level` is `doctrinal`. The passage itself is on the record's history. A "
                "later correction has the same `event_id`; when the treatment was withdrawn, it says "
                "`review_state` `withdrawn`.",
            },
            "membership": {
                "type": ["object", "null"],
                "description": "For a membership: {id, member, body, valid_from, valid_from_bounds, valid_to, "
                "valid_to_bounds, basis, evidence_url, retracted}. `member` and `body` are {id, name}; the dates "
                "are its first and last day (`valid_to` null: not ended), each an EDTF date (a day, a month, a "
                "year or a window holding the day) with its bounds {earliest, latest, precision}, as a reviewer "
                "checked them against the evidence; `basis` {kind, instrument, pinpoint, evidence_url} says why "
                "it starts then. A change with `change_reason` `ended` records its end; a later correction has "
                "the same `event_id`, and `retracted` true when the membership is no longer a reviewed fact.",
            },
            "suspension": {
                "type": ["object", "null"],
                "description": "For a suspension from participation: {id, kind, membership_id, member, body, "
                "valid_from, valid_from_bounds, valid_to, valid_to_bounds, legal_basis, evidence_url, "
                "lifted_evidence_url, retracted}. `legal_basis` lists each article {instrument, pinpoint, role, "
                "evidence_url}: `role` `decision_basis` for the one the decision cites, `treaty_rule` for the "
                "treaty's rule it applies; a change of their words alone is not announced. It never ends the "
                "membership, which no event changes. A change "
                "with `change_reason` `lifted` records its lifting; a later correction has the same `event_id`, "
                "and `retracted` true when it is no longer a reviewed fact.",
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
            "download_base": {
                "type": S,
                "description": "Where its files are; append `/` and a file name. `/downloads/…` needs no "
                "key; `/api/v1/bulk/…` (releases with full text) needs your key.",
            },
            "files": {"type": "object", "description": "Its files, with their SHA-256 checksums."},
        },
        "additionalProperties": True,
    },
}

# Field descriptions of the response models, by schema.
JURISDICTION = {
    "id": "The jurisdiction's ID, such as `ng`, or `aa-ecowas` for a regional body.",
    "name": "Its name.",
    "country_code": "Its ISO country code, such as `NG`; null for the African Union and the regional bodies.",
    "parent_id": "The country a state or region belongs to.",
    "legal_systems": "Its legal systems.",
    "languages": "Its official languages.",
}
# What every court register entry carries (CourtEntry).
COURT_ENTRY = {
    "review_state": "`reviewed` once a reviewer has checked this entry against its sources, and it is "
    "unchanged since; else `unreviewed`.",
    "review_level": "The review the entry needs: `structural` (which courts there are, from when, where "
    "an appeal or reference lies) or `doctrinal` (how decisions bind, prevail or persuade, reviewed by "
    "someone legally qualified for the jurisdiction).",
    "reviewed_by": "Who reviewed it.",
    "reviewer_role": "Their role, such as `owner` or `Kenyan advocate`.",
    "reviewer_qualification": "Their legal qualification for the jurisdiction, which a doctrinal review "
    "states.",
    "reviewed_on": "The day they reviewed it.",
    "evidence": "The official sources the entry rests on.",
    "note": "What the sources say, in brief.",
}
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
        "applicability": "With `scope=applicable`: the widening applied, and what it rests on and left out.",
    },
    "EvidenceQuery": {
        "text": "The question, with spacing normalised.",
        "routes": "The routes taken, in order: `link` (the query is an Open Alvary or official address), "
        "`citation`, `page` (a page of one law's official file), `instrument`, `definition`, `topic`, and the "
        "modifiers `historical` and `comparative`.",
        "filters": "The filters applied, as given or as your plan set them; `scope` when asked for (the "
        "widening it applied is in `applicability`).",
        "as_of": "The date asked about, when given.",
    },
    "EvidenceRevisions": {
        "dataset": "The change log's epoch and latest publication revision: any published change moves it.",
        "router": "The routing rules, such as `oa-route-3`.",
        "ranking": "The ranking profile, such as `oa-rank-3`.",
        "analyzer": "The text analysis, such as `oa-search-5`.",
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
        "expression_authority": "What the edition is worth as law, as its record states it: `authoritative`, "
        "`equally_authentic`, `official_translation`, `unofficial_translation`, `machine_translation`, or "
        "`unknown` when nothing is recorded. Get a record's editions gives the instrument's rule where "
        "versions differ.",
        "expression_authority_evidence": "Where that authority is stated, when recorded.",
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
        "retrieved_at": "When the official file was fetched.",
        "original_sha256": "SHA-256 of the official file as fetched: a copy with this hash is the file the text "
        "was read from.",
        "page_start": "The page of the official file the unit starts on (1 is the file's first page, not a "
        "printed page number). Absent when the text was not read from a file with pages.",
        "page_end": "The page of the official file the unit ends on.",
        "links": "Where to read the unit: on the website, by the API, and the whole text.",
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
        "regional": "For a regional instrument: the organ that adopted it, its legal effect, the member states it binds and "
        "its transposition deadline, as its record states them.",
        "treaty": "For a treaty: who adopted it, its depositary, when it entered into force and its authentic "
        "languages. Its parties are on the record (Get a record) and its anchors.",
        "applicability": "With `scope=applicable`: how the record relates to the jurisdiction asked about, and what that rests on.",
        "ranking": "Why the unit ranks where it does.",
        "context": "What travels with the unit and is never part of its words.",
        "warnings": "Notes on the unit, such as an excerpt or a newer text.",
    },
    "EvidenceLinks": {
        "web": "The record on the website, at the unit when the website shows this text (a record's one text in "
        "force); otherwise the record's page.",
        "api": "This exact part of this exact text (`/anchors/{anchor_id}`); for a document unit, the record.",
        "text": "The whole text the unit is in (`/versions/{version_id}/text`).",
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
        "court_relation": "For a decision, the ranking signal: how its court's decisions bear on the courts "
        "of the jurisdiction asked about generally, from a reviewed relation of its court register. "
        "`binding` (precedent binds) ranks first within a band; `prevails_on_matter` (precedence on a "
        "subject matter, not stare decisis) is given only on a question of that matter, and then ranks with "
        "`binding`: a request states no matter, so it is never given here; `persuasive` ranks next; "
        "`binding_in_referred_case` (the ruling binds only the court that referred the question, in that "
        "case), `not_applicable` and `unknown` (no reviewed relation says, or only on a subject matter: see "
        "`court.bears_on`) rank alike. `not_applicable` for anything but a decision.",
        "court": "For a decision whose court a court register lists: that court, and how its decisions bear "
        "on other courts.",
    },
    "EvidenceCourt": {
        "id": "The court's ID in its register, as `/courts/{jurisdiction}` lists it.",
        "name": "The court's name.",
        "jurisdiction": "The jurisdiction whose court register answers: the one asked about or, for a "
        "regional or continental court when one of its member states is asked about (by a reviewed "
        "membership), that body's (`aa-ohada`, `aa-eac`, …).",
        "review_state": "`reviewed` once a reviewer has checked the court's entry against its sources, else "
        "`unreviewed`.",
        "reviewed_on": "The day they did.",
        "bears_on": "Each relation of the court's decisions to other courts on the date assessed (`as_of`, "
        "or today) that has its own doctrinal review; none until then.",
    },
    "EvidenceBearing": {
        "id": "The relation's ID in the register.",
        "court": "The court bound or persuaded, by its ID in the register; null for the jurisdiction's courts "
        "generally.",
        "members": "True when the relation covers the courts of each member state of a regional body.",
        "matter": "The subject matter the relation is limited to, by its ID in the register, such as "
        "`uniform-acts`. Absent: any matter.",
        "relation": "`binding` (precedent binds), `prevails_on_matter` (the court's decisions on `matter` "
        "take precedence over national courts' decisions: not stare decisis), `binding_in_referred_case` "
        "(the ruling binds the court that referred the question, in that case only), `persuasive` or "
        "`not_applicable`.",
        "valid_from": "The first day the relation applies, when it has a start.",
        "valid_until": "The first day it no longer applies, when it has ended.",
        "evidence": "The official sources the relation rests on.",
        "rests_on": "The words of those sources it rests on, verbatim.",
    },
    "CourtQuote": {
        "quote": "The source's words, verbatim.",
        "provision": "Where they are, such as `art. 33(2)` or `para 39`.",
        "title": "The source, such as `Constitution of Kenya, 2010`.",
        "url": "Its official text.",
    },
    "CourtRegisters": {"registers": "Each jurisdiction with a court register, in order of its ID."},
    "CourtRegisterSummary": {
        "jurisdiction": "The jurisdiction ID, such as `ke` or `aa-ohada`.",
        "review_state": "`reviewed` when every entry has its review, `partly_reviewed` when some have, "
        "`unreviewed` when none has.",
        "courts": "How many courts and tribunals it lists.",
        "relations": "How many relations between courts it records.",
        "entries": "How many entries it holds, each reviewed on its own.",
        "reviewed_entries": "How many of them have a review that covers them as they are.",
    },
    "CourtRegisterView": {
        "jurisdiction": "The jurisdiction ID, such as `ke` or `aa-ohada`.",
        "review_state": "`reviewed` when every entry has its review, `partly_reviewed` when some have, "
        "`unreviewed` when none has: a proposal awaiting review.",
        "precedent": "Whether decisions bind later courts there at all, as its law says; null when not "
        "recorded, and then every `binding` or `persuasive` relation is `unknown`.",
        "courts": "Each court and tribunal.",
        "relations": "How each court's decisions bear on other courts. A relation not listed is `unknown`: "
        "none is inferred from rank or appeal routes.",
        "matters": "The subject matters a relation or route is limited to.",
        "members": "For a regional or continental body: the states its courts reach, and when.",
        "member_routes": "For a regional body: routes from member states' courts to its own.",
        "declarations": "Declarations accepting a court's jurisdiction over cases brought by individuals and "
        "NGOs, such as those under art. 34(6) of the African Court Protocol, with their withdrawals.",
    },
    "CourtPrecedentView": COURT_ENTRY
    | {
        "id": "`precedent`, the entry's ID for review.",
        "binds": "Whether decisions bind later courts there at all (stare decisis).",
    },
    "CourtLevelView": COURT_ENTRY
    | {
        "id": "The court's ID, as evidence units give it in `authority.court.id`.",
        "name": "Its name.",
        "kind": "`court`, `tribunal` or `quasi_judicial` (a body such as a human-rights commission whose "
        "findings are recommendations).",
        "level": "`apex`, `appellate`, `superior`, `subordinate`, `specialised`, `customary`, `regional` or "
        "`continental`.",
        "rank": "1 for the highest; courts of equal standing share a rank. Absent when no source makes it "
        "plain.",
        "jurisdiction": "Set for another jurisdiction's court that this one's law gives a role.",
        "valid_from": "The first day its role applies, when it has a start.",
        "valid_until": "The first day its role no longer applies, when it has ended.",
        "appeals_to": "The courts of this register an appeal lies to.",
    },
    "CourtRelationView": COURT_ENTRY
    | {
        "id": "The relation's ID in the register.",
        "court": "The court whose decisions bear.",
        "bound": "The court they bear on; null for the jurisdiction's courts generally, or with `members` "
        "the courts of each member state.",
        "members": "True when the relation covers the courts of each member state while it is a member.",
        "matter": "The subject matter it is limited to, by its ID in `matters`. Absent: any matter.",
        "relation": "What Open Alvary answers: the recorded relation once it has its own doctrinal review and "
        "the courts it names have theirs (and, for `binding` and `persuasive`, once the precedent rule has its "
        "own), `unknown` until then.",
        "recorded": "The relation the register records, in its source's terms: `binding` (precedent binds), "
        "`prevails_on_matter` (precedence on `matter` over national courts' decisions: not stare "
        "decisis), `binding_in_referred_case` (the ruling binds the court that referred the question, in "
        "that case only), `persuasive` or `not_applicable`.",
        "rests_on": "The words of its sources it rests on, verbatim.",
        "valid_from": "The first day it applies, when it has a start.",
        "valid_until": "The first day it no longer applies, when it has ended.",
    },
    "CourtMatterView": COURT_ENTRY
    | {
        "id": "The matter's ID, as relations and routes name it.",
        "name": "The matter, as its source defines it.",
    },
    "CourtMemberView": COURT_ENTRY
    | {
        "id": "The membership's ID in the register.",
        "state": "The member state, such as `sn`.",
        "court": "Set when the state is party only to this court's own instrument, not the body's treaty.",
        "valid_from": "The day the instrument entered into force for the state, when known.",
        "valid_until": "The first day it no longer bound the state, after a withdrawal.",
        "withdrawal_notified": "The day the state notified its withdrawal.",
    },
    "CourtRouteView": COURT_ENTRY
    | {
        "id": "The route's ID in the register.",
        "court": "The register's court the route leads to.",
        "kind": "`cassation` (in place of the national court of last resort), `appeal`, or `reference` (a "
        "national court refers a question of interpretation).",
        "matter": "The subject matter it is limited to, by its ID in `matters`. Absent: any matter.",
        "valid_from": "The first day it applies, when it has a start.",
        "valid_until": "The first day it no longer applies, when it has ended.",
    },
    "CourtDeclarationView": COURT_ENTRY
    | {
        "id": "The declaration's ID in the register.",
        "state": "The declaring state, such as `gh`.",
        "court": "The court whose jurisdiction it accepts.",
        "valid_from": "The day the declaration was deposited.",
        "valid_until": "The first day it no longer applied, after a withdrawal.",
        "withdrawal_deposited": "The day the withdrawal was deposited.",
    },
    "CourtEvidence": {
        "title": "The source, such as `Constitution of Kenya, 2010`.",
        "url": "Its official text.",
        "pinpoint": "The provision, such as `art. 163(7)`.",
    },
    "RankingExplanation": {
        "position": "The unit's position, from 1.",
        "router": "The routing rules.",
        "routes": "The routes taken.",
        "profile": "The ranking profile.",
        "arms": "Each search the unit came from and its rank there: `link`, `citation`, `page`, `instrument`, "
        "`definition`, "
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
        "jurisdiction": "A country, or a place within one, such as `sn`: the answer then says whether the record "
        "binds there on `as_of` (`applicability`).",
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
        "applicability": "With a `jurisdiction`: whether the record binds there on the date, and what that rests "
        "on.",
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
        "regional": "For a regional instrument: the organ that adopted it, its legal effect, the member states it binds and "
        "its transposition deadline, as its record states them.",
        "treaty": "For a treaty: its status as its record states it, with each party's signature, ratification, "
        "accession and denunciation, its reservations and the acts that domesticate it.",
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
        "regional": "For a regional instrument: the organ that adopted it, its legal effect, the member states it binds and "
        "its transposition deadline, as its record states them.",
        "treaty": "For a treaty: its status and parties, as on the record.",
        "text": "Its words, exactly: the text's bytes from `start_byte` to `end_byte`.",
    },
    "SourceRelationships": {
        "source_id": "The record asked about.",
        "legal_status": "Whether it is in force, as far as the catalogue has checked (as on the record).",
        "current": "When it has been repealed or replaced: the record in force in its place now, at the "
        "end of its line of repeals (up to three steps). Absent when nothing in the catalogue repealed it.",
        "total": "Links matching, across all pages.",
        "items": "This page's links, from the record's side, in this order: a judgment is `overruled_by`, "
        "`doubted_by`, `distinguished_by`, `applied_by` or `followed_by` a later judgment; it is "
        "`repealed_by`, `superseded_by`, `amended_by` or `commenced_by` a later record; it is `made_under` a "
        "parent law, or `transposes`, `domesticates`, `implements` or `ratifies` a treaty or directive; it "
        "`repeals`, `supersedes`, `amends` or `commences` another record; a treaty or directive is "
        "`transposed_by`, `domesticated_by`, `implemented_by` or `ratified_by` a law; a judgment "
        "`overrules`, `doubts`, `distinguishes`, `applies` or `follows` another; a `cross_reference`; it "
        "`cites` or is `cited_by` another record; it `enables` an instrument made under it. Then by ID, and "
        "a judgment's citations in the order of its paragraphs.",
        "next_cursor": "Pass it as `cursor` for the next page; absent on the last.",
    },
    "RelatedSource": {
        "kind": "How the other record relates to this one, from this one's side: `repeals`, "
        "`repealed_by`, `amends`, `amended_by`, `supersedes`, `superseded_by`, `commences`, `commenced_by`, "
        "`made_under`, `enables`, `transposes`, `transposed_by`, `domesticates`, `domesticated_by`, "
        "`implements`, `implemented_by`, `ratifies`, `ratified_by`, `cites`, `cited_by` or "
        "`cross_reference`; for judgments, `follows`, `applies`, `distinguishes`, `doubts` or `overrules` "
        "and `followed_by`, `applied_by`, `distinguished_by`, `doubted_by` or `overruled_by`. A kind and its "
        "`_by` form are the same link seen from each end. `cites` says only that one record refers to the "
        "other, never that it agrees with it or changes it. A treatment is a reviewer's reading of one "
        "paragraph, in the opinion it is in; it is never inferred from a citation, and overruling is not "
        "repeal.",
        "source_id": "The other record's ID.",
        "title": "The other record's title.",
        "citation": "How the other record is cited.",
        "legal_status": "The other record's legal status.",
        "year": "The year the other record was made, when known.",
        "effective_date": "When the link took effect, such as the date of a repeal or commencement, when "
        "known.",
        "evidence_url": "The official text that makes the link (the repealing, amending, commencing or "
        "transposing law, for example; for a judgment's citation or treatment, the citing judgment).",
        "verified": "Whether a reviewer checked the link against that text.",
        "assertion_id": "For a judgment's citation or treatment: its ID. Absent on a link between whole records.",
        "citing_locator": "The citing judgment's paragraph, by locator, such as `para_12` or `op_2__para_4`.",
        "citing_pinpoint": "That paragraph as it is cited, such as `[12]` or `op. 2 [4]`.",
        "cited_locator": "The part of the cited record the paragraph cites, by locator, such as "
        "`sec_2__subsec_1`; absent when it cites the record as a whole, or its pinpoint names no part of a "
        "public text.",
        "cited_pinpoint": "That part as it is cited, such as `s. 2(1)` or `[4]`.",
        "printed": "The citation as the paragraph writes it. Present only while the citing judgment's text is "
        "public.",
        "quote": "The paragraph's words (or, for a long paragraph, the sentences around the citation); for a "
        "treatment, the passage that treats the other judgment. Present only while the citing judgment's text "
        "is public.",
        "opinion": "For a treatment: the opinion its paragraph is in, as the reviewer recorded it: `majority`, "
        "`concurrence`, `dissent`, `plurality`, `order` or `unknown`. A dissent's words are not the court's "
        "holding.",
        "review": "Who checked it against the paragraph, at what level and when.",
    },
    "AssertionReview": {
        "level": "`structural` for a citation (the paragraph cites that record or part), `doctrinal` for a "
        "treatment (how the paragraph treats the other judgment).",
        "reviewed_by": "The reviewer.",
        "reviewer_role": "The reviewer's role, such as `owner` or `Nigerian advocate`.",
        "reviewer_qualification": "For a treatment: the reviewer's legal qualification for the jurisdiction, "
        "which a treatment is never recorded without.",
        "reviewed_at": "When it was reviewed.",
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
        "notices": "Present when there is something to know about this search: published texts were not "
        "searched (the section index is being built, or that search took too long), or the query has no letters "
        "or digits to match.",
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
        "expression_authority": "What that edition is worth as law, as the record states it: `authoritative`, "
        "`equally_authentic`, `official_translation`, `unofficial_translation`, `machine_translation`, or "
        "`unknown` when nothing is recorded. Never implied by which text is preferred or selected.",
        "expression_authority_evidence": "Where that authority is stated, when recorded.",
    },
    "ExpressionPage": {
        "source_id": "The record.",
        "language_rule": "The instrument's own rule for which version prevails where its language versions "
        "differ, as the record states it.",
        "items": "Its language editions with a public text.",
    },
    "ExpressionView": {
        "expression_id": "The edition's ID: `{source_id}:{language}`.",
        "source_id": "The record.",
        "language": "The edition's language.",
        "authority": "What the edition is worth as law, as the record states it: `authoritative` (the only "
        "authoritative text), `equally_authentic` (one of several equally authentic texts), "
        "`official_translation`, `unofficial_translation`, `machine_translation`, or `unknown` when nothing "
        "is recorded.",
        "authority_evidence": "Where that authority is stated, when recorded.",
        "version_ids": "The edition's public texts.",
        "preferred_version_id": "The text to read when no date is given, when one can be named; else absent.",
        "preferred_reason": "Why: `only_text`, `in_force_today` (the one text in force today, every text "
        "dated), or, when none is preferred, `several_texts` or `dates_unknown`.",
    },
    "AuthorityEvidence": {
        "source": "Where it is stated: `document` (this document: its page and words), `external` (another "
        "instrument, at `url`), `reviewer`, `registry` or `manifest`.",
        "page": "The page it is stated on, of the official file (1 is the file's first page) or of the "
        "instrument at `url`.",
        "quote": "The words that state it, as printed.",
        "url": "The address of the instrument that states it, when it is another.",
    },
    "LanguageRuleView": {
        "status": "`stated` (the instrument states a rule), `not_stated` (the reviewed copy states none), "
        "`not_applicable`, or `unknown` (nothing is recorded).",
        "rule": "The rule, as the instrument states it.",
        "prevails": "The language whose version prevails where versions differ, when the rule names one; "
        "absent when it names none, as when every version is equally authentic.",
        "provision": "Where the instrument states it, such as `Article 42`.",
        "evidence": "Where the rule is stated: its page and words, or the instrument's address.",
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
        "analysis": "How each language's texts are matched: `english`, `french`, `portuguese` or `spanish` "
        "(by word stems, common words left out), `arabic` (by word stems, every word kept) or `simple` (word "
        "for word).",
        "analyzer": "The version of the index's analysis.",
        "ranking": "The ranking used: `oa-rank-1`, as the search guide describes.",
        "index_built_at": "When the index was built.",
        "notices": "Anything that limits these results, in sentences.",
        "applicability": "With `scope=applicable`: the widening applied, and what it rests on and left out.",
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
        "applicability": "With `scope=applicable`: how the record relates to the jurisdiction asked about, and what that rests on.",
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
    "JurisdictionView": JURISDICTION
    | {"member_of": "For a country: the bodies it belongs to on `as_of`, reviewed memberships only."},
    "JurisdictionDetail": JURISDICTION
    | {
        "member_of": "For a country: the African Union and regional bodies it belongs to on `as_of`.",
        "level": "`continental`, `regional`, `national`, `subnational` or `local`, when known.",
        "as_of": "The date the memberships are given for.",
        "members": "For the African Union or a regional body: its members on `as_of`.",
    },
    "MembershipList": {
        "status": "`reviewed`: the memberships listed are reviewed facts. `unknown`: none is reviewed yet, "
        "which never means none exists.",
        "items": "The memberships held on the date.",
        "uncertain": "The memberships that may or may not have held on the date: it falls inside the window "
        "of an uncertain first or last day. Unknown, never guessed.",
        "ended": "The reviewed memberships that had certainly ended before the date, with their last day.",
        "notice": "What the list covers, in a sentence.",
    },
    "MembershipView": {
        "id": "The membership's ID: member, body and period, such as `ml:aa-ecowas:1`.",
        "member_id": "The member's jurisdiction ID, such as `ml`.",
        "member_name": "The member's name.",
        "body_id": "The body's jurisdiction ID, such as `aa-ecowas`.",
        "body_name": "The body's name.",
        "valid_from": "The membership's first day, as an EDTF date: a day (`2001-05-26`), a month (`2000-12`), "
        "a year (`1976`) or a window holding the day (`[2016-08-15..2016-09-05]`: one day, unknown which, "
        "never a period), as precisely as the evidence gives it.",
        "valid_from_bounds": "The earliest and latest day `valid_from` can be, and its precision.",
        "valid_to": "Its last day, when it has ended (a withdrawal effective on 29 January ends it on the 28th), "
        "in the same forms; null when it has not.",
        "valid_to_bounds": "The earliest and latest day `valid_to` can be, and its precision; null when it has "
        "not ended.",
        "basis": "Why the membership starts when it does, with the article that makes that its start.",
        "evidence_url": "The official source the dates were checked against: the body's own site or the "
        "treaty's depositary.",
        "note": "What the evidence says, and anything a reader should know about the dates.",
        "reviewed_by": "Who reviewed it.",
        "reviewed_at": "When it was reviewed.",
        "suspensions": "The member's reviewed suspensions from participation in the body, as context. A "
        "suspension never ends a membership: a suspended member is still listed as holding it.",
    },
    "DateBounds": {
        "earliest": "The earliest day the date can be.",
        "latest": "The latest day the date can be (the same as `earliest` for a day).",
        "precision": "`day`, `month`, `year` or `range` (a window `[FIRST..LAST]` holding the day).",
    },
    "ProvisionView": {
        "instrument": "The instrument, such as `Constitutive Act of the African Union`.",
        "pinpoint": "Its article, such as `art. 30`.",
        "role": "Why it is cited: `decision_basis`, the article the decision itself cites as its basis (read "
        "in the decision), or `treaty_rule`, the treaty's rule the suspension applies (read in the treaty), "
        "whether or not the decision names it.",
        "evidence_url": "Where it was read: the decision, for `decision_basis`; the treaty, for `treaty_rule`.",
        "quote": "The words read there, when recorded: the decision's citing the article, or the article's "
        "own.",
    },
    "BasisView": {
        "kind": "`entry_into_force` (the instrument entered into force for the member), `deposit` (the "
        "member deposited its instrument), `admission_decision` (the body decided to admit it), "
        "`provisional_application_on_signature` (the instrument applied from signature) or `signature` "
        "(where the instrument makes signature binding).",
        "instrument": "The instrument whose article sets the start.",
        "pinpoint": "The article, such as `art. 28`.",
        "evidence_url": "Where the article was read.",
        "quote": "The article's words, when recorded.",
    },
    "SuspensionView": {
        "id": "The suspension's ID: its membership and its number, such as `ga:aa-au:1:s1`.",
        "kind": "`suspended_from_participation`: the member may not take part in the body's activities. It "
        "remains a member.",
        "valid_from": "The first day of the suspension, as an EDTF date.",
        "valid_from_bounds": "The earliest and latest day `valid_from` can be, and its precision.",
        "valid_to": "Its last day, once lifted (a suspension lifted on 30 April ends on the 29th); null while it "
        "lasts.",
        "valid_to_bounds": "The earliest and latest day `valid_to` can be, and its precision; null while it "
        "lasts.",
        "in_effect": "Whether it was in effect on `as_of`; null when that date falls inside an uncertain first "
        "or last day.",
        "legal_basis": "The articles it rests on: the one its decision cites (`decision_basis`), always, "
        "and the treaty's rule it applies (`treaty_rule`), where there is one.",
        "evidence_url": "The decision that suspended the member.",
        "lifted_evidence_url": "The decision that lifted it, once lifted.",
        "note": "What the evidence says, and anything a reader should know.",
        "reviewed_by": "Who reviewed it.",
        "reviewed_at": "When it was reviewed.",
    },
    "StatedDateView": {
        "value": "The date (for a month or a year, its first day), when the record gives one.",
        "precision": "How precise it is: `day`, `month` or `year`. A date is compared with another only where "
        "its precision allows.",
        "printed": "The date as printed, when the record keeps it.",
    },
    "RegionalView": {
        "organ": "The organ that adopted it, such as the Council of Ministers.",
        "legal_effect": "`directly_applicable` (applies in the members as it stands), `requires_transposition` "
        "(binds the members as to its result, through national law), `binding_on_addressees` (binds the member "
        "states it is addressed to) or `non_binding`.",
        "member_states": "The member states it binds; empty means every member.",
        "transposition_deadline": "The date by which members must transpose it, when one is set.",
    },
    "LinkedAct": {
        "source_id": "The other record.",
        "jurisdiction_id": "Its jurisdiction.",
        "title": "Its title.",
        "citation": "How it is cited.",
        "effective_date": "When the link took effect, when known.",
    },
    "TreatyView": {
        "adopted_by": "The body that adopted it, when recorded.",
        "depositary": "Its depositary, when recorded.",
        "entered_into_force": "When it entered into force, when the record gives one date for the whole treaty.",
        "authentic_languages": "The languages in which its text is authentic.",
        "parties": "On a record and its anchors: each party as its record lists it. Empty means none is "
        "recorded, never that it has none.",
        "parties_complete_as_of": "On a record and its anchors: the date the party list is complete as of, "
        "by the depositary's status list, when recorded. Until a list is complete on a date, a state missing "
        "from it is `unknown` on that date, never not a party.",
        "parties_complete_evidence": "Where the list is shown complete: the depositary's status list.",
    },
    "TreatyPartyView": {
        "party": "The party, as listed.",
        "place": "Its jurisdiction ID, such as `ke`.",
        "signed": "When it signed. Signature alone never binds.",
        "ratified": "When it ratified.",
        "acceded": "When it acceded.",
        "denounced": "When its denunciation or withdrawal took effect.",
        "reservations": "Its reservations, as recorded.",
        "domesticated_by": "Acts of that state that a checked link records as domesticating the treaty.",
    },
    "MembershipBasis": {
        "id": "The reviewed membership, such as `sn:aa-ecowas:1`; Get a jurisdiction gives its dates and evidence.",
        "body_id": "The body.",
        "held": "Whether the jurisdiction was a member on the date: `held`, `not_held`, or `unknown` when the "
        "date falls in an uncertain window of the membership's dates.",
    },
    "Applicability": {
        "jurisdiction_id": "The jurisdiction asked about.",
        "as_of": "The date it was assessed for.",
        "relation": "`national` (its own law; for a place within a country, such as `ng-la`, its country's "
        "law too); `regional_direct`, `regional_requires_transposition` or "
        "`regional_binding_on_addressees` (an instrument of a body it belonged to on the date, by its legal "
        "effect); `treaty_in_force_for_state` or `treaty_not_in_force_for_state` (by its own ratification or "
        "accession, never by membership of a body; missing from the party list, only when the list is "
        "complete on the date); `non_binding`; `comparative` (another jurisdiction's law, an instrument "
        "addressed to other states, or of a body it was not a member of on the date); or `unknown` (the "
        "membership, legal effect or ratification is not reviewed, the state is missing from a party list not "
        "known to be complete, or a date cannot be compared).",
        "binds": "`bound`, `not_bound` or `unknown`. Whether the text was in force is `currentness`.",
        "memberships": "The reviewed memberships it rests on.",
        "regional": "The instrument's legal effect it rests on.",
        "party": "The state's entry in the treaty's party list it rests on, with its domesticating acts.",
        "entered_into_force": "When the treaty entered into force, when recorded.",
        "transposed_by": "For a directive: the jurisdiction's acts that a checked link records as transposing it.",
        "reasons": "Why, in sentences.",
    },
    "ApplicableScope": {
        "jurisdiction_id": "The jurisdiction whose applicable law was asked for.",
        "within": "For a place within a country, such as `ng-la`, the country (`ng`): its law applies "
        "throughout the place and was added whole, and its memberships and treaty consents were assessed. "
        "Absent for a country.",
        "as_of": "The date memberships and treaties were assessed for.",
        "date_basis": "`as_of` (the date asked) or `today` (none was given; texts were not limited by date).",
        "membership": "`reviewed`, or `unknown` when no membership of the jurisdiction is reviewed yet (no "
        "body's law was added, which never means it belongs to none).",
        "bodies": "Each body it has a reviewed membership of, with whether it held on the date. Only the law of "
        "bodies `held` was added.",
        "admitted": "How many records of other jurisdictions were added, besides the law of `within`.",
        "left_out": "How many records of those bodies, or treaties naming the jurisdiction, were left out "
        "because they do not bind there, by relation.",
        "notices": "What the widening covered and did not, in sentences.",
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
    # Absolute, so a client generated from a downloaded copy calls the API, not the place it was saved.
    spec["servers"] = [{"url": "https://api.open.alvary.ai/api/v1", "description": "The REST API"}]
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
        # A route's own refusal at a status the gate also gives is added to the gate's words.
        errors = {} if words.get("public") else dict(GATE)
        for status, text in words["errors"].items():
            errors[status] = f"{errors[status]} {text}" if status in errors else text
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
