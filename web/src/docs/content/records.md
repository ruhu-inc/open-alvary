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

## Cite and link

When you show a record, link the official source (`canonical_url`) and the record's page on the website, and give the citation as recorded. If you show a text, follow its rights: see [Rights and reuse](/docs/rights).
