---
title: Find records
description: Find records by title, citation or text, search the words of published texts, and resolve citations to the part they name.
---

There are four ways to find what you need: **search** records by words, **search published texts** for the passages that match, **list** records with filters, and **resolve** a citation you already have.

## Search

[Search](/docs/api/search) finds records with all your words in their title, citation or summary, and records with all of them in one part of their cleared full text.

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

- **Every word** must be in the record, in any order: `central bank` and `bank central` both find the Central Bank of Nigeria Act. The words can be spread over its title, citation and summary, but in a text they must all be in one part, such as a section.
- **Whole words only**: `investments` finds the Investments and Securities Act; `invest` doesn't.
- Case, punctuation and extra spaces don't matter, and letters are compared in a normalised form, so `ＦＯＩ` finds `FOI` and `43/2016` finds `n° 43/2016`. Accents count: `cafe` doesn't find `café`.
- In a text, words are matched as [Search published texts](#search-published-texts) matches them (English texts in English, so `lends` finds `lend`). Quotes, `-word` and `OR` have no special meaning here.
- Records come in ID order. A query with no letters or digits finds nothing, and `notices` says why; `notices` also says when published texts couldn't be searched, so you can try again.
- A search covers catalogue details and **cleared** full text only. Text that isn't cleared for reuse here isn't searched.

### Filters

| Parameter | Use |
|---|---|
| `jurisdiction` | One jurisdiction ID, such as `ng`. It matches that jurisdiction only: `ng` finds Nigeria's national records, not Lagos State's (`ng-la`). Without it, a search covers every jurisdiction in your plan, its countries' states and local law included. |
| `access` | `open` for records with cleared full text, `metadata` for records without, `all` (default) for both. |
| `limit`, `offset` | Page size (1 to 100, default 25) and where to start. See [Pagination](/docs/pagination). |

Asking about a country outside your plan works, at the Starter rate; see [Plans and rate limits](/docs/plans#how-limits-are-counted).

The same goes for `jurisdiction` in [Search published texts](#search-published-texts), [List records](#list-records) and [Retrieve evidence](/docs/retrieval). Two places work differently: the [change feed](/docs/changes#filters)'s `jurisdiction` filter matches by prefix, so a country there includes its states and local law; and the [applicable-law scope](/docs/retrieval#the-law-that-applies-in-a-country) (`scope=applicable`) adds a place's country, with the regional and treaty law that binds there.

## Search published texts

[Search published texts](/docs/api/search-text) finds the **passages** that match, not just the records: each hit is a section, an article or a schedule paragraph of a published text, with a snippet of its exact words, a citation such as `s. 23(1)` and the anchor to get the part whole.

```curl
curl -G https://api.open.alvary.ai/api/v1/search/text \
  --data-urlencode "q=personal data consent" \
  -d jurisdiction=ng \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

page = requests.get(
    "https://api.open.alvary.ai/api/v1/search/text",
    params={"q": "personal data consent", "jurisdiction": "ng"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
print(page["status"], page["coverage"]["notice"])
for hit in page["items"]:
    print(hit["citation"], "-", hit["snippet"])
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/search/text");
url.searchParams.append("q", "personal data consent");
url.searchParams.append("jurisdiction", "ng");
const page = await (
  await fetch(url, { headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` } })
).json();
console.log(page.status, page.coverage.notice);
for (const hit of page.items) console.log(hit.citation, "-", hit.snippet);
```

How words match:

- Every word must be in the passage, in any order. `"exact phrase"` keeps words together, `-word` leaves passages with it out, and `OR` between words accepts either.
- English texts are matched in English: `lends` finds `lend`, and common words such as `the` are left out. French, Portuguese and Spanish texts are matched in their language the same way (`prêter` finds `prête`, with accents counting; `trabalhador` finds `trabalhadores`; `trabajador` finds `trabajadores`). Arabic texts are matched by word stems too (`موظف` finds `للموظفين`), but every word is kept, as there is no list of common Arabic words. Texts in other languages, such as Kiswahili and Kinyarwanda, are matched word for word until each passes our search tests: `mkataba` doesn't find `mikataba`, and `notices` says so. `analysis` says which applies.
- A citation of a part, such as `NDPA s. 23(1)(a)`, is resolved rather than searched: that part comes first, marked `matched_by: citation`.

Each hit's `snippet` is the text's own bytes from `snippet_start_byte` to `snippet_end_byte`, so it can be quoted exactly; `highlights` gives the matching words as byte offsets. Check the hit's `licence` and `attribution_text` before you quote it.

**Ranking.** Passages are ordered by how closely and how often your words appear, counting a part's heading most, then its words, then its record's title and citation. A text no longer in force (its record is repealed, superseded or historical, or a later text replaces it) counts 0.8 of that. Ties go to the earlier passage. Scores rank passages; they don't say which law applies.

**What was searched.** Only published texts are searched. `coverage` says how many records in the scope have one, and `status` tells the cases apart:

| `status` | Means |
|---|---|
| `results` | Passages matched. |
| `no_result` | Published texts in the scope were searched and none matched. |
| `unsupported_scope` | No published text is in the scope (a country, type, language or date with none). Search the records instead. |
| `degraded` | The search could not run; `notices` says why. Try again later. |

Filter with `jurisdiction`, `language`, `type`, `status` (legal status), `as_of` (texts in force on a date, from their effective dates; a text with no effective date is left out), `year_from` and `year_to` (the year the law was made), `source_id` and `version_id`. Pages are 25 passages by default and 100 at most, continued with `cursor`; the first 1,000 passages are served.

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

The answer's `status` is `resolved` (one record), `ambiguous` (several: show the `matches` and let the reader choose) or `unresolved` (none). A citation matches the forms the catalogue records for each law, and its title, ignoring case and spacing; if yours is written differently, search for it instead.

### Cite a part

Add a part after the citation, such as `FOI Act 2011, s. 1(1)`, `section 1 of the FOI Act 2011`, `art. 12` or `First Schedule, para. 3`, and the answer also has `provision`: the part in the record's published text, with its `anchor_id`, `locator` and exact span (`start_byte`, `end_byte` and `span_hash`). [Get one part of a text](/docs/api/get-anchor) gives its words.

```curl
curl "https://api.open.alvary.ai/api/v1/citations/FOI%20Act%202011,%20s.%201(1)" \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

Nothing is chosen for you. When the part is in several texts, such as two languages, `provision.status` is `ambiguous`, every candidate is listed and `reason` says why; add `language` or `as_of` to choose. When it can't be found, `reason` says whether the record has no published text, none matches your `language` or `as_of`, or the text has no such part; `nearest` then gives the closest part that does exist, such as the section of a paragraph the text doesn't divide.

The citation each [anchor](/docs/api/get-anchor) gives resolves back to that anchor.

## Searching from an AI tool

Connected AI tools search records with the `search_catalogue` tool, search published texts with `search_text`, and resolve citations with `resolve_citation`, which work the same way. Ask in your own words, for example "Find Nigeria's Freedom of Information Act and tell me whether its text may be reused." See [Connect AI tools](/docs/ai-tools).

## On the website

[Explore the corpus](/browse) on the website searches the same catalogue, with filters for country, type, language, status and year. It needs no account.
