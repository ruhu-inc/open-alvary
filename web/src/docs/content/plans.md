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
- **Countries.** On a plan that names countries, requests about those countries run at the plan's limits. Every other country still answers, at the Starter rate (60 a minute, burst 30). A search or list without `jurisdiction` covers your plan's countries.
- The change feed isn't narrowed by your plan's countries.

You can see today's requests, by key and app, under **Usage** in the console.

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

To stay within your limits, keep a local copy of what you need and refresh it from the [change feed](/docs/changes), rather than asking for the same records again.
