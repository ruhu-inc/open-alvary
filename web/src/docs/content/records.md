---
title: Read a record and its text
description: A record's details, its official source, and its full text once cleared.
---

Every law in the catalogue is a **record**: its title, citation, type, legal status and official link. When the rights to its text are cleared, the record also has one or more **text versions** you can read in full.

## Get a record

```curl
curl https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999 \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

record = requests.get(
    "https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999",
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
print(record["title"], record["legal_status"], record["canonical_url"])
```

```javascript
const record = await (
  await fetch("https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999", {
    headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
  })
).json();
console.log(record.title, record.legal_status, record.canonical_url);
```

What to look at:

| Field | Meaning |
|---|---|
| `id` | The record's ID. It never changes, so store it. The record's page on the website is `https://open.alvary.ai/sources/<id>`. |
| `canonical_url`, `url_kind` | The official source, and whether it is the `document` itself, the official `landing_page` for it, or an official listing (`discovery`). |
| `legal_status` | `current`, `historical`, `repealed`, `superseded` or `unknown`, as far as the catalogue has checked; `status_evidence` links the official proof when there is one. |
| `catalogue_note` | A public caveat about the record, when there is one. Show it to your readers. |
| `has_full_text` | Whether a cleared text can be read (below). |
| `rights_status` | What may be done with the text. See [Rights and reuse](/docs/rights). |

Every field is listed in the [reference](/docs/api/get-source).

## Find what replaced a law

