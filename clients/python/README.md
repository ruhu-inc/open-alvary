# Open Alvary reference client (Python)

Reference code for the client contract RSV-09: how an application that caches Open Alvary evidence
keeps that cache honest. It is not an official SDK and is not published as a package. Whether to
publish one, who owns it and which versions it supports is a separate decision (DEV-03). Until then,
read it, copy it, or run your own client against its conformance scenarios.

It needs Python 3.12 and httpx.

## Use

```python
import os

from open_alvary_client import API, Client, SQLiteCache

client = Client(API, os.environ["OPEN_ALVARY_KEY"], cache=SQLiteCache("open-alvary-cache.sqlite"))

answer = client.retrieve("when must a data controller report a breach", jurisdiction="ng")
for cited in answer.units:
    print(cited.unit["citation"], cited.label, cited.unit["currentness"]["state"])

if not answer.checked:
    client.check(answer.units)  # before you show an answer that came from the cache
```

`retrieve` takes the parameters of [Retrieve evidence](https://open.alvary.ai/docs/api/retrieve-evidence)
as keywords: `jurisdiction`, `language`, `type`, `status`, `as_of`, `year_from`, `year_to`,
`source_id`, `version_id`, `max_units`, `max_evidence_bytes` and `include`. It returns an `Answer`:
the pack (`answer.pack`), whether it came from the cache (`from_cache`), whether Open Alvary assessed
its currentness in this call (`checked`), whether Open Alvary was out of reach (`offline`), and each
cited unit with its label (`checked` or `currentness_not_checked`) and the time it was cached.

`check(items)` is [Check cited parts](https://open.alvary.ai/docs/api/check-currentness) for any
number of units, 100 at a time. `sync_changes()` reads the change feed; `retrieve` also reads it
first whenever the last read is more than 15 minutes old, so a scheduled sync is optional.

The cache is `MemoryCache` (the default) or `SQLiteCache(path)`, which keeps the feed position
across restarts. Another store implements four methods of `Cache`: `get`, `put`, `delete` and
`scan`. Keep the cache for your own users; never build a search index from it. Use the
[releases](https://open.alvary.ai/docs/api/list-releases) for a full copy.

`save_answer(key, body, answer)` keeps an answer your application wrote from `answer`, and
`saved_answer(key)` returns it until a withdrawal purges it with the evidence it cites.

## What it does

| When | The client |
|---|---|
| It has no feed position yet | Takes one (`start=now`) before caching anything |
| The same request comes again, nothing is flagged and the feed was read in the last 15 minutes | Answers from the cache with no request; every unit is labelled `currentness_not_checked` |
| `withdrawal` naming a text | Purges every cached unit, pack and saved answer citing it, at once, and never serves it from the cache again |
| `text_version_published` (not a first text) | Flags that expression's units; before reuse it runs the check, and fetches the pack again (with its ETag) if a newer version exists |
| `legal_effect_recorded`, a correction of one, or a corrected legal status | Flags that work's currentness; before reuse it runs the check and keeps the currentness it gives |
| A feed reset: `409 feed_reset`, `410 replay_window_expired` or a token the server cannot open | Flags every cached unit for the check before reuse, and takes the feed again from `start=now` |
| `409 stale_cursor`, a new `feed_epoch` on a page, or a withdrawal that names no record | Flags every cached unit for the check before reuse, and continues from its last resume token |
| Open Alvary is out of reach (network error, timeout or 5xx) | Answers from the cache only, every unit labelled `currentness_not_checked` with the time it was cached, never as checked; a check is never answered from the cache |
| Seven days pass without a successful feed read | Stops answering from the cache (`CacheExpired`) |
| A request fails or answers 429 or 5xx | Sends it at most three times, never sooner than `Retry-After` says; a wait of more than a minute ends the attempts |

It logs only the method, path and status of a request. It never logs query text or legal text, and
removes the query string from httpx's own request log lines.

## Conformance scenarios

`clients/conformance/scenarios/` holds one JSON file per rule: what Open Alvary answers to each
request, step by step, and what the client must do. They check a fresh answer and its cached reuse;
the purge on a withdrawal; the check after a later text; currentness after a legal effect and its
correction; the check after a feed reset, an expired replay window and a stale cursor; labelled
answers when Open Alvary is out of reach; the seven-day limit; `Retry-After`; and never more than
three attempts. Throughout, no log record may carry a query or legal text. The answers they share,
in `bodies.json`, are valid public documents.

The files are plain JSON, for a client in any language. A Python client runs them with
`run(scenario, make_client)` from `clients/conformance/runner.py`; its docstring describes the
format and what the client must expose. This client passes every one, with either cache:

```sh
python -m pytest -q tests/test_reference_client.py
```
