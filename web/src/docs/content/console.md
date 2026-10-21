---
title: Console guide
description: Every page of the console, step by step, with pictures.
---

The console at [open.alvary.ai/console](/console) is where you get access, create API keys, connect AI tools such as Claude, and follow changes with monitors. The pictures show a made-up organisation, Kigali Legal Lab; the numbers in them match the steps beside each picture. Select a picture to see it full size.

## Sign in {#sign-in}

Open [the console](/console). There is no separate sign-up: the first time you sign in, your account is made.

- {1} **Continue with Google** if you have a Google account, or
- {2} **Continue with email**: we send a code to your address and you type it in.

![The console's sign-in page, with Continue with Google and Continue with email](/guide/sign-in.webp)

The first time, the console asks two things before you start.

- {1} Your full name, as colleagues will see it.
- {2} A name for your organisation, such as your company's. Leave it blank to use your own name. Access, keys and usage belong to an organisation; this one is yours, and you can rename it or invite colleagues later.
- {3} Agree to the [terms of service](/terms) and [acceptable use policy](/acceptable-use), and confirm you have read the [privacy notice](/privacy).
- {4} **Continue to the console**.

![The welcome form: full name, organisation name, two agreement boxes and Continue to the console](/guide/welcome.webp)

The dashboard is the starting point. It shows your plan, today's requests and what is in use.

- {1} **Create an API key** to call the API from your own code ([below](#keys)).
- {2} **Connect Claude or Codex** to ask questions over the corpus from an AI tool ([below](#apps)).
- {3} **Read the docs**: these pages.

![The dashboard: three cards to create an API key, connect Claude or Codex, and read the docs](/guide/dashboard.webp)

## Choose a plan {#plan}

Every new organisation starts on **Starter**: free, with no card, and ready at once. It reads every country through the REST API and MCP, at 60 requests a minute. You can create a key and start straight away; the other plans raise the limits ([compare plans](/docs/plans)).

- {1} Your current plan and its limits.
- {2} **Request upgrade** on Developer or Team. For research, government or legal aid, choose **Apply** under Public interest; for Enterprise, **Contact us**.

![Plan and limits: the current plan, Starter, and cards for Developer, Team and Enterprise](/guide/plans.webp)

The request form asks how you will use the corpus. Only owners can request a plan.

- {1} What you are building. Don't include confidential client or case details.
- {2} The countries you need most. Developer includes 2 of your choice and Team 5; other countries still answer, at the Starter rate.
- {3} Roughly how many requests a day you expect.
- {4} **Send request**.

![The upgrade request form: how you will use it, countries, expected requests a day and Send request](/guide/plan-request.webp)

Your request shows as **Being reviewed**, and we reply by email. Prices for paid plans are coming; until then, we confirm each upgrade by email. You can withdraw the request while it is open.

![The plan page with the request for Developer marked Being reviewed](/guide/plan-requested.webp)

## Create an API key {#keys}

A key lets your code call the REST API and MCP. Claude, ChatGPT and Codex don't need one: they sign in themselves ([below](#apps)).

> Note: On a paid or approved plan, owners and admins confirm a second factor before creating keys or connecting apps. Set one up first under [Security and profile](#security); on Starter it's recommended.

- {1} Open **API keys** and choose **Create key**.

![The API keys page with no keys yet and the Create key button](/guide/keys-empty.webp)

- {1} A name, so you can tell keys apart later.
- {2} **Personal** for your own scripts: it stops working if you leave the organisation. **Shared, for servers** belongs to the organisation and survives people leaving (owners and admins).
- {3} When it expires. A key can read the published corpus within the organisation's access; nothing else.
- {4} **Create key**.

![The Create an API key form: name, type, expiry and Create key](/guide/key-create.webp)

- {1} **Copy** the key. This is the only time it is shown: store it in a password manager or your server's secret store. (It is blurred in this picture.)
- {2} **I've stored it** closes the window.

![The new key, blurred, with a Copy button, a sample request and I've stored it](/guide/key-created.webp)

Then make your first request, with the key in the Authorization header ([Quickstart](/docs/quickstart)):

```bash
export OPEN_ALVARY_KEY="oa_live_..."
curl "https://api.open.alvary.ai/api/v1/search?q=constitution" \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

The list shows each key's type, who made it, when it expires and when it was last used. **Rotate** gives a new key in its place, even when you have as many keys as allowed. The old one keeps working for 24 hours, marked **Replaced**, and isn't counted meanwhile. **Revoke** stops a key at once.

![The API keys list with one personal key, Rotate and Revoke](/guide/keys-list.webp)

## Connect an AI tool {#apps}

AI tools that speak MCP sign in with your Open Alvary account and read the corpus for one organisation. You approve each one, and can disconnect it any time. Open **Connected apps** and choose **Connect an app**.

![The Connected apps page with no apps yet and the Connect an app button](/guide/apps.webp)

### Claude

- {1} Choose the tab for your tool.
- {2} Copy the address, `https://api.open.alvary.ai/mcp`.

In Claude, open **Settings → Connectors**, choose **Add custom connector**, paste the address and add it. Then choose **Connect**: Claude sends you to Open Alvary to sign in and approve.

![Connect an app, Claude tab: the steps and the MCP address with a Copy button](/guide/connect-claude.webp)

### Claude Code

Add the server, then run `/mcp` in Claude Code, choose **open-alvary** and **Authenticate**. Your browser opens to approve.

```bash
claude mcp add --transport http open-alvary https://api.open.alvary.ai/mcp
```

![The Claude Code tab: the command to add the server and how to authenticate](/guide/connect-code.webp)

### Codex

Add the server and sign in; approve in the browser window that opens.

```bash
codex mcp add open-alvary --url https://api.open.alvary.ai/mcp
codex mcp login open-alvary
```

### Other tools

Other MCP clients that sign in with OAuth, such as ChatGPT, use the same address. Clients that take a header instead can send an API key as `Authorization: Bearer`. More in [Connect AI tools](/docs/ai-tools).

### Approve

The tool sends you to this page. The badge says who published it: **Verified publisher** means we have checked who makes the app; be careful with an **Unverified app** you didn't start yourself.

- {1} Choose the organisation it acts for.
- {2} **Approve**. You go back to the tool, and it can now search and read the corpus.

![The approval page: Claude wants to connect, a Verified publisher badge, the organisation and Approve](/guide/consent.webp)

Connected apps lists every app with who connected it, when, and when it was last used. **Disconnect** stops it at its next request; remove it in the app too, so it stops asking you to sign in.

![Connected apps listing Claude, a verified publisher, with Disconnect](/guide/apps-list.webp)

## Monitor changes {#monitor}

A monitor watches for changes to the records you care about: laws added, catalogue records corrected, texts published, records withdrawn, and reviewed memberships of regional bodies and suspensions from them. Matches arrive in an inbox in the console, and by email if you choose. Open **Monitor** and choose **New monitor**.

![The Monitor page before any monitor exists, with New monitor](/guide/monitor-empty.webp)

- {1} A name, such as "Nigeria: new and corrected laws".
- {2} Countries to follow. A country includes its states and local law.
- {3} Kinds of change: **Added**, **Corrected**, **Text published**, **Withdrawn**, **Coverage**, **Membership** and **Suspension**.
- {4} Optionally, a keyword from the title or citation.
- {5} Email: **No email, inbox only**, a **Weekly digest**, or a **Daily digest** on every plan except Starter. On Team and above you can also share a monitor with your organisation.
- {6} **Create monitor**.

![The New monitor form: name, countries, kinds of change, keyword, email and Create monitor](/guide/monitor-new.webp)

Leave countries empty to follow every country, or kinds empty to follow every kind, but choose at least one country, record, kind or keyword: a monitor with none is refused.

A monitor tells you about changes after it starts. To check your choices first, **Preview recent changes** shows what it would have matched recently.

![Preview recent changes listing a corrected catalogue record](/guide/monitor-preview.webp)

Matches arrive in the **Inbox**, newest first, with the kind of change, the date and the monitor that matched. Each links to the record. **Mark all read** clears the count; **Your monitors** lists your monitors to edit or delete. The page also shows how many monitors your plan allows.

![The Monitor inbox with one new match: a corrected record](/guide/monitor-inbox.webp)

You can also start from any record on this website: under **Changes to this record**, choose **Monitor this record**. The console opens a new monitor for that record.

![A record page's Changes to this record panel with Monitor this record](/guide/record-monitor.webp)

Choose when digests arrive in **Email digests**, under **Security and profile** (your name, at the bottom left).

- {1} Turn digest emails on or off. Matches always stay in your inbox.
- {2} Your time zone, the time of day, and the day for weekly digests.
- {3} **Save**.

![Email digests settings: send digest emails, time zone, time, weekly day and Save](/guide/monitor-digest.webp)

Every digest email has a link to stop it, without signing in.

## Webhooks {#webhooks}

On every plan except Starter, webhooks send your monitors' matches to your own HTTPS endpoint as they happen, signed so you can check each one came from Open Alvary. Create a monitor first, then open **Webhooks** and choose **Add endpoint**. The [webhooks guide](/docs/webhooks) has the code to receive them.

![The Webhooks page with no endpoints and Add endpoint](/guide/webhooks-empty.webp)

- {1} Your endpoint's address: HTTPS on port 443, reachable from the internet. Add a description if you like, and choose the monitors it receives.
- {2} **Add endpoint**.

![The Add a webhook endpoint form: endpoint URL, description, monitors and Add endpoint](/guide/webhooks-add.webp)

Copy the signing secret: it is shown only once (blurred here). We then send your endpoint a challenge. To answer it, reply to that first POST, of type `endpoint.verification`, with a 2xx status and the body `{"challenge": "<the challenge>"}`. If your endpoint wasn't ready, choose **Verify** later to try again.

![The endpoint's signing secret, blurred, and how to verify the endpoint](/guide/webhooks-secret.webp)

Each endpoint has **Verify**, **Send test**, **Deliveries** (what was sent and how it answered), **Rotate secret** and **Delete**. **How to receive deliveries** on the page has sample code.

![An endpoint not yet verified, with Verify, Send test, Deliveries, Rotate secret and Delete](/guide/webhooks-list.webp)

## Usage {#usage}

**Usage** counts the requests of all your organisation's keys and apps, per day (UTC): requests and data sent today, requests refused in the last 30 days, a daily chart, and today's requests by key or app. Limits are shared by every key and app in the organisation.

![The Usage page: requests today, data sent, refused requests, a daily chart and requests by key](/guide/usage.webp)

## Members {#members}

Invite colleagues to your organisation from **Members**.

- {1} Their email address.
- {2} Their role. **Admin** manages members and shared keys; **Developer** creates their own keys and connects apps; **Viewer** sees access and usage only. The **Owner** also manages access and can delete the organisation.
- {3} **Send invitation**. They accept by signing in with that address within 7 days.

![The Members page: the owner, and the Invite someone form with email, role and Send invitation](/guide/members.webp)

## Security and your data {#security}

Choose your name at the bottom left of the console, then **Security and profile**.

- {1} **Set up a second factor**: Touch ID, a security key or an authenticator app. You set it up on the sign-in page and come back. Keep the recovery code it shows.

![The Security page: Second factor with Set up a second factor, and sign-in methods](/guide/security.webp)

Below that you can add a sign-in method (Google or an email code), change your name, and see and end the places you are signed in. At the bottom, under Your data:

- {1} **Download my data**: everything Open Alvary holds about you.
- {2} **Delete my account**. The console first tells you what follows: your keys and connected apps stop, and an organisation where you are the only member is deleted with it.

![Your data: Download my data and Delete my account](/guide/your-data.webp)

See the [privacy notice](/privacy) for how long things are kept, and the [security page](/security) for how accounts and keys are protected.
