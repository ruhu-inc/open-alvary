---
title: Help
description: Answers to common questions, and how to reach us.
---

## Access and keys

**Do I need an account?** Not to read the website, download catalogue releases or follow the change feed. The REST API and the MCP server need an account: sign in to [the console](/console), and your organisation gets the free Starter plan at once. Releases with full text are for organisations whose access includes bulk releases, granted by agreement; they download with a key from [`/api/v1/bulk`](/docs/api/get-bulk-file).

**Is it free?** The Starter plan is free, with no card. Prices for paid plans are coming; until then, we confirm each upgrade by email. Research, government and legal aid can apply for the Public interest plan. See [Plans and rate limits](/docs/plans).

**I lost my key.** Keys are shown only once. Revoke the old one under **API keys** and create a new one.

**My key stopped working.** Check under **API keys** whether it expired or was revoked, and whether your organisation still has access (**Plan and limits**). A personal key stops when its holder leaves the organisation; use a shared key for servers.

**Can I call the API from a web page?** No. Keys must stay on your server; the API's data routes don't allow calls from other websites. Call the API from your server and send your page what it needs.

## Answers

**Why is a search empty?** Search finds your words as a phrase, in order. Try fewer words, or leave out `jurisdiction`. Records outside your plan's countries still answer, at the Starter rate. See [Find records](/docs/search).

**Why is there no text for a record?** Texts are served only once their rights are cleared and a reviewer has checked them against the official file. The record links the official source in `canonical_url`. See [Rights and reuse](/docs/rights).

**What does an error code mean?** Every refusal has a `code`, explained on the [errors page](/docs/errors).

**What is `request_id`?** Every answer carries an `X-Request-ID` header, and every refusal a `request_id`. Quote it when you write to us.

## Changes and monitors

**How do I know when a law changes?** Follow the [change feed](/docs/changes), or create a monitor in the console to be emailed or to receive [webhooks](/docs/webhooks). Changes cover what Open Alvary holds, not every change in a country's law.

**Why didn't my monitor tell me about an older change?** A monitor tells you about changes after it starts. Use **Preview recent changes** to see what it would have matched.

**My webhook endpoint isn't verified.** It must answer the challenge with `2xx` and the body `{"challenge": "<the challenge>"}` within 10 seconds, at an HTTPS address on port 443 that is reachable from the internet. Then choose **Verify** in the console. See [Webhooks](/docs/webhooks).

## Contact

- Questions, corrections and plan requests: [hello@ruhu.ai](mailto:hello@ruhu.ai). Include the record's ID or the `request_id`.
- Security issues: see the [security page](/security).
- Privacy: see the [privacy notice](/privacy).