[Get a record's history](/docs/api/get-relationships) tells you whether a record is current and how it relates to other records: the laws it repeals, amends or brings into force and the law it is made under, the later laws that repealed, amended or commenced it, the treaty or directive it transposes, domesticates, implements or ratifies (and, for a treaty or directive, the laws that do so), the records it cites and that cite it, and the instruments made under it. When the record has been repealed or replaced, `current` names the record in force in its place now, at the end of its line of repeals.

```curl
curl https://api.open.alvary.ai/api/v1/sources/ng-isa-2007/relationships \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

history = requests.get(
    "https://api.open.alvary.ai/api/v1/sources/ng-isa-2007/relationships",
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
if history["current"]:
    print("Read instead:", history["current"]["title"])
for link in history["items"]:
    print(link["kind"], link["title"], link["evidence_url"])
```

```javascript
const history = await (
  await fetch("https://api.open.alvary.ai/api/v1/sources/ng-isa-2007/relationships", {
    headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
  })
).json();
if (history.current) console.log("Read instead:", history.current.title);
for (const link of history.items) console.log(link.kind, link.title, link.evidence_url);
```

{{example:get_relationships}}

Each link is seen from this record's side and cites the official text that makes it, usually the repealing, amending or transposing law. A kind and its `_by` form are the same link seen from each end:

| From this record's side | From the other record's side | Meaning |
|---|---|---|
| `repeals`, `amends`, `supersedes` | `repealed_by`, `amended_by`, `superseded_by` | It repealed, amended or replaced the other record. |
| `commences` | `commenced_by` | It brought the other record into force. |
| `made_under` | `enables` | It was made under the other record, its parent law. |
| `transposes`, `domesticates`, `implements`, `ratifies` | `transposed_by`, `domesticated_by`, `implemented_by`, `ratified_by` | It gives effect, in national law, to a treaty or a regional community's directive or instrument. |
| `cites` | `cited_by` | It refers to the other record. A citation says nothing about agreement or legal effect. |
| `follows`, `applies`, `distinguishes`, `doubts`, `overrules` | `followed_by`, `applied_by`, `distinguished_by`, `doubted_by`, `overruled_by` | A judgment's paragraph treats the other judgment so, as a legally qualified reviewer recorded it. See [Case law](#case-law-citations-and-treatment). |
| `cross_reference` | `cross_reference` | A reference between the two, shown the same way from either side. |

Links come 25 to a page (`limit` up to 100); pass `next_cursor` as `cursor` for the next, or ask for one `kind` only, such as `enables` for the instruments made under a law or `transposed_by` for the national laws that transpose a directive. Only links one step away are listed (depth 1), and only between public records that a reviewer has checked against that text, so a record may be repealed before its history shows by what.

## Case law: citations and treatment

A judgment's history also lists, paragraph by paragraph, the provisions and the judgments it cites, and how later judgments treated it. Each comes from the judgment's published text: we read the citations in each numbered paragraph, and a reviewer confirms each one against the paragraph before it is listed. Nothing is guessed: a citation we read but no one has confirmed is not listed.

To find the judgments that cite a provision, ask the provision's record for `cited_by` with the provision's `locator` (from the text's [structure](#parts-chapters-and-sections)). A locator includes the parts within it, so `sec_2` also finds citations of `sec_2__subsec_1`:

```curl
curl "https://api.open.alvary.ai/api/v1/sources/ng-data-protection-act-2023/relationships?kind=cited_by&locator=sec_2" \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

citing = requests.get(
    "https://api.open.alvary.ai/api/v1/sources/ng-data-protection-act-2023/relationships",
    params={"kind": "cited_by", "locator": "sec_2"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
for link in citing["items"]:
    print(link["title"], link["citing_pinpoint"], "cites", link["cited_pinpoint"])
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/sources/ng-data-protection-act-2023/relationships");
url.search = new URLSearchParams({ kind: "cited_by", locator: "sec_2" });
const citing = await (await fetch(url, { headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` } })).json();
for (const link of citing.items) console.log(link.title, link.citing_pinpoint, "cites", link.cited_pinpoint);
```

Each citation names the citing paragraph (`citing_locator`, and `citing_pinpoint` such as `[12]` or `op. 2 [4]` for a later opinion's paragraph) and the part it cites (`cited_locator` and `cited_pinpoint`, such as `s. 2(1)`; absent when the paragraph cites the record as a whole, or its pinpoint names no part of a public text). While the citing judgment's text is public, `printed` gives the citation as the paragraph writes it and `quote` the paragraph's words (the sentences around the citation, for a long paragraph). `review` says who confirmed it, in what role and when; confirming a citation is a check of fact, so its `level` is `structural`. On a judgment, `kind=cites` lists what it cites, and `locator=para_12` what one paragraph cites.

A judgment's treatment of another judgment is listed only where a legally qualified reviewer has recorded it: `follows`, `applies`, `distinguishes`, `doubts` or `overrules` from the treating judgment's side, and `followed_by`, `applied_by`, `distinguished_by`, `doubted_by` or `overruled_by` from the judgment treated, which lists them first. Each gives the treating paragraph, the passage that does it (`quote`), the `opinion` the paragraph is in (`majority`, `concurrence`, `dissent`, `plurality`, `order` or `unknown`: a dissent's words are not the court's holding) and a `review` whose `level` is `doctrinal`, with the reviewer's `reviewer_qualification`.

> Note: No treatment listed means none has been recorded, not that the judgment is good law. A treatment is never inferred from a citation: a judgment that cites another may agree with it, disagree or neither. An overruled judgment is not repealed, so `current` stays empty and its `legal_status` is unchanged.

Changes to treatments are in the [change feed](/docs/changes) as `treatment_recorded`. Appeals between courts in the same case (appealed from, affirmed, reversed) are not listed yet.

## Regional instruments and treaties

Which bodies a country belongs to, and the court registers, are in [Jurisdictions, bodies and courts](/docs/jurisdictions).

A regional body's instrument, such as an OHADA Uniform Act or an ECOWAS directive, has `regional` on its record ([Get a record](/docs/api/get-source)), on its [anchors](/docs/api/get-anchor) and on each evidence unit from it:

| Field | Meaning |
|---|---|
| `organ` | The organ that adopted it, such as the Council of Ministers. |
| `legal_effect` | `directly_applicable` (it applies in the members as it stands), `requires_transposition` (it binds the members as to its result, through national law), `binding_on_addressees` (it binds the member states it is addressed to) or `non_binding`. |
| `member_states` | The member states it binds. Empty means every member. |
| `transposition_deadline` | The date by which members must transpose it, when one is set. |

A treaty has `treaty` instead: `adopted_by`, `depositary`, `entered_into_force` and `authentic_languages`, and on the record and its anchors, `parties`. Each party gives the dates it `signed`, `ratified`, `acceded` and `denounced` the treaty, when recorded, its `reservations`, and `domesticated_by`: the acts of that state that a checked link records as domesticating the treaty. Signature alone never binds a state. An empty `parties` list means none is recorded yet, never that the treaty has none.

A party list may leave a state out. Once a reviewer has checked the list against the depositary's status list, the record and its anchors give `parties_complete_as_of`, the date the list is complete as of, and `parties_complete_evidence`, where that is shown. Until then, or after that date, a state missing from the list is not taken to be a non-party: whether the treaty binds it is `unknown`. An evidence unit carries the treaty's status without its parties or their completeness.

A date gives its `precision` (`day`, `month` or `year`) and, when the record keeps it, the date as `printed`. Records of other kinds have neither `regional` nor `treaty`.

## Read the text

[Get a record's texts](/docs/api/get-versions) returns the record's cleared text versions, in full:

```curl
curl https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999/versions \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

