# Court hierarchy registers

One file per jurisdiction, `<jurisdiction>.json` (`ng.json`, `ke.json`, `ng-la.json`, `aa-ohada.json`),
read by `api/courts.py` (CPR-04, JUR-03, JUR-04). A register says, for that jurisdiction's law, which
courts and tribunals there are, how appeals run between them, and how each court's decisions bear on
the others. It is what lets an answer say that a judgment it cites is binding, persuasive or not
applicable, and say `unknown` when nobody has confirmed which. REST v1 serves each register at
`/courts/{jurisdiction}`, and evidence units give a decision's court from it.

A regional or continental body has a register of its own (`aa-ohada`, `aa-eac`, `aa-ecowas`, `aa-au`):
its courts, the states they reach and from when, the routes from those states' courts to its own, and
declarations accepting a court's jurisdiction over cases from individuals. How its decisions bear on a
member state's courts is a relation like any other, limited to the subject matter its source gives;
never a rank above national courts.

Do not add an entry you cannot cite: every entry names at least one source. The registers here are
proposals until a reviewer has checked every entry against its sources and recorded the review:

```
python -m operations.courts show ingestion/courts/ke.json
python -m operations.courts review ingestion/courts/ke.json --by "A. Reviewer"
python -m operations.courts withdraw ingestion/courts/ke.json
```

Until then every answer from a register is `unknown`. A review records who reviewed it, the day, and
the SHA-256 of what was reviewed. A register changed after its review is refused (the tests fail and
the API answers `503`) until it is reviewed again, so nothing added later is answered as reviewed.

## Format

```json
{
  "jurisdiction": "xx",
  "review_state": "unreviewed",
  "reviewed_by": null,
  "reviewed_on": null,
  "reviewed_sha256": null,
  "precedent": {
    "binds": true,
    "note": "Decisions of the apex court bind every other court.",
    "sources": ["constitution"]
  },
  "courts": [
    {
      "id": "xx-sc",
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
      "id": "xx-ca",
      "name": "Court of Appeal of Xland",
      "level": "appellate",
      "rank": 2,
      "appeals_to": ["xx-sc"],
      "note": "Hears appeals from the High Court.",
      "sources": ["constitution"]
    }
  ],
  "relations": [
    {"court": "xx-sc", "bound": "xx-ca", "relation": "binding", "sources": ["constitution"]},
    {"court": "xx-ca", "bound": "xx-sc", "relation": "persuasive", "sources": ["practice"]},
    {"court": "xx-sc", "bound": null, "relation": "binding", "sources": ["constitution"]}
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
- **`review_state`**: `unreviewed` or `reviewed`, written by `operations.courts`, never by hand. A
  reviewed register names `reviewed_by`, `reviewed_on` and `reviewed_sha256`. One person may review it.
- **`precedent`**: whether decisions bind later courts there at all. In a civil-law jurisdiction
  where they do not, record `"binds": false` with the rule and its source. It is never assumed
  from the legal system, and without it every answer is `unknown`. Where `binds` is false, no
  relation may be `binding`.
- **`courts`**: each court or tribunal. Its `id` is the issuing authority's ID in the catalogue
  (`ke-hc`, `aa-ohada-ccja`), so a judgment's court is found by its record's `authority_id`. `kind`
  is `court`, `tribunal` or `quasi_judicial` (a body such as the African Commission, whose findings
  are recommendations). `level` is one of `apex`, `appellate`, `superior`, `subordinate`,
  `specialised`, `customary`, `regional` or `continental`, as in the jurisdiction profiles. `rank`
  is 1 for the highest, and courts of equal standing share a rank; leave it out rather than guess.
  `valid_from` and `valid_until` are the dates the court's role applies: `valid_until` is the first
  day it no longer applies, and `null` means open-ended. `appeals_to` lists the courts an appeal lies
  to. `jurisdiction` is set only for another jurisdiction's court that this one's law gives a role,
  such as a former final appellate court.
- **`relations`**: how decisions of `court` bear on `bound`, which is `binding`, `persuasive` or
  `not_applicable`. When `bound` is `null`, the relation covers the jurisdiction's courts
  generally. Give a relation its own `valid_from` and `valid_until` when it changed, for example a
  court whose decisions stopped binding when appeals to it ended. Two relations between the same
  courts, on the same matter, may not apply on the same date.
  - `matter` limits a relation to one of the register's `matters`: it answers only a question of
    that matter, and before a relation that is not limited.
  - `"members": true` (with `bound` `null`) covers the courts of each state the register lists in
    `members`, while it is one: "binding in a member state's courts on the Uniform Acts".
- **`matters`**: subject matters a relation or route is limited to, each with `id`, `name` and
  `sources`: `{"id": "uniform-acts", "name": "The Uniform Acts and regulations…", "sources": [...]}`.
- **`members`**: the states a regional or continental register's courts reach: `state` (the
  country's place code), `valid_from` (the instrument's entry into force for it), `valid_until` (the
  first day after its withdrawal took effect) and `withdrawal_notified`. `court` is set when the
  state is party only to that court's own instrument, such as the African Court's Protocol.
- **`member_routes`**: routes from member states' courts to a court of the register: `kind` is
  `cassation` (in place of the national court of last resort), `appeal` or `reference` (a question
  of interpretation referred by a national court), with an optional `matter`.
- **`declarations`**: declarations accepting a court's jurisdiction over cases from individuals and
  NGOs (African Court Protocol, art. 34(6)): `state`, `court`, `valid_from` (deposited),
  `valid_until` (the first day it no longer applied) and `withdrawal_deposited`.
- **`note`**: any court, relation, member, route or declaration may say in a sentence what its
  sources say, or what a reviewer must still settle.
- **`sources`**: what each entry cites: the title, a link to the official text, and the provision
  where it helps.

A court or relation that is not recorded is `unknown`: the register never infers one relation
from another, from a court's rank, or from a body's membership.
