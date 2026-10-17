---
title: Pagination
description: Page through records with limit and offset, and through changes with cursors.
---

Long answers come a page at a time. Records use **offsets**; the change feed uses **cursors**, so that a page never skips or repeats a change.

## Records: limit and offset

[Search](/docs/api/search) and [List records](/docs/api/list-sources) take `limit` (1 to 100) and `offset` (where to start), and answer `total`, the number of matching records across all pages. Ask for the next page with `offset` plus `limit` until you have `total`:

```python
import os

import requests


def every_record(jurisdiction):
    offset = 0
    while True:
        page = requests.get(
            "https://api.open.alvary.ai/api/v1/sources",
            params={"jurisdiction": jurisdiction, "limit": 100, "offset": offset},
            headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
            timeout=30,
        ).json()
        yield from page["items"]
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            return
```

```javascript
async function* everyRecord(jurisdiction) {
  for (let offset = 0; ; ) {
    const url = new URL("https://api.open.alvary.ai/api/v1/sources");
    url.searchParams.append("jurisdiction", jurisdiction);
    url.searchParams.append("limit", "100");
    url.searchParams.append("offset", String(offset));
    const page = await (
      await fetch(url, { headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` } })
    ).json();
    yield* page.items;
    offset += page.items.length;
    if (page.items.length === 0 || offset >= page.total) return;
  }
}
```

If the catalogue changes while you page, a record can move between pages. For a complete, consistent copy, start from a [data release](/docs/api/list-releases) and follow the [change feed](/docs/changes).

## Changes: cursors and resume tokens

[List changes](/docs/api/list-changes) answers `events`, `has_more`, `next_cursor` and `resume_token`:

- While `has_more` is true, ask again with `cursor` set to `next_cursor`, within 15 minutes. A page may be short, or even empty, and still have more.
- When `has_more` is false, you are up to date. Keep the `resume_token`, and send it next time as `resume_token` to get only newer changes.

```python
import os

import requests


def new_changes(resume_token=None):
    """Every change since resume_token (or since the start), and the token for next time."""
    params, events = {"limit": 100}, []
    if resume_token:
        params["resume_token"] = resume_token
    while True:
        page = requests.get(
            "https://api.open.alvary.ai/api/v1/changes",
            params=params,
            headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
            timeout=30,
        ).json()
        events += page["events"]
        if not page["has_more"]:
            return events, page["resume_token"]
        params = {"limit": 100, "cursor": page["next_cursor"]}
```

Send only one of `cursor`, `resume_token` and `start` in a request. A cursor is tied to the filters and page size it was issued for. [Follow changes](/docs/changes) explains what to do when a cursor expires or the feed is reset.
