---
title: Rights and reuse
description: What you may do with the catalogue and with each law's text.
---

Two things have separate rights: the **catalogue** that Open Alvary writes, and the **legal texts** it points to.

## The catalogue

The catalogue details that Open Alvary contributors write, such as summaries, notes and coverage descriptions, are published under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/). Credit "Open Alvary contributors", link to the licence, and say if you changed anything. The licence never covers the legal texts themselves.

## The legal texts

Being published officially does not, by itself, give permission to copy a text. Each record carries a rights decision, taken by a reviewer from evidence, and only texts cleared for reuse are served.

Two fields on every record say where it stands:

| `access_mode` | Meaning |
|---|---|
| `open_full_text` | A text is cleared for reuse here; [Get a record's texts](/docs/api/get-versions) returns it. |
| `metadata_link_only` | Only the catalogue record and the official link are available. Read the text at the official source. |

| `rights_status` | Meaning | What you can do |
|---|---|---|
| `OPEN` | Open to reuse. | Reuse the text; follow any attribution the rights record asks for. |
| `OPEN_WITH_CONDITIONS` | Open to reuse, with conditions. | Reuse it within the `conditions` in the rights record. |
| `LINK_ONLY` | Official link only. | Link to the official source; the text isn't served here. |
| `RESTRICTED` | Restricted. | Link to the official source only. |
| `RIGHTS_UNDER_REVIEW` | Not decided yet. | Link to the official source; check again later. |

## The rights record

[Get a record's rights](/docs/api/get-rights) shows the evidence behind the decision, and what the decision allows here:

```curl
curl https://api.open.alvary.ai/api/v1/rights/ng-foi-act-2011 \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

| Field | Meaning |
|---|---|
| `record.licence`, `record.licence_url` | The licence, when there is one. |
| `record.redistribution_allowed`, `commercial_reuse_allowed`, `derivatives_allowed` | `yes`, `no`, `conditional` or `unknown`. |
| `record.attribution_required`, `attribution_text` | Whether and how to credit the source. |
| `record.conditions` | Conditions of reuse, for `OPEN_WITH_CONDITIONS`. |
| `record.evidence`, `reviewer`, `reviewed_at`, `expires_at` | The evidence, who reviewed it, when, and when it must be reviewed again. |
| `decision.metadata`, `decision.full_text`, `decision.reasons` | Whether the record and the text may be shown here, and why not. |

Decisions can change: a review can expire, or new evidence arrive. When a text is withdrawn, the [change feed](/docs/changes) says so; check the rights before reusing texts you stored earlier.

This page explains the catalogue's rights fields; it isn't legal advice. How we decide is on the [rights and governance](/governance) page.
