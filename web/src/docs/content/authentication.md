---
title: Authentication
description: API keys, connected apps, roles and the second factor.
---

Every request to the REST API and the MCP server carries a credential in the `Authorization` header:

```http
Authorization: Bearer <credential>
```

The credential is either an **API key**, for your own code, or an **access token** that an AI tool gets when you connect it. Both read the same published corpus, within your organisation's plan, and share its limits. Neither can change anything: the API is read-only.

Three things need no credential: the change feed at `https://open.alvary.ai/api/changes`, the [plans](/docs/api/list-plans) and the [OpenAPI document](https://api.open.alvary.ai/api/v1/openapi.json).

## API keys

Create keys in the console under **API keys** ([pictures](/docs/console#keys)). A key looks like `oa_live_` followed by its ID and a secret, and is shown once, when you create it.

| | Personal | Shared, for servers |
|---|---|---|
| For | Your own scripts and experiments | Production servers and scheduled jobs |
| Who can create one | Owners, admins and developers | Owners and admins |
| When the person leaves | Stops working | Keeps working; it belongs to the organisation |
| How many | 10 per person per organisation | 20 per organisation |

Every key expires: after 90 days unless you choose otherwise, and after at most 365. The console shows when each key was last used.

- **Rotate** a key to replace it: you get a new key in its place, and the old one keeps working for 24 hours so you can switch over. You can rotate even when you have as many keys as allowed, because the old key isn't counted during those 24 hours. If you rotate the new key before they are up, the old one stops at once.
- **Revoke** a key to stop it at once.
- If your organisation's access ends, its keys are kept and refused with [`entitlement_lapsed`](/docs/errors#entitlement_lapsed) until access is renewed.

### Keeping keys safe

- Keep keys in environment variables or a secret store, never in source code.
- Send keys only in the `Authorization` header. A key in the address is refused with [`credential_not_accepted`](/docs/errors#credential_not_accepted), because addresses end up in logs.
- Never use a key in a web page. The API's data routes don't allow calls from other websites (no CORS), so a key in browser code wouldn't work anyway; call the API from your server.
- Revoke a key you think was exposed, and create a new one.

## Connected apps

AI tools such as Claude, Claude Code, Codex and ChatGPT don't use keys. When you add Open Alvary to one, it sends you to sign in and approve it for one of your organisations; it then holds an access token for that organisation only. See [Connect AI tools](/docs/ai-tools).

Owners, admins and developers can connect apps. **Connected apps** in the console lists them; disconnecting one stops it at its next request. An app can only read the corpus (the scope `corpus:read`). It can't see your keys, change anything or act for another organisation.

### For tool builders

The API and the MCP server follow the MCP authorization specification, built on OAuth 2.1. A request without a credential is answered `401` with a `WWW-Authenticate` header naming the resource's metadata:

```http
WWW-Authenticate: Bearer resource_metadata="https://api.open.alvary.ai/.well-known/oauth-protected-resource/mcp", scope="corpus:read"
```

That document names the authorisation server; tokens must be issued for the exact resource, `https://api.open.alvary.ai/mcp` or `https://api.open.alvary.ai/api/v1`, with the scope `corpus:read`. Tokens for one resource are refused by the other.

## Roles

Each person has a role in each organisation they belong to.

| Role | Can |
|---|---|
| Owner | Everything an admin can, plus request plans, end access, transfer ownership and delete the organisation |
| Admin | Invite and remove members, create shared keys, revoke any key or app, read the audit log |
| Developer | Create personal keys and connect apps |
| Viewer | See the organisation's access and usage |

Invite people under **Members** ([pictures](/docs/console#members)).

## Second factor

Owners and admins of an organisation with a paid or approved plan confirm a second factor (Touch ID, a security key or an authenticator app) before creating keys, connecting apps and other sensitive changes. On the free Starter plan it's recommended. Set one up under **Security and profile** in the console ([pictures](/docs/console#security)); keep the recovery code it shows.
