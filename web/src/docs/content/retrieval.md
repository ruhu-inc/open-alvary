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
| `expression_id`, `expression_authority` | The language edition the words are from, and what it is worth as law as its record states it: `authoritative`, `equally_authentic`, `official_translation`, `unofficial_translation`, `machine_translation`, or `unknown` when nothing is recorded. [Get a record's editions](/docs/api/get-expressions) gives the law's rule for which version prevails where versions differ. |
| `currentness` | Whether it was in force. Read `basis`: `record` means only the whole law's status is known, which says nothing about one section; `provision` means reviewed events for this part. |
| `context.definitions` | The definitions the part relies on, with their own exact words. |
| `licence`, `attribution_text` | What quoting it asks for. |
| `authority` | Where the text stands: an instrument's place in its hierarchy, or for a decision its court and how the court's decisions bear on other courts (see [Court authority](#court-authority)). |
| `ranking` | Why it ranks where it does: the route, each search's rank and every signal applied. |

Quote only `text`. A unit whose `unit` is `document` tells you which law is relevant; never cite it for what a provision says.

## Court authority

A decision's unit names its court in `authority.court`, as the [court register](/docs/api/get-court-register) of the jurisdiction asked about lists it, with whether a reviewer has checked the court's entry. `court.bears_on` lists how the court's decisions bear on other courts on the date assessed (`as_of`, or today), each relation once someone legally qualified for the jurisdiction has reviewed it, with the words of its source:

```json
"authority": {
  "document_type": "judgment",
  "court_relation": "unknown",
  "court": {
    "id": "ke-hc",
    "name": "High Court of Kenya",
    "jurisdiction": "ke",
    "review_state": "reviewed",
    "reviewed_on": "2026-10-01",
    "bears_on": [
      {
        "id": "ke-hc-on-ke-mc",
        "court": "ke-mc",
        "relation": "binding",
        "evidence": ["https://…"],
        "rests_on": [{"quote": "…", "provision": "para 17", "title": "…", "url": "https://…"}]
      }
    ]
  }
}
```

- `relation` says how the court's decisions bear, in its source's own terms:

  | `relation` | Meaning |
  |---|---|
  | `binding` | Precedent binds: the court bound must follow the decisions (stare decisis). |
  | `prevails_on_matter` | On the subject matter in `matter` only, the court's decisions take precedence over national courts' decisions, as EAC Treaty art. 33(2) gives the East African Court of Justice's decisions on the Treaty. Not stare decisis, and not an appeal. |
  | `binding_in_referred_case` | The ruling binds the court that referred the question to it, in that case only. |
  | `persuasive` | The decisions may be followed, but need not be. |
  | `not_applicable` | The decisions don't bear on that court. |

- `rests_on` quotes the source verbatim, with the provision; `evidence` lists the official sources. `court` is the court bound, by its ID in the register; null means the jurisdiction's courts generally.
- A regional court's bearing on member states' courts (`members`) is limited to the subject matter its treaty gives (`matter`), such as the Uniform Acts for the OHADA court: never a rank above national courts.
- `court_relation` is the ranking signal: the court's bearing on the jurisdiction's courts generally. Within a band, `binding` decisions rank first, then `persuasive` ones; `binding_in_referred_case`, `not_applicable` and `unknown` rank alike. `prevails_on_matter` would rank with `binding`, but only on a question within its matter, and a request doesn't state a matter: it is never the `court_relation` here, only a bearing in `bears_on`.
- Nothing is guessed. Each register entry is reviewed on its own. A relation answers once it has its own review by someone legally qualified for the jurisdiction, the courts it names have theirs and, for `binding`, `persuasive` and `not_applicable`, the register's precedent rule has its own; until then it is not in `bears_on`, and `court_relation` is `unknown`. An entry changed after its review is unreviewed again. A relation the register does not record is `unknown` too: it is never inferred from a court's rank or appeal routes, and a bearing limited to a matter never becomes a general one.

[List court registers](/docs/api/list-court-registers) says which jurisdictions have a register and how many of its entries have been reviewed.

## Routes

A question that cites a part, names a law or asks what a term means (`meaning of "personal data"`) is answered by that first; anything else is a search of the words and, where a semantic index is active, of their meaning (`revisions.vector` names it). `query.routes` says which routes ran.

- **Citations as people write them:** `NDPA s. 23(1)(a)`, `section 23 of the Nigeria Data Protection Act`, `s. 23 Nigeria Data Protection Act 2023`, `Part V of the NDPA` (its heading, then its sections in order), `First Schedule, para. 16`. In French, an article's alinéa is read as its `(2)`, an article numbered after another by its printed number (`art. 2-1`), and the law may follow `de`, `du`, `de la`, `de l'` or `des`: `article 10, alinéa 2, de l'Acte uniforme …`, `art. 10 al. 2`. A law can be named by its title with or without the year, its citation, or its initials (`NDPA`; three letters only with the year, `ISA 2025`).
- **A pinpoint alone**, such as `s. 23`, is read within the record you limit the request to with `source_id`.
- **A page:** `page 18 of the NDPA` gives the provisions on that page of the official file.
- **An address:** paste an Open Alvary page or API address of a record or part, or a law's official address, and the pack holds what it addresses. Add `as_of` for the law on a date: only texts in force then, and currentness assessed at it. Without `as_of`, nothing is assumed about the date.

## The law that applies in a country

A request covers exactly the jurisdictions you name: `jurisdiction=sn` searches Senegal's own law. To also search the regional and treaty law that binds in Senegal, add `scope=applicable`, with one `jurisdiction`:

```curl
curl -G https://api.open.alvary.ai/api/v1/retrieve \
  --data-urlencode "q=who may manage a commercial company" \
  -d jurisdiction=sn -d scope=applicable -d as_of=2026-01-01 \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

pack = requests.get(
    "https://api.open.alvary.ai/api/v1/retrieve",
    params={"q": "who may manage a commercial company", "jurisdiction": "sn", "scope": "applicable",
            "as_of": "2026-01-01"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
for unit in pack["evidence"]:
    print(unit["citation"], unit["applicability"]["relation"], unit["applicability"]["binds"])
print(pack["applicability"]["left_out"])
```

The search then also covers:

- the instruments of the African Union and the regional bodies the country belonged to on `as_of` (today's date if you leave it out), from [reviewed memberships](/docs/records#find-which-bodies-a-country-belongs-to) only. A body whose membership is not reviewed yet is not added, which never means the country is not a member;
- the treaties whose party list records the country's own ratification or accession. Membership of a body never applies its treaties to a member.

A place within a country, such as Lagos State (`jurisdiction=ng-la`), has its country's law too: national and federal law applies throughout a country's states and places, so the search also covers Nigeria's own law (`ng`), labelled `national`. The regional bodies and treaties are those of the country, the member state and party; another place's law within the country is `comparative`. Each label that rests on the country says so in its `reasons` (`ng-la is within ng: …`).

Of those, instruments that don't bind in the country are left out, and those whose relation to it is unknown are kept. Each unit's `applicability` says how it relates to the country on that date:

| `relation` | Meaning | `binds` |
|---|---|---|
| `national` | The country's own law. For a place within a country, its own law and the country's. | `bound` |
| `regional_direct` | An instrument of a body the country belonged to, which applies directly in its members. | `bound` |
| `regional_requires_transposition` | A directive or similar: it binds the country as to its result, through national law. `regional.transposition_deadline` is the deadline, and `transposed_by` the country's transposing acts that a checked link records. | `bound` |
| `regional_binding_on_addressees` | It binds the member states it is addressed to, the country among them. | `bound` |
| `treaty_in_force_for_state` | The country ratified or acceded to it on or before the date, had not denounced it, and the treaty was in force. `party` is the country's entry, with any act that domesticates it. | `bound` |
| `treaty_not_in_force_for_state` | The country's entry in the party list gives no ratification or accession in force on the date: signature alone does not bind, nor does a ratification after the date or one denounced by then. A country missing from the list is `treaty_not_in_force_for_state` only when the list is recorded as complete on the date or later (`treaty.parties_complete_as_of` on the record); otherwise it is `unknown`, as the list may leave it out. | `not_bound` |
| `non_binding` | The instrument is not binding. | `not_bound` |
| `comparative` | Another country's law, another place's law within the same country, an instrument addressed to other states, or one of a body the country had left or not yet joined. | `not_bound` |
| `unknown` | The membership, the legal effect or the ratification is not reviewed, the country is missing from a party list not known to be complete, or a date can't be compared (a ratification known only to the year, say). | `unknown` |

Each label carries what it rests on: `memberships` (the reviewed memberships, each `held`, `not_held` or `unknown` on the date), `regional` (the instrument's legal effect) and `party` (the country's entry in the treaty's party list), with `reasons` in sentences. Whether a unit was in force on the date is still `currentness`, a separate question. These labels report what the reviewed records state; they never decide which law governs anyone's situation.

The pack's `applicability` states the widening: for a place, `within` names the country whose law was added whole; `bodies` lists each body the country has a reviewed membership of, with whether it held on the date (only `held` bodies' law was added), `admitted` how many records of other jurisdictions were added besides, and `left_out` how many were left out because they don't bind there, by relation. `query.filters` shows `scope` as you asked for it. A record you name with `source_id` is answered whatever its relation, so you can ask whether a particular treaty binds a country. Without `scope`, nothing is added and no unit has `applicability`.

[Search texts](/docs/api/search-text) takes `scope=applicable` too, with the same `applicability` on each passage and on the page.

## Languages

A question's words are matched against each text in the text's own language:

| Texts in | How words match |
|---|---|
| English, French, Portuguese, Spanish | By word stems, so `lends` finds `lend` and `trabalhador` finds `trabalhadores`. Common words such as `the`, `le`, `os` and `los` are left out. |
| Arabic | By word stems, so `موظف` finds `للموظفين`. Every word is kept: there is no list of common Arabic words. |
| Any other language, such as Kiswahili or Kinyarwanda | Word for word: `mkataba` doesn't match `mikataba`. `coverage.notices` says so for each such language in your scope. |

These are rules for matching word forms, not a measure of how well questions are answered in each language. `revisions.analyzer` names the rules that ran (`oa-search-5`). Add `language` to search texts in one language only; `en` also matches `en-NG`. Questions aren't translated, so matching words finds only texts in the question's language.

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

Add `jurisdiction` to an item, such as `{"anchor_id": "…", "as_of": "2026-01-01", "jurisdiction": "ke"}`, and its result also says whether the record binds in that country on that date: `applicability`, with the same labels and basis as [above](#the-law-that-applies-in-a-country). For a treaty, that is `treaty_in_force_for_state` only where the country's own ratification or accession is recorded.

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

The MCP tools [`retrieve_evidence`](/docs/api/mcp#retrieve_evidence) and [`check_currentness`](/docs/api/mcp#check_currentness) answer exactly what these routes answer, within the MCP answer size (at most 15 units and 40,960 bytes of words). `retrieve_evidence` and `search_text` take `scope`, and `check_currentness` items take `jurisdiction`.
