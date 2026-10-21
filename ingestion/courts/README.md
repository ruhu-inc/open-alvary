# Court hierarchy registers

One file per jurisdiction, `<jurisdiction>.json` (`ng.json`, `ke.json`, `ng-la.json`, `aa-ohada.json`),
read by `api/courts.py` (CPR-04, JUR-03, JUR-04). A register says, for that jurisdiction's law, which
courts and tribunals there are, how appeals run between them, and how each court's decisions bear on
the others. It is what lets an answer say that a judgment it cites is binding, prevails on a matter,
binds only in the case referred, is persuasive or not applicable, and say `unknown` when nobody has
confirmed which. REST v1 serves each register at `/courts/{jurisdiction}`, and evidence units give a
decision's court from it.

A regional or continental body has a register of its own (`aa-ohada`, `aa-eac`, `aa-ecowas`, `aa-au`):
its courts, the states they reach and from when, the routes from those states' courts to its own, and
declarations accepting a court's jurisdiction over cases from individuals. How its decisions bear on a
member state's courts is a relation like any other, limited to the subject matter its source gives;
never a rank above national courts.

Do not add an entry you cannot cite: every entry names at least one source, and every relation quotes
the words it rests on. The registers here are proposals until reviewers have checked each entry
against its sources and recorded their reviews, entry by entry:

```
python -m operations.courts show ingestion/courts/ke.json
python -m operations.courts review-structural ingestion/courts/ke.json --by "A. Owner" --role owner
python -m operations.courts review ingestion/courts/ke.json ke-sc-on-courts --by "A. Advocate" \
    --role "Kenyan advocate" --level doctrinal --qualification "Advocate of the High Court of Kenya"
python -m operations.courts withdraw ingestion/courts/ke.json [ke-sc-on-courts]
```

Each entry is reviewed at its kind's level:

- **Structural**: courts (with their appeal routes), matters, members, routes from member states'
  courts and declarations. Who sits where, from when, and where an appeal or a reference lies. The
  owner may review them; `review-structural` reviews every one not yet reviewed.
- **Doctrinal**: relations and the precedent rule. How decisions bind, prevail or persuade. A
  doctrinal review states the reviewer's legal qualification for the jurisdiction (`--qualification`),
  whatever their role, and is refused without one.

A review records who reviewed the entry, their role (`owner`, `Kenyan advocate`, `community-law
academic`), the level, the day, and the SHA-256 of the entry as reviewed, with the sources it cites and
the matter it is limited to. Until an entry is reviewed, the API serves it as unreviewed, and a relation
answers `unknown`. An entry changed after its review is served as unreviewed again, on its own, and the
tests fail until it is reviewed again or its review withdrawn, so nothing changed later is answered as
reviewed.

## Format

```json
{
  "jurisdiction": "xx",
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
    {
      "id": "xx-sc-on-xx-ca",
      "court": "xx-sc",
      "bound": "xx-ca",
      "relation": "binding",
      "rests_on": [
        {
          "source": "constitution",
          "provision": "s. 287(1)",
          "quote": "Decisions of the Supreme Court shall bind every other court."
        }
      ],
      "sources": ["constitution"]
    },
    {
      "id": "xx-ca-on-xx-sc",
      "court": "xx-ca",
      "bound": "xx-sc",
      "relation": "persuasive",
      "rests_on": [
        {"source": "practice", "provision": "para 4", "quote": "The Court may consider them."}
      ],
      "sources": ["practice"]
    }
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
- **`id`**: every entry has one, unique in the register, which `operations.courts` reviews it by. The
  precedent rule's is `precedent`.
- **`review`**: an entry's review, written by `operations.courts`, never by hand: `review_level`,
  `reviewed_by`, `reviewer_role`, `reviewer_qualification` (a doctrinal review states one),
  `reviewed_on` and `reviewed_sha256`. Absent until the entry is reviewed.
- **`precedent`**: whether decisions bind later courts there at all. In a civil-law jurisdiction
  where they do not, record `"binds": false` with the rule and its source. It is never assumed
  from the legal system. Without it, reviewed, a `binding`, `persuasive` or `not_applicable` relation
  answers `unknown`. Where `binds` is false, no relation may be `binding`.
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
- **`relations`**: how decisions of `court` bear on `bound`. When `bound` is `null`, the relation
  covers the jurisdiction's courts generally. `relation` is, in the source's own terms:
  - `binding`: precedent binds (stare decisis);
  - `prevails_on_matter`: a rule of supremacy or precedence on a subject matter, such as EAC Treaty
    art. 33(2) ("Decisions of the Court on the interpretation and application of this Treaty shall
    have precedence over decisions of national courts on a similar matter"). It is not stare decisis,
    always names its `matter`, and answers only a question of that matter;
  - `binding_in_referred_case`: the ruling binds the court that referred the question, in that case
    only, as for a preliminary reference whose source says so. It says nothing of other cases;
  - `persuasive` or `not_applicable`.

  Never record a relation in other words than its source's: where the source fits none of these, say
  what it says in a `note` instead. `rests_on` quotes the source verbatim: each quote names its
  `source` (one the relation cites), the `provision` it is in, and the `quote`. Give a relation its
  own `valid_from` and `valid_until` when it changed, for example a court whose decisions stopped
  binding when appeals to it ended. Two relations between the same courts, on the same matter, may not
  apply on the same date, save one binding only in the referred case beside one with general effect.
  - `matter` limits a relation to one of the register's `matters`: it answers only a question of
    that matter, and before a relation that is not limited.
  - `"members": true` (with `bound` `null`) covers the courts of each state the register lists in
    `members`, while it is one: "binding in a member state's courts on the Uniform Acts".
- **`matters`**: subject matters a relation or route is limited to, each with `id`, `name` and
  `sources`: `{"id": "uniform-acts", "name": "The Uniform Acts and regulations…", "sources": [...]}`.
- **`members`**: the states a regional or continental register's courts reach: `state` (the
  country's place code), `valid_from` (the instrument's entry into force for it), `valid_until` (the
  first day after its withdrawal took effect) and `withdrawal_notified`. `court` is set when the
  state is party only to that court's own instrument, such as the African Court's Protocol. A
  relation to member states' courts answers for a state only once its membership is reviewed.
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