Texts appear as their rights are cleared and a reviewer has checked them against the official file. Until then a record's answer has no versions and a `notice` saying why:

```json
{
  "items": [],
  "rights": { "status": "RIGHTS_UNDER_REVIEW", "...": "..." },
  "structure": [],
  "notice": "No version has passed both rights and content review."
}
```

To find records with a text, search or list with `access=open`, or watch [coverage](/docs/api/get-coverage)'s `open_full_text_sources`.

### What a version contains

| Field | Meaning |
|---|---|
| `canonical_text` | The full text. |
| `content_hash` | The SHA-256 of `canonical_text` in UTF-8. Check your copy against it. |
| `canonical_url`, `retrieved_at` | The official file it was read from, and when. |
| `effective_from`, `effective_until` | When this text applies, when known. |
| `verification_status`, `content_verifier`, `content_verified_at` | Whether, by whom and when it was checked against the official file. |
| `expression_id`, `expression_authority` | The language edition the text belongs to, and what that edition is worth as law ([below](#language-editions)). |

### Parts, chapters and sections

`structure` lists the parts, chapters, sections and other divisions of each text, in order. Each one gives `start_byte` and `end_byte`: byte positions in the UTF-8 encoding of `canonical_text`, not character positions. Slice the bytes, then decode:

```python
import hashlib
import os

import requests

answer = requests.get(
    "https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999/versions",
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
if not answer["items"]:
    raise SystemExit(answer["notice"])  # no cleared text yet
version = answer["items"][0]
data = version["canonical_text"].encode("utf-8")
assert hashlib.sha256(data).hexdigest() == version["content_hash"]

for node in answer["structure"]:
    if node["version_id"] == version["id"] and node["kind"] == "section":
        print(node["locator"], data[node["start_byte"] : node["end_byte"]].decode("utf-8")[:80])
```

```javascript
import { createHash } from "node:crypto";

const answer = await (
  await fetch("https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999/versions", {
    headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
  })
).json();
if (answer.items.length === 0) throw new Error(answer.notice); // no cleared text yet
const version = answer.items[0];
const data = new TextEncoder().encode(version.canonical_text);
const hash = createHash("sha256").update(data).digest("hex");
if (hash !== version.content_hash) throw new Error("text changed in transit");

for (const node of answer.structure) {
  if (node.version_id === version.id && node.kind === "section") {
    console.log(node.locator, new TextDecoder().decode(data.slice(node.start_byte, node.end_byte)).slice(0, 80));
  }
}
```

`parent_id` says which division a node sits in, and `page` which page of the official file it starts on, when known.

## Read one text or one part

Get a record's texts gives every text with its structure in one answer. To read one text, or one part of it, use these routes. Each takes an ID from an answer you already have:

| Route | Answers | IDs from |
|---|---|---|
| [`/versions/{version_id}/text`](/docs/api/get-version-text) | The text alone, as `text/plain; charset=utf-8`: exactly the bytes whose SHA-256 is its `content_hash`, which every offset refers to. | A version's `id` in [Get a record's texts](/docs/api/get-versions), or `version.id` in a `text_version_published` change |
| [`/versions/{version_id}/structure`](/docs/api/get-version-structure) | The text's parts in document order, 100 a page by default (`limit` up to 500), continued with `next_cursor`. Each part's `id` is its anchor. | The same |
| [`/anchors/{anchor_id}`](/docs/api/get-anchor) | One part exactly: its `text`, its span (`start_byte`, `end_byte`), the `span_hash` of its bytes, a `citation` such as `s. 23(1)(a)`, the `official_url` and the attribution its rights record asks for. The words are never shortened. | A part's `id` in a structure, or `anchor_id` in a passage, an evidence unit or a resolved citation |

```curl
curl "https://api.open.alvary.ai/api/v1/anchors/$ANCHOR_ID" \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

{{example:get_anchor}}

### Check a copy without downloading it again

Each of these answers has an `ETag`. For a text, it is the text's `content_hash` in quotes. Keep it with your copy, and send it back as `If-None-Match`:

```python
import os

import requests

anchor_id = "nod_…"  # from a structure, a passage or an evidence unit
etag = '"…"'  # the ETag you kept with your copy
response = requests.get(
    f"https://api.open.alvary.ai/api/v1/anchors/{anchor_id}",
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}", "If-None-Match": etag},
    timeout=30,
)
if response.status_code == 304:
    print("Unchanged and still public")
