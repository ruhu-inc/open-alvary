---
title: Follow changes
description: Keep a copy current, subscribe to a feed, or get notified when the catalogue changes.
---

Every change to the catalogue is recorded, in the order it was made. There are three ways to follow them:

| Way | Best for | Needs |
|---|---|---|
| The [change feed API](#keep-a-copy-current) | Keeping your own copy of the catalogue current | Nothing (or a key, to use your plan's limits) |
| [Atom and RSS feeds](#feeds) | Feed readers and simple alerts | Nothing |
| [Monitors and webhooks](/docs/webhooks) | Being told about particular laws or countries, by email or at your own endpoint | An account; webhooks need a paid plan |

Changes cover what Open Alvary holds, not every change in a country's law. [Change coverage](/docs/api/get-change-coverage) says when recording began and what is held for each country.

## What a change says

| `event_type` | When |
|---|---|
| `catalogue_added` | A record became public: an existing law newly catalogued (not necessarily a new law), or one restored after a withdrawal. |
| `metadata_corrected` | A record's catalogue details were corrected; `changed_fields` lists which. A correction never changes legal text. |
| `text_version_published` | A text was cleared and published; `change_reason` says whether it is the first text, a revision at the source, or a correction. |
| `withdrawal` | A record or a text stopped being public. |
| `coverage_changed` | A country's coverage changed, for example its first record. |

Each change has a plain `summary` sentence, the `work` it concerns (ID, title, citation, and its links on the website and in the API) and, for a text, the `version`. Every field is listed in the [reference](/docs/api/list-changes).

{{example:list_changes}}

If we correct a change itself, it keeps its `event_id`, its `event_revision` goes up, and `supersedes` names the revision it replaces. So store changes by **(`feed_epoch`, `event_id`, `event_revision`)**, and never apply the same one twice.

## Keep a copy current

The feed is public: these examples use `https://open.alvary.ai/api/changes` without a key. With a key, `https://api.open.alvary.ai/api/v1/changes` answers the same, within your plan's limits.

1. **Note where the feed is now**, before you copy anything: `start=now` answers a resume token at the latest change.
2. **Load the catalogue**: a [data release](/docs/api/list-releases), or [every record](/docs/pagination).
3. **Apply every change since the token**, then keep the new token. Do this as often as you like, for example every hour.

```python
import json
import pathlib

import requests

FEED = "https://open.alvary.ai/api/changes"
TOKEN = pathlib.Path("resume_token.txt")


def changes(**params):
    response = requests.get(FEED, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


if not TOKEN.exists():  # first run: remember where the feed is, then load a release
    TOKEN.write_text(changes(start="now")["resume_token"])

page = changes(resume_token=TOKEN.read_text(), limit=100)
while True:
    for event in page["events"]:
        print(event["event_type"], event["summary"])  # apply it to your copy here
    if not page["has_more"]:
        break
    page = changes(cursor=page["next_cursor"], limit=100)
TOKEN.write_text(page["resume_token"])  # only after the changes are applied
```

```javascript
import { existsSync, readFileSync, writeFileSync } from "node:fs";

const FEED = "https://open.alvary.ai/api/changes";

async function changes(params) {
  const url = new URL(FEED);
  for (const [name, value] of Object.entries(params)) url.searchParams.append(name, value);
  const response = await fetch(url);
  if (!response.ok) throw Object.assign(new Error((await response.json()).detail), { status: response.status });
  return response.json();
}

if (!existsSync("resume_token.txt")) {
  // First run: remember where the feed is, then load a release.
  writeFileSync("resume_token.txt", (await changes({ start: "now" })).resume_token);
}
let page = await changes({ resume_token: readFileSync("resume_token.txt", "utf8"), limit: 100 });
for (;;) {
  for (const event of page.events) console.log(event.event_type, event.summary); // apply it here
  if (!page.has_more) break;
  page = await changes({ cursor: page.next_cursor, limit: 100 });
}
writeFileSync("resume_token.txt", page.resume_token); // only after the changes are applied
```

### Filters

Narrow the feed with any of these; repeat a parameter for several values. Values within one filter are alternatives; different filters must all match.

| Parameter | Matches |
|---|---|
| `jurisdiction` | Countries or other jurisdictions, such as `ng`. A country includes its states and local law. |
| `work` | Particular records, by ID. |
| `type` | Kinds of change, such as `withdrawal`. |
| `class` | Document types, such as `act`. |
| `language` | The language of the affected text. |
| `q` | Whole words in the title, citation or summary. |

A resume token belongs to its filters: keep one token per set of filters.

### When the feed answers an error

| Answer | What to do |
|---|---|
| `409` [`stale_cursor`](/docs/errors#stale_cursor) | The cursor is older than 15 minutes. Continue from your last resume token. |
| `409` [`feed_reset`](/docs/errors#feed_reset) | The feed was reset. Take a new `start=now` token, reload from a current release, then continue from the new token. |
| `410` [`replay_window_expired`](/docs/errors#replay_window_expired) | Your token is older than the changes still kept. Reload from a current release with a new token. |
| `503` with `Retry-After` | Try again after that many seconds. |

## Feeds

`https://open.alvary.ai/api/changes/feed.atom` and `https://open.alvary.ai/api/changes/feed.rss` give the latest 50 changes of the last 90 days, newest first, with the same filters. For example, Nigeria's withdrawals:

```text
https://open.alvary.ai/api/changes/feed.atom?jurisdiction=ng&type=withdrawal
```

Feeds are for reading, not for keeping a copy: use the feed API above for that. Every record's page on the website also links its own feed.

## From an AI tool

Connected AI tools read the same changes with the `list_changes` and `get_change` [tools](/docs/api/mcp#list_changes). Ask, for example, "What changed in Nigeria's catalogue this month?"
