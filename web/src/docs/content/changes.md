---
title: Follow changes
description: Keep a copy current, subscribe to a feed, or get notified when the catalogue changes.
---

Every change to the catalogue is recorded, in the order it was made. There are three ways to follow them:

| Way | Best for | Needs |
|---|---|---|
| The [change feed API](#keep-a-copy-current) | Keeping your own copy of the catalogue current | Nothing (or a key, to use your plan's limits) |
| [Atom and RSS feeds](#feeds) | Feed readers and simple alerts | Nothing |
| [Monitors and webhooks](/docs/webhooks) | Being told about particular laws or countries, by email or at your own endpoint | An account; webhooks need a plan other than Starter |

Changes cover what Open Alvary holds, not every change in a country's law. [Change coverage](/docs/api/get-change-coverage) says when recording began and what is held for each country.

## What a change says

| `event_type` | When |
|---|---|
| `catalogue_added` | A record became public: an existing law newly catalogued (not necessarily a new law), or one restored after a withdrawal. |
| `metadata_corrected` | A record's catalogue details were corrected; `changed_fields` lists which. A correction never changes legal text. |
| `rights_changed` | A record's [rights record](/docs/rights) has a new revision: its status, licence, attribution, conditions, expiry or any other field changed. `changed_fields` lists the rights record's fields that changed, `rights_revision` is the new revision, and `rights` gives it with the `decision` taken from it (whether the record and its text may be shown, and why). Refresh the licence, attribution and conditions you show from [Get a record's rights](/docs/api/get-rights). A change of rights status is also a `metadata_corrected` with `rights_status`. A record the change takes out of public view has a `withdrawal` instead. |
| `text_version_published` | A text was cleared and published; `change_reason` says whether it is the first text, a revision at the source, or a correction. A text that replaces another lists in `version_diff` the provisions it adds, removes and changes. |
| `legal_effect_recorded` | A reviewer checked, against the official text, that another public record repeals, amends, supersedes or brings into force this one, or that a law transposes, domesticates or ratifies a treaty or directive. `legal_effect` names the record that had the effect (`by`) and the date it took effect, when known. A transposition, domestication or ratification changes the law on both sides, so each record has its own change; the law's names the instrument it gives effect to (`of`). It may describe a past effect recorded late. If the link is withdrawn, the same change is corrected with `review_state` `withdrawn`, still naming the link. |
| `treatment_recorded` | A legally qualified reviewer recorded how a paragraph of a judgment treats another judgment: `follows`, `applies`, `distinguishes`, `doubts` or `overrules`. `treatment` names the judgment treated (`of`), the treating paragraph (`citing_pinpoint`), the part treated and the `opinion` the paragraph is in; `assertion` is `direct_treatment`, a reviewer's reading, never an inferred risk. An overruling also has a change on the overruled judgment, naming the overruling one in `by`. The passage itself is on the judgment's [history](/docs/records#case-law-citations-and-treatment). If the treatment is withdrawn, the same changes are corrected with `review_state` `withdrawn`. No change means no treatment was recorded, not that a judgment is good law. |
| `withdrawal` | A record or a text stopped being public. |
| `coverage_changed` | A country's coverage changed, for example its first record. |
| `membership_recorded` | A reviewer checked a country's membership of the African Union or a regional body against its evidence. `membership` gives the member, the body, the first and last day (each with its bounds when known only to a month, a year or a window), its `basis` and the evidence. `change_reason` is `ended` when the membership ended, and `corrected` when we corrected it; `retracted` is true when it is no longer a checked fact. |
| `suspension_recorded` | A reviewer checked that a member was suspended from taking part in a body's activities. `suspension` gives the member, the body, the first day, the legal basis (each article with its `role`, `decision_basis` or `treaty_rule`) and the decision. The country is still a member: no `membership_recorded` change follows. `change_reason` is `lifted` when the suspension was lifted, with its last day, and `corrected` when we corrected it; `retracted` is true when it is no longer a checked fact. |

Only links that change what law applies record a `legal_effect_recorded` change. A record's other checked links, which cite or refer to another record (`cites`, `cross_reference`), implement an instrument (`implements`) or name the law it was made under (`made_under`), are in its [history](/docs/api/get-relationships) but record no change.

Each change has a plain `summary` sentence, the `work` it concerns (ID, title, citation, and its links on the website and in the API) and, for a text, the `version`. A correction of a record's legal status, rights status, type or year gives the old value in `previous`. Every field is listed in the [reference](/docs/api/list-changes).

`committed_at` is when the change was recorded. `observed_at` is when we saw at the official source what the change announces: for `text_version_published`, when the official file the text was read from was retrieved. It is null for every other kind of change, which is our own (cataloguing, a review, a correction, a rights decision or a withdrawal). `publisher_published_at` is null on every change for now: the catalogue keeps no publication date for a record or a text, and we don't guess one.

Each change carries `schema_version` (`change-event-1`). New kinds of change and new fields can appear within a version without notice: skip an `event_type` you don't know, and ignore fields you don't know, rather than refusing the page. A parser that rejects them stops at the first new kind of change. Existing fields keep their names, types and meaning within a version. [Change coverage](/docs/api/get-change-coverage) lists the kinds recorded.

{{example:list_changes}}

If we correct a change itself, for example the date of a legal effect, or withdraw a legal effect we recorded, it keeps its `event_id`, its `event_revision` goes up, `change_reason` is `corrected`, and `supersedes` names the revision it replaces. So store changes by **(`feed_epoch`, `event_id`, `event_revision`)**, keep the highest revision of each change, and never apply the same one twice.

A `metadata_corrected` is different: it records a correction of the record, not of an earlier change. Each correction is a new change with its own `event_id`, and an earlier one stays true (it says what was corrected then, with its `previous` values), so apply every one.

## Keep a copy current

The feed is public: these examples use `https://open.alvary.ai/api/changes` without a key. With a key, `https://api.open.alvary.ai/api/v1/changes` answers the same changes, within your plan's limits, and names more of the records withdrawn ([see below](#when-a-text-is-published-or-withdrawn)).

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

### When a text is published or withdrawn

**A text is published** (`text_version_published`): the change names the `version` (its `id`, `content_hash` and `language`) and the `rights_revision` in force. A later change to the record's rights record arrives as `rights_changed`, with the new revision. With a key:

1. Get the text from `https://api.open.alvary.ai/api/v1/versions/{id}/text` and check that its SHA-256 equals `content_hash`.
2. Get its parts, a page at a time, from `/api/v1/versions/{id}/structure`. Offsets are UTF-8 bytes into that exact text. Each part's `id` is an anchor: `/api/v1/anchors/{id}` gives its words, a citation and the attribution to show.
3. Keep the `rights_revision` with your copy. `/api/v1/rights/{source_id}/history` lists every revision of the record's rights record.

A later text of the same record names the one it replaces in `prior_version_id`, and `change_reason` says why: `extraction_correction` (read again from the same official file) or `source_revision` (a new official file). The same provision keeps the same `locator` across versions.

**To check a text you hold**, send its `ETag` back as `If-None-Match`. `304` means it is unchanged and still public. `404` means it is no longer public. A `304` costs a request but no bytes.

**A record or a text is withdrawn** (`withdrawal`):

| `scope_disclosure` | `action` | What to do |
|---|---|---|
| `named`, with `version` | `stop_serving` | Stop serving the text in `version` now. `reason_code` says why: `rights_changed` (its rights record changed or lapsed) or `correction` (its approval was withdrawn). A corrected text, if there is one, arrives as a new `text_version_published`. |
| `named`, `version` null | `stop_serving` | With a key only: the record in `work` is no longer public because its rights record changed (`reason_code` `rights_changed`). Stop serving it and every text of it now. |
| `revalidate_releases` | `revalidate` | A record stopped being public, and which one is not said. Check each record you hold: one that answers `404` or `410` is no longer public; stop serving it. |

Whether a withdrawal names what it withdraws depends on why it was withdrawn:

- **A text withdrawn from a record that stays public** is named to everyone, with its `version`.
- **With a key** (`https://api.open.alvary.ai/api/v1/changes`), a record that was public and was withdrawn because its rights record changed or lapsed is named too, by its IDs only: `work` has its `id` and `jurisdiction_id`, and every other field of `work` is null (no title, citation or links). A text withdrawn from it earlier keeps its `version.id`, with `content_hash` and `language` null. Reading the record or its texts with a key then answers `410` [`withdrawn`](/docs/errors#withdrawn), and a check of a part of it answers `withdrawn`.
- **Anything else is `revalidate_releases`** and names nothing: every withdrawal of a record to a caller without a key; a record withdrawn for a confidential reason, such as privacy or a legal or court order, whatever the caller; a record withdrawn before Open Alvary began recording why; and a text withdrawn earlier from a record that is now withdrawn for one of those reasons. Open Alvary can't say which texts these concern, so check every record you hold. Reads answer `404` [`unavailable`](/docs/errors#unavailable), the same as for a record that was never public or never existed.

When a rights review expires, its withdrawal is in the feed within minutes. Nothing already sent can be recalled, so poll at least daily if you serve texts to others.

### Filters

Narrow the feed with any of these; repeat a parameter for several values. Values within one filter are alternatives; different filters must all match.

| Parameter | Matches |
|---|---|
| `jurisdiction` | Countries or other jurisdictions, such as `ng`. A country includes its states and local law. A body, such as `aa-ecowas`, includes changes to who its members are and to their suspensions. |
| `work` | Particular records, by ID. |
| `type` | Kinds of change, such as `withdrawal`. |
| `class` | Document types, such as `act`. |
| `language` | The language of the affected text. |
| `q` | Whole words in the title, citation or summary. |

A withdrawal of a whole record, named or not, matches every filter except `type`, so a filtered feed never misses one. A withdrawal that shows no title is never selected by `q` on words it no longer shows: it matches every `q`.

A resume token belongs to its filters: keep one token per set of filters.

### When the feed answers an error

| Answer | What to do |
|---|---|
| `400` [`invalid_cursor`](/docs/errors#invalid_cursor) | The cursor or resume token isn't valid, or was issued for other filters or another page size. Send it with the filters and `limit` it was issued for, or continue from your last resume token. |
| `400` [`invalid_filter`](/docs/errors#invalid_filter) | A filter isn't valid: an unknown change type, too many values, or a keyword over 200 characters. Correct it. |
| `409` [`stale_cursor`](/docs/errors#stale_cursor) | The cursor is older than 15 minutes. Continue from your last resume token. |
| `409` [`feed_reset`](/docs/errors#feed_reset) | The feed was reset. Take a new `start=now` token, reload from a current release, then continue from the new token. |
| `410` [`replay_window_expired`](/docs/errors#replay_window_expired) | Your token is older than the changes still kept. Reload from a current release with a new token. |
| `429` [`rate_limited`](/docs/errors#rate_limited) or `503` with `Retry-After` | Try again after that many seconds. |

## Feeds

`https://open.alvary.ai/api/changes/feed.atom` and `https://open.alvary.ai/api/changes/feed.rss` give the latest 50 changes of the last 90 days, newest first, with the same filters. For example, Nigeria's withdrawals:

```text
https://open.alvary.ai/api/changes/feed.atom?jurisdiction=ng&type=withdrawal
```

Feeds are for reading, not for keeping a copy: use the feed API above for that. Every record's page on the website also links its own feed.

## From an AI tool

Connected AI tools read the same changes with the `list_changes` and `get_change` [tools](/docs/api/mcp#list_changes). Ask, for example, "What changed in Nigeria's catalogue this month?"
