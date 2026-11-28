---
title: Connect AI tools
description: Connect Claude, Claude Code, Codex or Antigravity to the Open Alvary catalogue over MCP, with no API key.
---

AI tools that speak MCP (the Model Context Protocol) can search and read the published Open Alvary catalogue for you. You add Open Alvary to the tool once, sign in in your browser, and approve it for your organisation; after that, ask questions in your own words and the tool calls Open Alvary when it needs to.

For a plain-English setup guide, see [Connect AI tools](/connect).

| | |
|---|---|
| Address | `https://api.open.alvary.ai/mcp` |
| Sign-in | In your browser via OAuth 2.1 + PKCE when the tool asks; no API key needed for supported tools |
| Protocol | Stateless HTTP `POST` JSON-RPC 2.0 supporting MCP `2026-07-28` and `2025-06-18` (`2025-03-26` compatible) |
| What the tool can do | Read the published catalogue, cleared full texts and changes, within your organisation's plan |
| What it can't do | Change anything, see your API keys, or act for another organisation |

You need an Open Alvary account with active access (the free Starter plan is enough) and the role of owner, admin or member in the organisation. Create your free Starter account in the [console](/console) before connecting an AI tool.

## Supported AI tools

{{ai-hosts}}

![Connect an app, Claude tab: the steps and the MCP address with a Copy button](/guide/connect-claude.webp)

For agent workflows in Claude Code, Codex and Antigravity, the public plugin and skills repository at [github.com/ruhu-inc/open-alvary-skills](https://github.com/ruhu-inc/open-alvary-skills) bundles the MCP configuration and guided legal-research skills.

## Approve the tool

The tool opens the Open Alvary approval page. Check the badge: **Verified publisher** means Open Alvary has checked who makes the app. Be careful with an **Unverified app** you didn't start yourself.

![The approval page: Claude wants to connect, a Verified publisher badge, the organisation and Approve](/guide/consent.webp)

- {1} Choose the organisation the tool acts for.
- {2} **Approve**.

On a paid or approved plan, owners and admins also confirm a second factor here.

## Protocol support (MCP 2026-07-28 and 2025-06-18)

`https://api.open.alvary.ai/mcp` is a stateless Streamable HTTP endpoint that accepts `POST` only (`GET` and `DELETE` return `405 Method Not Allowed` with `Allow: POST`).

- **MCP `2026-07-28`**: Supports unauthenticated `server/discover` for capability discovery, requires matching `MCP-Protocol-Version: 2026-07-28`, `Mcp-Method` and (on `tools/call`) `Mcp-Name` headers plus `params._meta`, and returns `resultType: "complete"` with serialized JSON in `content[0].text` and typed JSON in `structuredContent`.
- **MCP `2025-06-18` (and `2025-03-26` compatibility)**: Negotiates with `initialize` and returns the JSON payload in `content[0].text`.

For request headers, error codes, `server/discover` and all thirteen tool definitions, see [MCP tools](/docs/api/mcp).

## Plan jurisdictions and notices

Starter reads the law of one jurisdiction — Nigeria to begin with, together with applicable regional African bodies; an owner can change it in the console under **Plan and limits** (`/console/access`).

- **Explicit jurisdiction outside Starter**: Asking for a jurisdiction not included in your Starter plan returns a tool error (`isError: true`) with `code: "jurisdiction_not_included"`, `retryable: false`, and `manage_url` pointing to **Plan and limits**.
- **Broad searches on a scoped plan**: Searches that do not name a jurisdiction return results for your organisation's included jurisdiction and attach a notice in `notices` (or `coverage.notices`) stating which jurisdiction was searched.
- **Paid plans (Pro and Team)**: Requests for jurisdictions outside your included set still answer at the Starter rate; once that allowance is used up, they return `rate_limited` with `retry_after_seconds`.

## Rate limits and pacing

Connected AI tools share your organisation's limits (requests a minute, concurrent requests and data a day) with its API keys; see [Plans and rate limits](/docs/plans).

- If a call is refused with `429` (`rate_limited`), wait for the seconds in `Retry-After` or `retry_after_seconds` before retrying.
- Read only the specific sections or evidence units you need; do not harvest a whole Act part-by-part.

## Developer API-key fallback

Any OAuth-capable MCP client should connect through browser sign-in using `https://api.open.alvary.ai/mcp`.

If you are writing a script, CI check or custom MCP client that cannot sign in through a browser, create an [API key](/docs/authentication#api-keys) in the console under **API keys** (`/console/keys`) and send `Authorization: Bearer <key>` on each `POST https://api.open.alvary.ai/mcp` request. Static API keys are a developer fallback, not the standard flow for interactive AI apps.

## What to ask

The tool has thirteen read-only [tools](/docs/api/mcp) and picks them itself. For example:

- "Find Nigeria's Freedom of Information Act. What is its official source, and may its text be reused?"
- "Which published Nigerian texts say who may apply for information? Quote the sections."
- "What does section 1(2) of the FOI Act 2011 say, exactly?"
- "Is Nigeria's Investments and Securities Act 2007 still in force? If not, what replaced it?"
- "List the Nigerian acts in the Open Alvary catalogue, with their citations."
- "What changed in the Open Alvary catalogue for Nigeria recently?"
- "How many records does the catalogue hold for Nigeria, and how many have a cleared text?"

Records without a published text return catalogue metadata, rights status and links to the official source rather than searchable full text. Zero results means Open Alvary does not hold or publish the text within your plan's active jurisdictions, never that a law does not exist. The AI tool writes its own summaries and does not give legal advice; the official published copy always prevails.

## Manage connected tools

**Connected apps** in the console (`/console/apps`) lists every tool connected to your organisation, who connected it and when it was last used. **Disconnect** stops a tool at its next request; remove it in the tool too ([pictures](/docs/console#apps)).

## If it doesn't work

| What you see | What to do |
|---|---|
| The approval page says none of your organisations can connect apps | Your organisation needs active access (sign in to the console once for Starter) and your role must be owner, admin or member. |
| The approval page asks you to accept terms first | Open the console, accept, then connect again. |
| The tool says it isn't authorised, or keeps asking you to sign in | The connection was disconnected or expired: connect again from the tool. |
| The tool says a jurisdiction is not included (`jurisdiction_not_included`) | On Starter, change your organisation's jurisdiction under **Plan and limits** (`/console/access`), or upgrade your plan. |
| The tool says a limit was reached (`rate_limited`) | Wait for `Retry-After` / `retry_after_seconds`; see [Plans and rate limits](/docs/plans) and [Errors](/docs/errors). |