elif response.status_code == 404:
    print("No longer public: stop serving it")
else:
    response.raise_for_status()
    etag, part = response.headers["ETag"], response.json()
```

`304` means your copy is unchanged and still public: the same check runs as for a full answer. It counts as a request but sends no bytes, so it uses none of your [data a day](/docs/plans#how-limits-are-counted). `404` means the text or part is no longer public. A text or part too large for one answer is refused with `413` [`result_too_large`](/docs/errors#result_too_large): read a large text a part at a time.

## Language editions

A law published in several languages has one **edition** per language, with the ID `<record id>:<language>`. [Get a record's editions](/docs/api/get-expressions) lists the editions that have a text, and says what each is worth as law, as the record states it:

```curl
curl https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999/expressions \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

editions = requests.get(
    "https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999/expressions",
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
for edition in editions["items"]:
    print(edition["language"], edition["authority"], edition["preferred_version_id"])
if editions["language_rule"]["status"] == "stated":
    print("Where the versions differ:", editions["language_rule"]["rule"])
```

```javascript
const editions = await (
  await fetch("https://api.open.alvary.ai/api/v1/sources/ng-constitution-1999/expressions", {
    headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
  })
).json();
for (const edition of editions.items) console.log(edition.language, edition.authority);
```

| `authority` | Meaning |
|---|---|
| `authoritative` | The only authoritative text. |
| `equally_authentic` | One of several equally authentic texts. |
| `official_translation` | A translation published officially. It is not the authoritative text. |
| `unofficial_translation` | A translation with no official standing. |
| `machine_translation` | A machine translation. |
| `unknown` | Nothing recorded says. Don't assume either way. |

`authority_evidence` says where the authority is stated, when that is recorded. Its `source` is `document` (the official file itself, with the `page` and the words in `quote`), `external` (another instrument, at `url`), `registry` (a registry), `manifest` (the manifest of the collection the file came from) or `reviewer` (the reviewer recorded it).

`language_rule` is the law's own rule for which version prevails where its language versions differ. When `status` is `stated`, `rule` gives it as the law states it, `prevails` names the language whose version prevails (absent when the rule names none, as when every version is equally authentic), and `provision` and `evidence` say where it is stated. `not_stated` means the reviewed copy states no such rule; `not_applicable` means the reviewer recorded that no such rule applies to this instrument; `unknown` means nothing is recorded. `evidence` takes the same form as `authority_evidence`.

`preferred_version_id` is the text to read when you give no date. It is a reading default, never a claim that its edition is authoritative. Every text from [Get a record's texts](/docs/api/get-versions) carries its `expression_id` and `expression_authority`; pass `expression_id` there to get one edition's texts.

## Cite and link

When you show a record, link the official source (`canonical_url`) and the record's page on the website, and give the citation as recorded. If you show a text, follow its rights: see [Rights and reuse](/docs/rights). If it is a translation, or its authority is `unknown`, say so beside it.
