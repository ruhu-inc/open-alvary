---
title: Find records
description: Find records by title, citation or text, list them, and resolve citations.
---

There are three ways to find records: **search** by words, **list** records with filters, and **resolve** a citation you already have.

## Search

[Search](/docs/api/search) finds records whose title or citation contains your words, and records whose cleared full text does.

```curl
curl -G https://api.open.alvary.ai/api/v1/search \
  --data-urlencode "q=central bank" \
  -d jurisdiction=ng \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

page = requests.get(
    "https://api.open.alvary.ai/api/v1/search",
    params={"q": "central bank", "jurisdiction": "ng"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
for record in page["items"]:
    print(record["citation"], "-", record["title"])
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/search");
url.searchParams.append("q", "central bank");
url.searchParams.append("jurisdiction", "ng");
const page = await (
  await fetch(url, { headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` } })
).json();
for (const record of page.items) console.log(record.citation, "-", record.title);
```

How words match:

- Your words are found **as a phrase**, in that order: `central bank` finds the Central Bank of Nigeria Act; `bank central` doesn't.
- Case and extra spaces don't matter, and letters are compared in a normalised form, so `ＦＯＩ` finds `FOI`.
- Part of a word matches too: `invest` finds the Investments and Securities Act.
- A search covers catalogue details and **cleared** full text only. Text that isn't cleared for reuse here isn't searched.

### Filters

| Parameter | Use |
|---|---|
| `jurisdiction` | One jurisdiction ID, such as `ng`. A country includes its states and local law. Without it, a search covers your plan's countries. |
| `access` | `open` for records with cleared full text, `metadata` for records without, `all` (default) for both. |
| `limit`, `offset` | Page size (1 to 100, default 25) and where to start. See [Pagination](/docs/pagination). |

Asking about a country outside your plan works, at the Starter rate; see [Plans and rate limits](/docs/plans#how-limits-are-counted).

## List records

[List records](/docs/api/list-sources) returns the catalogue a page at a time, with the same `jurisdiction`, `access`, `limit` (default 50) and `offset`. Use it to go through everything in a country:

```curl
curl -G https://api.open.alvary.ai/api/v1/sources \
  -d jurisdiction=ng -d limit=100 \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

For a complete copy of the catalogue, a [data release](/docs/api/list-releases) is quicker, and the [change feed](/docs/changes) keeps it current.

## Resolve a citation

When you already have a citation, [Resolve a citation](/docs/api/resolve-citation) says which records it refers to. Encode the citation for the address:

```curl
curl https://api.open.alvary.ai/api/v1/citations/FOI%20Act%202011 \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

The answer's `status` is `resolved` (one record), `ambiguous` (several: show the `matches` and let the reader choose) or `unresolved` (none). A citation matches the forms the catalogue records for each law, ignoring case and spacing; if yours is written differently, search for it instead.

## Searching from an AI tool

Connected AI tools search with the `search_catalogue` tool, which works the same way, up to 25 records at a time. Ask in your own words, for example "Find Nigeria's Freedom of Information Act and tell me whether its text may be reused." See [Connect AI tools](/docs/ai-tools).

## On the website

[Explore the corpus](/browse) on the website searches the same catalogue, with filters for country, type, language, status and year. It needs no account.
