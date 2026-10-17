---
title: Monitors and webhooks
description: Get your monitors' matches at your own endpoint, signed, as they happen.
---

A **monitor** watches the catalogue for the changes you choose: countries, records and kinds of change. Its matches arrive in your inbox in the console, and by email if you like. A **webhook** also sends each match to your own HTTPS endpoint as it happens, signed so you can check it came from Open Alvary.

| Plan | Monitors | Email | Webhook endpoints |
|---|---|---|---|
| Starter | 3 | Weekly digest | None |
| Developer | 20 | Daily or weekly | 1 |
| Team | 50, shared with the organisation | Daily or weekly | 3 |

Other plans are in [Plans and rate limits](/docs/plans).

## Set up a monitor

In the console, open **Monitor** and choose **New monitor**: give it a name, choose countries and kinds of change, and choose the email. The [console guide](/docs/console#monitor) shows each step. A monitor tells you about changes after it starts; **Preview recent changes** shows what it would have matched recently.

You can also start from a record's page on the website: **Monitor this record**.

## Add a webhook endpoint

1. Create a monitor first.
2. In the console, open **Webhooks** and choose **Add endpoint** ([pictures](/docs/console#webhooks)). Give your endpoint's address and choose the monitors it receives.
3. Copy the **signing secret** (`whsec_…`). It is shown only once; keep it with your endpoint's settings.
4. We send your endpoint a **verification challenge** at once. Your endpoint answers it (below), and the endpoint becomes active. If it wasn't ready, choose **Verify** in the console to try again (up to 5 times an hour).

Your endpoint must be HTTPS on port 443 and reachable from the internet. We don't follow redirects, and we don't send to private or local addresses.

## Receive deliveries

Every delivery is a `POST` with a JSON body and these headers:

| Header | Value |
|---|---|
| `Open-Alvary-Delivery` | The delivery's ID. It stays the same when we retry. |
| `Open-Alvary-Timestamp` | When it was signed, in Unix seconds. |
| `Open-Alvary-Key` | The ID of the signing key. |
| `Open-Alvary-Signature` | `v1=` and the hex HMAC-SHA256, keyed with your secret, of the timestamp, a full stop, and the body as sent. During a rotation there are two, separated by a comma. |

The body's `type` is one of:

| `type` | Body | Answer |
|---|---|---|
| `endpoint.verification` | `challenge` | `2xx` with the body `{"challenge": "<the same challenge>"}`, within 10 seconds |
| `endpoint.test` | `endpoint_id` | Any `2xx` (sent by **Send test** in the console) |
| `change` | `watch_ids` (the monitors that matched) and `event`, as in the [change feed](/docs/changes#what-a-change-says) | Any `2xx` |

Every body also has `payload_version` (1), `delivery_id` and `attempted_at`. A change too large to send in full (over 64 KB) comes with `truncated: true` and the event's details without its long fields; fetch the rest from the record's `api_url`.

To receive them:

1. Read the body as raw bytes, **before** parsing it.
2. Check the timestamp is within 5 minutes, and that one of the signatures matches, comparing in constant time.
3. Answer the challenge, or handle the change.
4. Skip a `delivery_id` you have already handled, and store changes by (`feed_epoch`, `event_id`, `event_revision`).

```python
# pip install flask
import hashlib
import hmac
import json
import os
import time

from flask import Flask, request

app = Flask(__name__)
SECRET = os.environ["OPEN_ALVARY_WEBHOOK_SECRET"]  # whsec_...


def verified(body: bytes, headers) -> bool:
    stamp = headers.get("Open-Alvary-Timestamp", "")
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > 300:
        return False
    expected = hmac.new(SECRET.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(
        hmac.compare_digest(expected, part.strip().removeprefix("v1="))
        for part in headers.get("Open-Alvary-Signature", "").split(",")
    )


@app.post("/open-alvary")
def receive():
    body = request.get_data()
    if not verified(body, request.headers):
        return "", 401
    message = json.loads(body)
    if message["type"] == "endpoint.verification":
        return {"challenge": message["challenge"]}
    if message["type"] == "change":
        print(message["event"]["summary"])  # handle the change here
    return "", 204
```

```javascript
// node receive.mjs (Node.js 18 or later; no packages needed)
import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";

const SECRET = process.env.OPEN_ALVARY_WEBHOOK_SECRET; // whsec_...

function verified(body, headers) {
  const stamp = headers["open-alvary-timestamp"] ?? "";
  if (!/^\d+$/.test(stamp) || Math.abs(Date.now() / 1000 - Number(stamp)) > 300) return false;
  const expected = Buffer.from(createHmac("sha256", SECRET).update(`${stamp}.`).update(body).digest("hex"));
  return (headers["open-alvary-signature"] ?? "").split(",").some((part) => {
    const given = Buffer.from(part.trim().replace(/^v1=/, ""));
    return given.length === expected.length && timingSafeEqual(given, expected);
  });
}

createServer((request, response) => {
  const chunks = [];
  request.on("data", (chunk) => chunks.push(chunk));
  request.on("end", () => {
    const body = Buffer.concat(chunks);
    if (request.method !== "POST" || !verified(body, request.headers)) return response.writeHead(401).end();
    const message = JSON.parse(body);
    if (message.type === "endpoint.verification") {
      response.writeHead(200, { "Content-Type": "application/json" });
      return response.end(JSON.stringify({ challenge: message.challenge }));
    }
    if (message.type === "change") console.log(message.event.summary); // handle the change here
    response.writeHead(204).end();
  });
}).listen(8080);
```

Put the receiver behind HTTPS (your host's proxy or load balancer), at the address you gave in the console.

## Retries and failures

| Your endpoint answers | We |
|---|---|
| `2xx` | Count it delivered. |
| `410` | Turn the endpoint off and tell the owner. |
| `429` | Retry after its `Retry-After`. |
| Any other `4xx` | Stop that delivery, and show the error under **Deliveries**. |
| `3xx`, `5xx`, nothing within 10 seconds, or a network error | Retry after 1 minute, 5 minutes, 30 minutes, 2 hours and 8 hours, then give up after 24 hours. |

Each retry is signed afresh, with the same delivery ID. After 20 failed deliveries in a row, or 24 hours of failures, the endpoint is marked failing and then paused; resume it in the console, which verifies it again. **Deliveries** shows each attempt for 7 days.

## Rotate the secret

**Rotate secret** gives a new secret. For 24 hours we sign with both, so `Open-Alvary-Signature` has two values: deploy the new secret within that time. The code above accepts either.
