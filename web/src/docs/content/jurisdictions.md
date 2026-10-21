---
title: Jurisdictions, bodies and courts
description: The jurisdictions the catalogue holds, which regional bodies a country belongs to on a date, and each jurisdiction's courts.
---

Besides the records, the API serves reference data about the places they come from: the jurisdictions, the regional bodies a country belongs to, and the courts and how their decisions bear on each other. These routes need a key, like every data route, and count towards your organisation's limits, but your plan's countries don't narrow them: every jurisdiction and every register answers at your plan's own limits.

## List the jurisdictions

[List jurisdictions](/docs/api/list-jurisdictions) gives every jurisdiction the catalogue holds: countries and their states or regions, and the African Union and the regional bodies (IDs starting `aa-`, such as `aa-ecowas`), with their legal systems and languages. Each country has its `member_of`, as below.

## Find which bodies a country belongs to

[Get a jurisdiction](/docs/api/get-jurisdiction) tells you which regional bodies a country belonged to on a date: the African Union, ECOWAS, the EAC, OHADA and the others. For a body such as `aa-ecowas`, it lists the members instead.

```curl
curl "https://api.open.alvary.ai/api/v1/jurisdictions/sn?as_of=2026-01-01" \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

```python
import os

import requests

senegal = requests.get(
    "https://api.open.alvary.ai/api/v1/jurisdictions/sn",
    params={"as_of": "2026-01-01"},
    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},
    timeout=30,
).json()
if senegal["member_of"]["status"] == "unknown":
    print("No membership reviewed yet")
for body in senegal["member_of"]["items"]:
    print(body["body_name"], body["valid_from"], body["evidence_url"])
```

```javascript
const url = new URL("https://api.open.alvary.ai/api/v1/jurisdictions/sn");
url.searchParams.append("as_of", "2026-01-01");
const senegal = await (
  await fetch(url, { headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` } })
).json();
for (const body of senegal.member_of.items) console.log(body.body_name, body.valid_from, body.evidence_url);
```

{{example:get_jurisdiction}}

What the answer means:

| Field | Meaning |
|---|---|
| `member_of` | For a country: the bodies it belonged to on `as_of` (today if you leave it out). |
| `members` | For the African Union or a regional body: its members on `as_of`. |
| `items` | The memberships held on that date. |
| `uncertain` | Memberships that may or may not have held on that date, because it falls within an uncertain first or last day (see below). We never guess which. |
| `ended` | Memberships that had certainly ended by then, with their last day in `valid_to`. A country that left ECOWAS on 29 January 2025 has `valid_to` 28 January 2025. |
| `valid_from`, `valid_to` | The first and last day of the membership, as precisely as the evidence gives them (see below). `valid_to` is null if it has not ended. |
| `valid_from_bounds`, `valid_to_bounds` | The earliest and latest day each date can be, and its `precision`: `day`, `month`, `year` or `range`. |
| `basis` | Why the membership starts when it does: `entry_into_force`, `deposit` (of the country's instrument), `admission_decision`, `provisional_application_on_signature` or `signature`, with the article that says so (`instrument`, `pinpoint`), where it was read and, when recorded, its words. |
| `suspensions` | The country's suspensions from taking part in the body's activities, with their dates, legal basis and decisions, and `in_effect` on `as_of`. Each article of the `legal_basis` has a `role`: `decision_basis` is the article the decision itself cites, with the decision's words; `treaty_rule` is the treaty's rule the suspension applies, with the article's words, whether or not the decision names it. A suspended country is still a member: it stays in `items`. |
| `evidence_url`, `note` | The body's own source, or the treaty depositary's, that the dates were checked against, and what it says. |
| `status` | `reviewed`, or `unknown` when no membership has been reviewed. `unknown` never means "none". |

Dates are written as [EDTF](https://www.loc.gov/standards/datetime/) dates, so a date known only to the month or the year is never filled in:

| `valid_from` or `valid_to` | Means |
|---|---|
| `2001-05-26` | That day. |
| `2000-12` | A day in December 2000. |
| `1976` | A day in 1976. |
| `[2016-08-15..2016-09-05]` | One day between 15 August and 5 September 2016, inclusive, unknown which: the sources disagree. It is not a period lasting from one to the other. A window of years, such as `[1976..1977]`, is one day in one of those years. |

A window is EDTF's "one of a set", in square brackets. We never write it with a slash: in EDTF, `2016-08-15/2016-09-05` is an interval, every day from the first to the last.

A date that falls within such a window is listed in `uncertain`, not in `items` or `ended`: for a country that left a body in December 2000, `as_of` 2000-12-15 gives `uncertain`, 2000-11-30 gives `items` and 2001-01-01 gives `ended`.

A membership is listed only after a reviewer has checked it against its evidence. One that has not been checked yet is left out, so a missing body is not proof that a country is not a member. Membership alone does not mean a body's treaties apply to a member: signature, ratification and entry into force are separate facts.

[List jurisdictions](/docs/api/list-jurisdictions) gives every country's `member_of` in the same form. Changes to memberships are in the [change feed](/docs/changes) as `membership_recorded`, and suspensions as `suspension_recorded`. To search a country's law together with the regional and treaty law that binds there, see [the law that applies in a country](/docs/retrieval#the-law-that-applies-in-a-country).

## Court registers

A jurisdiction's court register records its courts and tribunals, with their levels and appeal routes, how each court's decisions bear on the others, and whether precedent binds there at all. Every entry gives its sources and has its own review.

```curl
curl https://api.open.alvary.ai/api/v1/courts/ke \
  -H "Authorization: Bearer $OPEN_ALVARY_KEY"
```

- [List court registers](/docs/api/list-court-registers) says which jurisdictions have a register, and how many of each register's entries have been reviewed. A jurisdiction not listed has no register: every court relation there is `unknown`.
- [Get a court register](/docs/api/get-court-register) gives one register: its `precedent` rule, its `courts` with their levels and appeal routes, and the `relations` between them; for a regional or continental body (`aa-ohada`, `aa-eac`, `aa-ecowas`, `aa-au`), also the `matters` its courts decide, the `members` they reach, the `member_routes` from national courts and the `declarations` admitting individual cases.
- Each entry has its own review. Courts, matters, members, routes and declarations have a `structural` one; relations and the precedent rule a `doctrinal` one, by someone legally qualified for the jurisdiction. Until a relation has its own doctrinal review, its `relation` is `unknown`, and `recorded` shows what awaits review.

No register has been reviewed yet, so for now every relation is `unknown`. How a decision's court appears in an evidence pack, and what each relation means, is in [Court authority](/docs/retrieval#court-authority). How one judgment cites and treats another, paragraph by paragraph, is in [Case law](/docs/records#case-law-citations-and-treatment).
