---
title: Changelog
description: Changes to the API, the MCP server and these docs.
---

Changes that affect how you use the API and the MCP server, newest first.

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
