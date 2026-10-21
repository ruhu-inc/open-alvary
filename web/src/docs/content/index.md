---
title: Overview
description: Search, read and follow Africa's published law from your own code and AI tools.
---

Open Alvary is a public catalogue of African law: what each law is, where its official text lives, and whether that text may be reused. The **REST API** and the **MCP server** give your code and your AI tools the same catalogue as [the website](/), with each record's rights status. The **change feed** tells you what changed, in order.

## At a glance

| | |
|---|---|
| REST API | `https://api.open.alvary.ai/api/v1` |
| MCP server | `https://api.open.alvary.ai/mcp` |
| Sign-in | `Authorization: Bearer <key>` with an API key, or sign in from your AI tool |
| Access | The free Starter plan, straight away, in [the console](/console) |
| Change feed | `https://open.alvary.ai/api/changes`, no key needed |
| Answers | JSON; refusals are [problem documents](/docs/errors) with a code |
| Description | [OpenAPI document](https://api.open.alvary.ai/api/v1/openapi.json) |

## Make your first call

1. Sign in to [the console](/console) with Google or an email code. The first time, give your name; an organisation name is optional (left blank, your own name is used). Your organisation gets the free Starter plan at once, with no card.
2. Under **API keys**, choose **Create key**, copy the key and keep it in an environment variable:

```bash
export OPEN_ALVARY_KEY="oa_live_..."
```

3. Search the catalogue:

```curl
curl -G https://api.open.alvary.ai/api/v1/search \
  --data-urlencode "q=freedom of information" \
  -d jurisdiction=ng \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

response = requests.get(
    "https://api.open.alvary.ai/api/v1/search",
    params={"q": "freedom of information", "jurisdiction": "ng"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
)
response.raise_for_status()
for record in response.json()["items"]:
    print(record["id"], record["title"], record["rights_status"])
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/search");
url.searchParams.append("q", "freedom of information");
url.searchParams.append("jurisdiction", "ng");
const response = await fetch(url, {
  headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
});
if (!response.ok) throw new Error((await response.json()).detail);
for (const record of (await response.json()).items) {
  console.log(record.id, record.title, record.rights_status);
}
```

The [Quickstart](/docs/quickstart) walks through it, with pictures of the console and what the answer means.

## Connect an AI tool

Claude, Claude Code, Codex, ChatGPT and other tools that speak MCP connect to `https://api.open.alvary.ai/mcp`. They sign you in, you approve them for your organisation, and they can then search and read the catalogue when you ask. No key is needed. See [Connect AI tools](/docs/ai-tools).

## Follow changes

Every record added, corrected or withdrawn, every text published, every checked legal effect such as a repeal, and every reviewed membership of a regional body, or suspension from one, is recorded in order. Read the feed with a resume token to get only what is new, subscribe to the Atom or RSS feed, or set up a monitor in the console to be emailed or to receive webhooks. See [Follow changes](/docs/changes).

## Retrieve evidence for AI answers

If your application writes answers with a language model, [Retrieve evidence](/docs/retrieval) gives it the parts of published texts that answer a question, each with its exact words, citation and hashes, and [Check cited parts](/docs/retrieval#check-before-you-answer) confirms them before an answer is shown. Open Alvary never writes the answer.

## Key terms

| Term | Means | In the API |
|---|---|---|
| Record | One law in the catalogue: its title, citation, type, legal status, official link and rights status. | A `source`: `/sources/{id}`, `source_id`. In the change feed, the `work`. |
| Text version | One cleared text of a record, read from one official file, in one language. | A `version`: `/versions/{id}`, `version_id`. |
| Edition | A record's texts in one language, with what that language's text is worth as law. | An `expression`: `expression_id`, written `<record id>:<language>`. |
| Anchor | One part of a text version, such as a section or an article, with its own ID. | `anchor_id`: `/anchors/{id}`, and each part's `id` in a structure. |
| Locator | A part's place in its text, such as `sec_23__subsec_1`. The same provision keeps it across versions. | `locator` |
| `span_hash` | The SHA-256 of a part's bytes: check a quotation against it. | On anchors, evidence units and resolved citations. |
| `content_hash` | The SHA-256 of a whole text version in UTF-8. | On versions, anchors and evidence units. |
| ETag | A tag for an answer. Send it back as `If-None-Match` to learn whether your copy is unchanged. | The `ETag` header. |
| Evidence unit | One part of a published text in a retrieval answer, with its exact words and everything needed to cite and check it. | An item of `evidence`. |
| Evidence pack | The answer of Retrieve evidence: ranked evidence units, what was searched, and the revisions they depend on. | The whole `/retrieve` answer. |
| Applicability label | How a record relates to a country on a date, such as `national` or `treaty_in_force_for_state`, and whether it binds there. | `applicability.relation` and `applicability.binds`, with `scope=applicable` or a check's `jurisdiction`. |

## Explore the docs

:::cards
- [Quickstart](/docs/quickstart) Your key, your first request, and what the answer means.
- [Console guide](/docs/console) Every page of the console, with pictures.
- [Authentication](/docs/authentication) API keys, connected apps and the second factor.
- [Plans and rate limits](/docs/plans) What each plan allows, and what to do at a limit.
- [API reference](/docs/api) Every route, parameter and field, with examples.
- [MCP tools](/docs/api/mcp) The tools AI tools use, and how to call them yourself.
- [Find records](/docs/search) Search by words, search published texts, list by country, and resolve citations.
- [Read a record and its text](/docs/records) A record's details, its history, its texts and one part exactly.
- [Retrieve evidence for AI answers](/docs/retrieval) Ranked, citable evidence for a question, checked before you answer.
- [Jurisdictions, bodies and courts](/docs/jurisdictions) Which regional bodies a country belongs to on a date, and court registers.
- [Rights and reuse](/docs/rights) What you may do with each record and text.
- [Follow changes](/docs/changes) Keep a copy current, or be told when the catalogue changes.
- [Download and mirror the data](/docs/mirror) Releases, their checksums, and catching up with the change feed.
- [Webhooks](/docs/webhooks) Signed deliveries of your monitors' matches.
- [Connect AI tools](/docs/ai-tools) Ask Claude, ChatGPT or Codex about the catalogue, with no key.
- [Pagination](/docs/pagination) Pages of records, and cursors through changes.
:::

## Get unblocked

The [errors page](/docs/errors) explains every error code, and [Help](/docs/help) answers common questions. For anything else, write to [hello@ruhu.ai](mailto:hello@ruhu.ai). To report a vulnerability, see the [security page](/security).
