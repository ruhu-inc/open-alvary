# Court hierarchy registers

One file per jurisdiction, `<jurisdiction>.json` (`ng.json`, `ke.json`, `ng-la.json`), read by
`api/courts.py` (CPR-04). A register says, for that jurisdiction's law, which courts and tribunals
there are, how appeals run between them, and how each court's decisions bear on the others. It is
what lets an answer say that a judgment it cites is binding, persuasive or not applicable, and say
`unknown` when nobody has confirmed which.

There are no registers here yet. Do not add an entry you cannot cite: every court, relation and
precedent rule names at least one source, and a register is used only once someone has reviewed
it against those sources and marked it `reviewed`. Until then every answer from it is `unknown`.

## Format

```json
{
  "jurisdiction": "xx",
  "review_state": "unreviewed",
  "reviewed_by": null,
  "reviewed_on": null,
  "precedent": {
    "binds": true,
    "note": "Decisions of the apex court bind every other court.",
    "sources": ["constitution"]
  },
  "courts": [
    {
      "id": "apex",
      "name": "Supreme Court of Xland",
      "kind": "court",
      "level": "apex",
      "rank": 1,
      "valid_from": "1963-10-01",
      "valid_until": null,
      "appeals_to": [],
      "sources": ["constitution"]
    },
    {
      "id": "appeal",
      "name": "Court of Appeal of Xland",
      "level": "appellate",
      "rank": 2,
      "appeals_to": ["apex"],
      "sources": ["constitution"]
    }
  ],
  "relations": [
    {"court": "apex", "bound": "appeal", "relation": "binding", "sources": ["constitution"]},
    {"court": "appeal", "bound": "apex", "relation": "persuasive", "sources": ["practice"]},
    {"court": "apex", "bound": null, "relation": "binding", "sources": ["constitution"]}
  ],
  "sources": [
    {
      "id": "constitution",
      "title": "Constitution of Xland, 1999",
      "url": "https://example.org/constitution.pdf",
      "pinpoint": "s. 287(1)"
    },
    {"id": "practice", "title": "Practice Direction 3 of 2010", "url": "https://example.org/pd3.pdf"}
  ]
}
```

- **`jurisdiction`**: the place code, as in the file name.
- **`review_state`**: `unreviewed` or `reviewed`. A reviewed register names `reviewed_by` and
  `reviewed_on`. One person may review it.
- **`precedent`**: whether decisions bind later courts there at all. In a civil-law jurisdiction
  where they do not, record `"binds": false` with the rule and its source. It is never assumed
  from the legal system, and without it every answer is `unknown`. Where `binds` is false, no
  relation may be `binding`.
- **`courts`**: each court or tribunal. `kind` is `court` or `tribunal`. `level` is one of
  `apex`, `appellate`, `superior`, `subordinate`, `specialised`, `customary`, `regional` or
  `continental`, as in the jurisdiction profiles. `rank` is 1 for the highest, and courts of equal
  standing share a rank; leave it out rather than guess. `valid_from` and `valid_until` are the
  dates the court's role applies: `valid_until` is the first day it no longer applies, and `null`
  means open-ended. `appeals_to` lists the courts an appeal lies to. `jurisdiction` is set only
  for another jurisdiction's court that this one's law gives a role, such as a former final
  appellate court.
- **`relations`**: how decisions of `court` bear on `bound`, which is `binding`, `persuasive` or
  `not_applicable`. When `bound` is `null`, the relation covers the jurisdiction's courts
  generally. Give a relation its own `valid_from` and `valid_until` when it changed, for example a
  court whose decisions stopped binding when appeals to it ended. Two relations between the same
  courts may not apply on the same date.
- **`sources`**: what each entry cites: the title, a link to the official text, and the provision
  where it helps.

A court or relation that is not recorded is `unknown`: the register never infers one relation
from another or from a court's rank.
