---
title: Retrieve evidence for AI answers
description: Get ranked, citable legal evidence for a question, check cited parts before an answer is shown, and keep a cache honest with the change feed.
---

If your application writes answers with a language model, use Open Alvary for the legal half: [Retrieve evidence](/docs/api/retrieve-evidence) returns the parts of published texts that answer a question, each with its exact words, its citation and everything needed to check it. Open Alvary never writes the answer; your application does, from this evidence and anything else it holds.

## Ask a question

```curl
curl -G https://api.open.alvary.ai/api/v1/retrieve \
  --data-urlencode "q=when must a data controller report a breach" \
  -d jurisdiction=ng \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

pack = requests.get(
    "https://api.open.alvary.ai/api/v1/retrieve",
    params={"q": "when must a data controller report a breach", "jurisdiction": "ng"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
for unit in pack["evidence"]:
    print(unit["citation"], unit["currentness"]["state"])
    print(unit["text"])
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/retrieve");
url.searchParams.append("q", "when must a data controller report a breach");
url.searchParams.append("jurisdiction", "ng");
const pack = await (
  await fetch(url, { headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` } })
).json();
```

Each unit in `evidence` is one part of a published text, best first:

| Field | Use it for |
|---|---|
| `text`, `complete` | The exact words to quote. When `complete` is false, `text` is an excerpt that fitted your byte budget; `/anchors/{anchor_id}` gives the whole part. |
| `citation`, `pinpoint` | How to cite it, such as `Nigeria Data Protection Act, 2023, s. 40(2)`. |
| `anchor_id`, `span_hash`, `content_hash` | How to check it later and link to it. Keep them with any answer that cites the part. |
| `links` | Where to read it: `web` on Open Alvary (at the part when the website shows this text), `api` for this exact part, `text` for the whole text. |
| `official_url`, `original_sha256`, `retrieved_at` | The official file the text was read from, and the hash to check a copy of it. |
| `page_start`, `page_end` | The pages of that file the part is on (1 is the file's first page, not a printed page number), when the text was read from a file with pages. |
| `currentness` | Whether it was in force. Read `basis`: `record` means only the whole law's status is known, which says nothing about one section; `provision` means reviewed events for this part. |
| `context.definitions` | The definitions the part relies on, with their own exact words. |
| `licence`, `attribution_text` | What quoting it asks for. |
| `ranking` | Why it ranks where it does: the route, each search's rank and every signal applied. |

Quote only `text`. A unit whose `unit` is `document` tells you which law is relevant; never cite it for what a provision says.

## Routes

A question that cites a part, names a law or asks what a term means (`meaning of "personal data"`) is answered by that first; anything else is a search of the words and, where a semantic index is active, of their meaning (`revisions.vector` names it). `query.routes` says which routes ran.

- **Citations as people write them:** `NDPA s. 23(1)(a)`, `section 23 of the Nigeria Data Protection Act`, `s. 23 Nigeria Data Protection Act 2023`, `Part V of the NDPA` (its heading, then its sections in order), `First Schedule, para. 16`. A law can be named by its title with or without the year, its citation, or its initials (`NDPA`; three letters only with the year, `ISA 2025`).
- **A pinpoint alone**, such as `s. 23`, is read within the record you limit the request to with `source_id`.
- **A page:** `page 18 of the NDPA` gives the provisions on that page of the official file.
- **An address:** paste an Open Alvary page or API address of a record or part, or a law's official address, and the pack holds what it addresses. Add `as_of` for the law on a date: only texts in force then, and currentness assessed at it. Without `as_of`, nothing is assumed about the date.

## Budgets

`max_units` (default 10, at most 25) and `max_evidence_bytes` (default 49,152) bound the answer. Definitions count towards the bytes once each. When the budget runs out, the last unit is an excerpt and later ones are left out: `coverage.incomplete_evidence` says so. The answer counts towards your organisation's [data a day](/docs/plans).

## Check before you answer

Before showing an answer that cites the corpus, send each cited part to [Check cited parts](/docs/api/check-currentness), up to 100 at a time:

```python
import os

import requests

check = requests.post(
    "https://api.open.alvary.ai/api/v1/currentness",
    json={"items": [{"anchor_id": unit["anchor_id"], "span_hash": unit["span_hash"]} for unit in pack["evidence"]]},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
for result in check["results"]:
    print(result["index"], result["result"])
```

`unchanged` means the part is still published with the same words. `newer_version` means a later text of the same edition exists: `newer_anchor_id` is the same part there and `same_words` says whether it changed. `unavailable` means the part is not public now: remove it from the answer.

## Caching and the change feed

You may cache evidence for your own users, keyed by `anchor_id` and `content_hash`, and whole answers keyed by the request and `revisions.dataset`. Keep the cache honest with the [change feed](/docs/changes):

| Change | What to do |
|---|---|
| `withdrawal` | Delete everything that cites that text, at once. |
| `text_version_published` | Treat cached parts of that edition as stale: check them before reuse. |
| `legal_effect_recorded` | Treat cached currentness for that law as stale. |
| A feed reset | Check every cached part before reuse. |

If Open Alvary cannot be reached, you may answer from your cache, but say that currentness was not checked, never serve a part whose withdrawal you have seen, and stop after seven days without reading the feed. Don't build your own index of the corpus for retrieval; the [releases](/docs/api/list-releases) are there for full copies.

## From an AI tool

The MCP tools [`retrieve_evidence`](/docs/api/mcp#retrieve_evidence) and [`check_currentness`](/docs/api/mcp#check_currentness) answer exactly what these routes answer, within the MCP answer size (at most 15 units and 40,960 bytes of words).
