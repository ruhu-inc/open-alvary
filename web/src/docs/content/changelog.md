---
title: Changelog
description: Changes to the API, the MCP server and these docs.
---

Changes that affect how you use the API and the MCP server, newest first.

## 21 October 2026

- [Retrieve evidence](/docs/api/retrieve-evidence) and [Resolve a citation](/docs/api/resolve-citation): French citations are read too. An article's alinéa is read as its `(2)` (`article 10, alinéa 2`, `art. 10 al. 2`), and the law may follow `de`, `du`, `de la`, `de l'` or `des` (`article 10, alinéa 2, de l'Acte uniforme …`). A citation is no longer also read as a shorter pinpoint in a law named by the rest (`article 10` in a law called `alinéa 2`).
- [Search](/docs/api/search) (`GET /api/v1/search`, MCP `search_catalogue`) and the website's search match **whole words** from an index, no longer any run of letters. Every word must be in the record, in any order (it was a phrase, in order); `invest` no longer finds `Investments`. Words are looked for in a record's summary too, and in one part of a cleared text rather than anywhere in it. See [Find records](/docs/search#search).
- Search answers may have `notices`: why published texts weren't searched (the index is being built, or that search took too long), or that the query has no letters or digits. A query of punctuation alone now finds nothing instead of matching it.
- [Get a record's editions](/docs/api/get-expressions) (`GET /api/v1/sources/{id}/expressions`, and the new MCP tool `list_expressions`): each edition's `authority` is now what the record states (`authoritative`, `equally_authentic`, `official_translation`, `unofficial_translation`, `machine_translation`, or `unknown` when nothing is recorded), with `authority_evidence` saying where it is stated. The answer adds `language_rule`: the law's own rule for which version prevails where its language versions differ. See [Language editions](/docs/records#language-editions).
- [Get a record's texts](/docs/api/get-versions) and [Retrieve evidence](/docs/api/retrieve-evidence): each text and each unit carries its edition's `expression_authority`, with `expression_authority_evidence` when recorded.
- [Search texts](/docs/search) and [Retrieve evidence](/docs/retrieval): Portuguese and Spanish texts are matched by word stems with their common words left out, and Arabic texts by word stems with every word kept (`analysis` gives `portuguese`, `spanish` or `arabic`). Other languages, such as Kiswahili, are still matched word for word, and both answers say so in their notices, now in the same words. The analysis is `oa-search-5`.
- [Get a record's history](/docs/api/get-relationships) (also MCP `get_relationships`) lists more kinds of checked link: `commences`/`commenced_by`, `transposes`/`transposed_by`, `domesticates`/`domesticated_by`, `implements`/`implemented_by`, `ratifies`/`ratified_by` and `cites`/`cited_by`. See [what each kind means](/docs/records#find-what-replaced-a-law).
- `legal_effect_recorded` now also records commencements, transpositions, domestications and ratifications; for the last three each record has its own change, the law's naming the instrument in `legal_effect.of`. A withdrawn link, or a withdrawn event on provisions, corrects the change that recorded it (`review_state` `withdrawn`).

## 19 October 2026

- [Retrieve evidence](/docs/api/retrieve-evidence): each unit now gives the pages of the official file it is on (`page_start`, `page_end`), that file's hash and when it was fetched (`original_sha256`, `retrieved_at`), and `links` to read it on Open Alvary or by the API. Questions may cite a part as people write it (`section 23 of the Nigeria Data Protection Act`, `NDPA s. 23`, `Part V of the NDPA`), ask for a page (`page 18 of the NDPA`) or be an Open Alvary or official address; `source_id` reads a pinpoint alone within one record (also on MCP). Routes `link` and `page` are new (`oa-route-2`); the ranking profile is `oa-rank-3`.
- [Retrieve evidence](/docs/api/retrieve-evidence) (`GET /api/v1/retrieve`, MCP `retrieve_evidence`): ranked parts of published texts for a question, each with its exact words, citation, hashes, currentness, definitions and why it ranks there. See [Retrieve evidence for AI answers](/docs/retrieval).
- [Check cited parts](/docs/api/check-currentness) (`POST /api/v1/currentness`, MCP `check_currentness`): whether cited parts are still published with the same words, and whether a newer text exists.
- [Download a bulk release file](/docs/api/get-bulk-file) (`GET /api/v1/bulk/…`): releases with full text, with a key, for organisations whose access includes bulk releases (granted by agreement). [List data releases](/docs/api/list-releases) lists them only to those organisations, for their plan's countries. Catalogue releases still download from `/downloads/…` with no key.

## 17 October 2026

- These docs: guides, a reference for every route generated from the API's own description, and every page also as Markdown (add `.md` to its address) and in [`/llms.txt`](/llms.txt).
- Every refusal from `/api/v1` is now a problem document with a code, including a malformed parameter (`400 invalid_input`), an unknown record (`404 unavailable`) and an answer too large (`413 result_too_large`).
- The OpenAPI document describes every route, parameter, field and error.
- The MCP tools describe their arguments.
- A change's `work.api_url` links the record at `https://api.open.alvary.ai/api/v1/sources/<id>`.
- `/api/v1/releases` gives each release's files at `/downloads/…`, where they download.

## 16 October 2026

- The REST API at `https://api.open.alvary.ai/api/v1` and the MCP server at `https://api.open.alvary.ai/mcp` open, with API keys and sign-in for AI tools, in the console at `https://open.alvary.ai/console`.
- The free Starter plan for everyone; other plans on request.
- Monitors with an inbox and email digests; webhooks on paid plans.
- The anonymous API at `https://open.alvary.ai/api` closes and answers `410 gone`, except the change feed, which stays open.
