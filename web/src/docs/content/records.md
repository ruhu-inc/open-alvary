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
| `cross_reference` | `cross_reference` | A reference between the two, shown the same way from either side. |

Links come 25 to a page (`limit` up to 100); pass `next_cursor` as `cursor` for the next, or ask for one `kind` only, such as `enables` for the instruments made under a law or `transposed_by` for the national laws that transpose a directive. Only links one step away are listed (depth 1), and only between public records that a reviewer has checked against that text, so a record may be repealed before its history shows by what. Judicial treatment, such as a judgment that follows or overrules another, is not listed yet.

## Find which bodies a country belongs to

[Get a jurisdiction](/docs/api/get-jurisdiction) tells you which regional bodies a country belonged to on a date: the African Union, ECOWAS, the EAC, OHADA and the others. For a body such as `aa-ecowas`, it lists the members instead.

```curl
curl "https://api.open.alvary.ai/api/v1/jurisdictions/sn?as_of=2026-01-01" \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

senegal = requests.get(
    "https://api.open.alvary.ai/api/v1/jurisdictions/sn",
    params={"as_of": "2026-01-01"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
if senegal["member_of"]["status"] == "unknown":
    print("No membership reviewed yet")
for body in senegal["member_of"]["items"]:
    print(body["body_name"], body["valid_from"], body["evidence_url"])
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/jurisdictions/sn");
url.searchParams.append("as_of", "2026-01-01");
const senegal = await (
  await fetch(url, { headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` } })
).json();
for (const body of senegal.member_of.items) console.log(body.body_name, body.valid_from, body.evidence_url);
```

{{example:get_jurisdiction}}

What the answer means:

| Field | Meaning |
|---|---|
| `member_of` | For a country: the bodies it belonged to on `as_of` (today if you leave it out). |
| `members` | For the African Union or a regional body: its members on `as_of`. |
| `items` | The memberships held on that date. |
| `ended` | Memberships that had ended by then, with their last day in `valid_to`. A country that left ECOWAS on 29 January 2025 has `valid_to` 28 January 2025. |
| `valid_from`, `valid_to` | The first and last day of the membership. `valid_to` is null if it has not ended. |
| `evidence_url`, `note` | The body's own source, or the treaty depositary's, that the dates were checked against, and what it says. |
| `status` | `reviewed`, or `unknown` when no membership has been reviewed. `unknown` never means "none". |

A membership is listed only after a reviewer has checked it against its evidence. One that has not been checked yet is left out, so a missing body is not proof that a country is not a member. Membership alone does not mean a body's treaties apply to a member: signature, ratification and entry into force are separate facts.

[List jurisdictions](/docs/api/list-jurisdictions) gives every country's `member_of` in the same form. Changes to memberships are in the [change feed](/docs/changes) as `membership_recorded`. To search a country's law together with the regional and treaty law that binds there, see [the law that applies in a country](/docs/retrieval#the-law-that-applies-in-a-country).

## Regional instruments and treaties

A regional body's instrument, such as an OHADA Uniform Act or an ECOWAS directive, has `regional` on its record ([Get a record](/docs/api/get-source)), on its [anchors](/docs/api/get-anchor) and on each evidence unit from it:

| Field | Meaning |
|---|---|
| `organ` | The organ that adopted it, such as the Council of Ministers. |
| `legal_effect` | `directly_applicable` (it applies in the members as it stands), `requires_transposition` (it binds the members as to its result, through national law), `binding_on_addressees` (it binds the member states it is addressed to) or `non_binding`. |
| `member_states` | The member states it binds. Empty means every member. |
| `transposition_deadline` | The date by which members must transpose it, when one is set. |

A treaty has `treaty` instead: `adopted_by`, `depositary`, `entered_into_force` and `authentic_languages`, and on the record and its anchors, `parties`. Each party gives the dates it `signed`, `ratified`, `acceded` and `denounced` the treaty, when recorded, its `reservations`, and `domesticated_by`: the acts of that state that a checked link records as domesticating the treaty. Signature alone never binds a state. An empty `parties` list means none is recorded yet, never that the treaty has none. An evidence unit carries the treaty's status without its parties.

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

`authority_evidence` says where the authority is stated, when that is recorded: the page and words of the official file, or the address of another instrument.

`language_rule` is the law's own rule for which version prevails where its language versions differ. When `status` is `stated`, `rule` gives it as the law states it, `prevails` names the language whose version prevails (absent when the rule names none, as when every version is equally authentic), and `provision` and `evidence` say where it is stated. `not_stated` means the reviewed copy states no such rule; `unknown` means nothing is recorded.

`preferred_version_id` is the text to read when you give no date. It is a reading default, never a claim that its edition is authoritative. Every text from [Get a record's texts](/docs/api/get-versions) carries its `expression_id` and `expression_authority`; pass `expression_id` there to get one edition's texts.

## Cite and link

When you show a record, link the official source (`canonical_url`) and the record's page on the website, and give the citation as recorded. If you show a text, follow its rights: see [Rights and reuse](/docs/rights). If it is a translation, or its authority is `unknown`, say so beside it.
