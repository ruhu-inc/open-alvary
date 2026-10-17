---
title: Quickstart
description: Get a key, make your first request and read the answer, in about five minutes.
---

You will create an API key, search the catalogue and read one record. You need a terminal; the Python examples need Python 3 with `requests` (`pip install requests`), and the JavaScript ones Node.js 18 or later.

> Tip: Only using an AI tool such as Claude? You don't need a key: see [Connect AI tools](/docs/ai-tools).

## 1. Get an API key

1. Sign in at [open.alvary.ai/console](/console) with Google or an email code. The first time, give your name and your organisation's name. Your organisation starts on the free Starter plan.
2. Open **API keys** and choose **Create key**.

![The Create an API key form: name, type, expiry and Create key](/guide/key-create.webp)

- {1} Name it after where it will run, such as "Research laptop".
- {2} Keep **Personal** for your own scripts.
- {3} Choose when it expires.
- {4} Choose **Create key**.

3. Copy the key. It is shown only once.

![The new key, blurred, with a Copy button](/guide/key-created.webp)

> Note: On a paid or approved plan, owners and admins confirm a second factor before creating keys. The console asks when it's needed; see [Authentication](/docs/authentication#second-factor).

## 2. Keep the key in an environment variable

```bash
export OPEN_ALVARY_KEY="oa_live_..."
```

Keep keys out of source code, web pages and addresses. A key sent in the address is refused with [`credential_not_accepted`](/docs/errors#credential_not_accepted).

## 3. Search the catalogue

```curl
curl -G https://api.open.alvary.ai/api/v1/search \
  --data-urlencode "q=freedom of information" \
  -d jurisdiction=ng -d limit=5 \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

response = requests.get(
    "https://api.open.alvary.ai/api/v1/search",
    params={"q": "freedom of information", "jurisdiction": "ng", "limit": 5},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
)
response.raise_for_status()
page = response.json()
print(page["total"], "found")
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/search");
url.searchParams.append("q", "freedom of information");
url.searchParams.append("jurisdiction", "ng");
url.searchParams.append("limit", "5");
const response = await fetch(url, {
  headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
});
if (!response.ok) throw new Error((await response.json()).detail);
const page = await response.json();
console.log(page.total, "found");
```

## 4. Read the answer

```json
{
  "total": 1,
  "items": [
    {
      "id": "ng-foi-act-2011",
      "jurisdiction_id": "ng",
      "authority_id": "ng-national-assembly",
      "title": "Freedom of Information Act 2011",
      "document_type": "act",
      "canonical_url": "https://www.justice.gov.ng/freedom-of-information-portal/",
      "url_kind": "landing_page",
      "citation": "FOI Act 2011",
      "language": "en",
      "legal_status": "unknown",
      "status_evidence": null,
      "catalogue_note": "The official link leads to the Federal Ministry of Justice's page for the Act.",
      "summary": "Gives everyone a right of access to records and information held by public institutions in Nigeria.",
      "year": 2011,
      "access_mode": "metadata_link_only",
      "rights_status": "RIGHTS_UNDER_REVIEW",
      "has_full_text": false
    }
  ],
  "scope": "Catalogue metadata and cleared full text only"
}
```

| Field | What it tells you |
|---|---|
| `total` | How many records match, across all pages. |
| `id` | The record's ID. It doesn't change: store it, and use it to fetch the record again. |
| `canonical_url` | The official source. `url_kind` says whether it is the document itself or the official page for it. |
| `access_mode` | `open_full_text` when a text is cleared for reuse here; `metadata_link_only` when only the record and the official link are. |
| `rights_status` | The rights decision for the text. See [Rights and reuse](/docs/rights). |

Every field is described in the [API reference](/docs/api/search).

## 5. Get one record

```curl
curl https://api.open.alvary.ai/api/v1/sources/ng-foi-act-2011 \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
record = requests.get(
    "https://api.open.alvary.ai/api/v1/sources/ng-foi-act-2011",
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
print(record["title"], record["canonical_url"])
```

```javascript
const record = await (
  await fetch("https://api.open.alvary.ai/api/v1/sources/ng-foi-act-2011", {
    headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
  })
).json();
console.log(record.title, record.canonical_url);
```

When a record has a cleared text (`has_full_text` is true), [Get a record's texts](/docs/api/get-versions) returns it in full. See [Read a record and its text](/docs/records).

## When something goes wrong

| You get | It means | What to do |
|---|---|---|
| `401` [`credential_required`](/docs/errors#credential_required) | No key was sent. | Send `Authorization: Bearer <key>`. Check the variable is set in this terminal. |
| `401` [`invalid_credentials`](/docs/errors#invalid_credentials) | The key is unknown, revoked or expired. | Copy it again, or create a new one. |
| `403` [`entitlement_required`](/docs/errors#entitlement_required) | The organisation has no access. | Sign in to the console once; Starter starts at once. |
| `429` [`rate_limited`](/docs/errors#rate_limited) | A limit is used up for now. | Wait for `Retry-After` seconds. See [Plans and rate limits](/docs/plans). |
| `"total": 0` | Nothing matched. | Try fewer words, or leave out `jurisdiction`. |

More answers in [Help](/docs/help).

## Next steps

:::cards
- [Find records](/docs/search) Filters, pages and citations.
- [Follow changes](/docs/changes) Keep up with what is added or corrected.
- [Connect AI tools](/docs/ai-tools) Ask Claude or ChatGPT about the catalogue.
- [API reference](/docs/api) Every route and field.
:::
