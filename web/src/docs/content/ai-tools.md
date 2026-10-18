---
title: Connect AI tools
description: Ask Claude, ChatGPT, Claude Code or Codex about the catalogue, with no key.
---

AI tools that speak MCP (the Model Context Protocol) can search and read the catalogue for you. You add Open Alvary to the tool once, sign in, and approve it for your organisation; after that, ask questions in your own words and the tool calls Open Alvary when it needs to.

| | |
|---|---|
| Address | `https://api.open.alvary.ai/mcp` |
| Sign-in | In your browser, when the tool asks; no key needed |
| What the tool can do | Read the published catalogue and changes, within your organisation's plan |
| What it can't do | Change anything, see your keys, or act for another organisation |

You need an Open Alvary account with access (the free Starter plan is enough) and the role of owner, admin or developer in the organisation.

## Claude

1. In Claude, open **Settings → Connectors** and choose **Add custom connector**.
2. Paste `https://api.open.alvary.ai/mcp` and add it.
3. Choose **Connect**. Claude sends you to Open Alvary: sign in, choose your organisation and **Approve**.

![Connect an app, Claude tab: the steps and the MCP address with a Copy button](/guide/connect-claude.webp)

## Claude Code

```bash
claude mcp add --transport http open-alvary https://api.open.alvary.ai/mcp
```

Then run `/mcp` in Claude Code, choose **open-alvary** and **Authenticate**. Your browser opens to approve.

## Codex

```bash
codex mcp add open-alvary --url https://api.open.alvary.ai/mcp
codex mcp login open-alvary
```

Approve in the browser window that opens.

## ChatGPT and other tools

Any MCP client that signs in with OAuth uses the same address: add it where the tool lets you add an MCP server or connector, then approve in your browser when asked. A client that can't sign in but can send a header can use an [API key](/docs/authentication#api-keys) as `Authorization: Bearer <key>`.

## Approve the tool

The tool opens this page. Check the badge: **Verified publisher** means we have checked who makes the app. Be careful with an **Unverified app** you didn't start yourself.

![The approval page: Claude wants to connect, a Verified publisher badge, the organisation and Approve](/guide/consent.webp)

- {1} Choose the organisation the tool acts for.
- {2} **Approve**.

On a paid or approved plan, owners and admins also confirm a second factor here.

## What to ask

The tool has six [tools](/docs/api/mcp) and picks them itself. For example:

- "Find Nigeria's Freedom of Information Act. What is its official source, and may its text be reused?"
- "Is Nigeria's Investments and Securities Act 2007 still in force? If not, what replaced it?"
- "List the Nigerian acts in the Open Alvary catalogue, with their citations."
- "What changed in the Open Alvary catalogue for Nigeria recently?"
- "How many records does the catalogue hold for Nigeria, and how many have a cleared text?"

The tool's answers come from the catalogue, but the tool writes them: check what matters against the official source it links.

## Manage connected tools

**Connected apps** in the console lists every tool connected to your organisation, who connected it and when it was last used. **Disconnect** stops a tool at its next request; remove it in the tool too ([pictures](/docs/console#apps)). Tools share your organisation's [limits](/docs/plans) with its keys.

## If it doesn't work

| What you see | What to do |
|---|---|
| The approval page says none of your organisations can connect apps | Your organisation needs access (sign in to the console once for Starter) and your role must be owner, admin or developer. |
| The approval page asks you to accept terms first | Open the console, accept, then connect again. |
| The tool says it isn't authorised, or keeps asking you to sign in | The connection was disconnected or expired: connect again from the tool. |
| The tool says a limit was reached | Wait a minute; see [Plans and rate limits](/docs/plans). |
