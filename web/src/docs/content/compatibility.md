---
title: Compatibility
description: Which changes come at any time, how breaking changes are announced, and what your code should ignore.
---

The REST API, the MCP server and the change feed change in two ways: by adding to what they answer, which can happen at any time, and by breaking changes, which never happen within a version. Every change that affects how you use them is in the [changelog](/docs/changelog).

## Additive changes

These can come at any time, and are recorded in the changelog when they take effect:

- a new field in an answer, or in an object within one;
- a new value of a field that lists its values, such as a relationship `kind`, an `applicability.relation` or an error `code`;
- a new kind of change in the feed (`type`), a new response header, a new route or a new optional parameter;
- a new MCP tool, or a new optional argument of one.

So your code should:

- ignore fields it doesn't know, rather than refuse the answer;
- treat a value it doesn't know as unknown, never as an error: show a link of a kind you don't know without its meaning, or leave it out;
- skip kinds of change it doesn't know, and still keep the feed's `resume_token`;
- branch on the error `code`, never on `detail`, and handle a code it doesn't know by its HTTP status.

The change feed's events keep `schema_version` `change-event-1` while fields and kinds of change are added.

## Breaking changes

A change is breaking when code written to these pages could stop working: a field, value or route removed or renamed, a field's type or meaning changed, or a parameter made required.

A breaking change never comes within the current version. It comes as a new versioned path (such as `/api/v2`) or a new format name (such as `change-event-2`), and an MCP tool is renamed only by adding the new name beside the old one. The old path, format or name keeps working beside the new one until it is retired. The changelog announces the new one, with the date the old one will be retired, before that date.

The old one keeps working for at least 180 days after the announcement. From the announcement, a route or format being retired answers with the `Deprecation` and `Sunset` headers (RFC 9745 and RFC 8594), and we email the organisations whose keys used it in the 90 days before.

## What is not a change to the API

- Withdrawals and corrections of records and texts take effect at once. They are changes to the data, recorded in the [change feed](/docs/changes), never held back for a notice period; you apply them to the copies you keep within 7 days, as the [acceptable use policy](/acceptable-use) asks. See [Apply withdrawals and corrections](/docs/mirror#apply-withdrawals-and-corrections).
- Ranking and routing in [Retrieve evidence](/docs/retrieval) improve over time, so the same request may give a different answer. Each answer names the revisions it used, in `revisions`.
