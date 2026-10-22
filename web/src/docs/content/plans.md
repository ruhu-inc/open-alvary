---
title: Plans and rate limits
description: What each plan allows, how limits are counted, and what to do when you reach one.
---

Every plan reads the same published corpus. Plans differ in their limits, which are shared by every key and connected app in your organisation, across the REST API and MCP.

{{plans}}

Prices for paid plans are coming. Until then, request a plan in the console under **Plan and limits**, and we confirm it by email ([pictures](/docs/console#plan)). Research, government and legal aid can apply for the Public interest plan. For anything else, write to [hello@ruhu.ai](mailto:hello@ruhu.ai).

The current plans are also published, without a key, at [`/api/v1/tiers`](/docs/api/list-plans).

## How limits are counted

- **Requests a minute** is a steady rate, and **burst** is how many requests you can make at once after a pause. The organisation's allowance refills continuously (a token bucket).
- **At once** is how many requests can be in progress together. One key or app can use at most half of them, so one busy script can't block the others.
- **Data a day** is how many bytes of answers your organisation can receive. It holds one day's worth and refills evenly over 24 hours, so it never resets all at once at midnight. Each answer's body counts as sent, before compression. An answer that would go over it is refused, and `Retry-After` says when enough has refilled for it.
- **Countries.** On a plan that names countries, requests about those countries, with their states and local law, run at the plan's limits. The law of the African Union and the regional bodies (IDs starting `aa-`, such as `aa-ecowas`) is within every plan. Every other country still answers, at the Starter rate (60 a minute, burst 30). A search or list without `jurisdiction` covers your plan's countries.
- The change feed isn't narrowed by your plan's countries.

You can see today's requests and data, by key and app, under **Usage** in the console.

## Pace yourself

Every answer counted against your organisation's requests a minute says how much of the allowance is left:

```http
RateLimit-Policy: "organisation";q=100;w=10
RateLimit: "organisation";r=87;t=2
```

`RateLimit-Policy` gives your plan's burst (`q`, in requests) and how many seconds it takes to refill from empty (`w`). `RateLimit` gives the requests left now (`r`) and the seconds until the burst is full again (`t`). When `r` gets low, slow down rather than wait for a `429`. The headers count requests only: requests at once, data a day and the Starter rate outside your plan's countries aren't in them. They follow the IETF draft [RateLimit header fields for HTTP](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/).

## When you reach a limit

The request is refused with `429` and the code [`rate_limited`](/docs/errors#rate_limited). The `Retry-After` header, and `retry_after_seconds` in the body, say how many seconds to wait:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 12
Content-Type: application/problem+json
```

Wait that long, then try again. A `503` with `Retry-After` ([`temporarily_unavailable`](/docs/errors#temporarily_unavailable) or [`capacity_exhausted`](/docs/errors#capacity_exhausted)) is handled the same way. For example:

```python
import os
import time

import requests


def get(url, **params):
    for attempt in range(5):
        response = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
            timeout=30,
        )
        if response.status_code in (429, 503):
            time.sleep(int(response.headers.get("Retry-After", 2 ** attempt)))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("still limited after 5 attempts")
```

```javascript
async function get(url, params = {}) {
  const address = new URL(url);
  for (const [name, value] of Object.entries(params)) address.searchParams.append(name, value);
  for (let attempt = 0; attempt < 5; attempt++) {
    const response = await fetch(address, {
      headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },
    });
    if (response.status === 429 || response.status === 503) {
      const wait = Number(response.headers.get("Retry-After") ?? 2 ** attempt);
      await new Promise((resolve) => setTimeout(resolve, wait * 1000));
      continue;
    }
    if (!response.ok) throw new Error((await response.json()).detail);
    return response.json();
  }
  throw new Error("still limited after 5 attempts");
}
```

To stay within your limits, keep a local copy of what you need and refresh it from the [change feed](/docs/changes), rather than asking for the same records again. The [acceptable use policy](/acceptable-use) lets you keep and index copies, and asks you to apply withdrawals and corrections within 7 days.
