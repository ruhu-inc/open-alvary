# Open Alvary

A rights-aware, machine-readable public legal corpus for African jurisdictions.
Nigeria is the first catalogue. Open Alvary is shared public data infrastructure;
**Alvary is a separate proprietary legal-AI product** that may consume the same
public interfaces as researchers, legal-aid organisations and other developers.
No commercial Alvary code, customer material, private annotations, prompts,
playbooks, or proprietary workflows are included here.

## v0.1 reality

Four independently authored Nigerian catalogue records; **zero cleared full-text
sources**. Every seed rights record is `RIGHTS_UNDER_REVIEW`. The release contains
metadata and official links, not legal text. Public accessibility is not a licence.
The 2007 Investments and Securities Act is marked historical/repealed using the
[SEC Acts page](https://www.sec.gov.ng/our-mandate/regulation/acts/). No current
consolidation or complete amendment coverage is asserted for the other entries.

## Run locally

Python 3.12+ and Node 20.19+ (Node 22 recommended). Commands run from this repository.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
alembic upgrade head
python -m scripts.seed
uvicorn api.main:app --host 127.0.0.1 --port 8018
```

In another terminal:

```sh
npm --prefix web ci
npm --prefix web run dev
```

Open http://127.0.0.1:5178 and API docs at http://127.0.0.1:8018/docs.
SQLite is the no-service development default. For PostgreSQL, set `DATABASE_URL`
before running migrations, seeding and the API. `.env.example` is a template;
environment files are not automatically loaded.

PostgreSQL migration `0002` adds identity shadow tables and a transactional maintenance
fence. Legacy records remain authoritative; migration does not backfill automatically,
publish text, or enable a target writer. SQLite retains its pilot schema. The additional
immutable DTO contract is `schema/identity.schema.json` (`identity-1`); current HTTP
payloads and source URLs are unchanged.

Maintenance helpers in `api.identity` accept an existing transaction and never commit
for their caller. In a synthetic test database, run `backfill(connection, expected_epoch=0)`,
commit, then `freeze(connection, expected_epoch=0)` and commit before final backfill and
reconciliation at epoch 1. `rehearse_cutover` rechecks full record/policy/hash parity and
denies legacy serving; it enables no target writer. `end_rehearsal` returns to frozen,
then `resume_legacy` can end this pre-cutover exercise. These are development helpers;
authenticated production review and live cutover remain unimplemented.

The fence covers INSERT/UPDATE/DELETE/TRUNCATE on every legacy table, including direct
SQL, and waits for active writer transactions. Runtime roles must lack migration-state/
identity-table writes, table ownership, schema CREATE and superuser privileges. Restricted
synthetic roles exercise the fence; compose still uses owner credentials, so production
role provisioning remains outstanding. Backfill takes an exclusive state-row lock for
the bounded copy; measure lock time before real migration. Failed copies roll back.
Downgrade is allowed only in legacy phase and removes shadows, preserving pilot data.
After a future target writer starts, use forward repair or compatible application rollback;
never resume serving stale legacy policy.

PostgreSQL migration `0003` adds separate `policy`, `staging` and `corpus` schemas
(isolated child schemas in test namespaces). It seeds no decisions, actors or approvals.
Legacy metadata handlers and Vercel remain unchanged; these foundations do not activate
new public routes or full text. `schema/policy.schema.json` versions the private record
shapes and the small public metadata/result DTOs separately from pilot responses.

Role adoption is an explicit administrator operation, not a startup hook:
`rights.database.provision_roles(connection, PolicyNamespace(base_schema))`, inside a
caller-owned transaction. It creates NOLOGIN, non-superuser serving/acquisition/review/
release capability groups and a private function/view guard. It refuses existing role
names so grants must be inspected before reprovisioning. The migration administrator
needs role-creation and object-ownership authority; those privileges must never be given
to runtime users. Group membership is granted to individual database subjects separately.
The policy foundation currently accepts **synthetic identities only**; real provisioning,
qualification and authenticated command workflows remain future work. Existing compose
credentials are still owner credentials, not a deployment of these capability boundaries.

Serving has only an eligible metadata view and narrow Boolean policy functions; it cannot
read private evidence, raw staging, legacy tables or mutate decisions. Review access uses
forced row-level security and explicit collection assignments. Actor checks bind to the
database session subject, so switching an inherited role cannot impersonate a reviewer.
History is append-only; each revision and its audit event commit together. Version
overrides can only narrow collection grants. Private acquisition pins assessment/privacy/
controller revisions and bounded retention, independently of public permission. Expired
or superseded authority hides staging immediately; scoped erasure removes bytes through
a narrow audited function. The metadata view checks current policy on each statement;
final emission checks, restore/journal guarantees and real publication remain unimplemented.

`PolicyRepository` uses caller-owned transactions, bounded metadata lookups and revision
locks. Its approval preflight checks distinct current reviewers and all six required
material coverage records; it does not mint approved text versions. The guard owns narrow
functions/view, never private tables, and has no schema CREATE after provisioning. Runtime
groups cannot assume it. Nonempty policy foundations require forward repair; downgrade
will not discard decisions or evidence. Measure migration/lock costs before real use.

Migration `0004` adds a private operator command foundation. Its current appointments
are **synthetic only**: it supports development of individual native PostgreSQL login
sessions, not a production identity provider or appointment of qualified reviewers.
An administrator explicitly calls `rights.operators.register_operator` with an existing
nonprivileged LOGIN, a `SyntheticAppointment` containing a stable private issuer/subject,
finite expiry and evidence, and collection/capability assignments. It creates no password
or login and does not commit. One issuer/subject can bind only one actor; role OID and
name are pinned, so renaming/recreating a login cannot inherit an old appointment.
Inherited privilege, schema CREATE, object ownership and legacy writes reject the
runtime role profile. Old NOLOGIN subjects remain synthetic PG fixtures and cannot
use the command CLI. Existing role provisioning is upgraded explicitly by the migration;
fresh capability provisioning recognizes the new command function.

Supply an individually authenticated `ALVARY_OPERATOR_DATABASE_URL` using your local
secret configuration, then run `python -m rights.cli < private-command.json` (or the
installed `alvary-operator` command). The CLI requires PostgreSQL, uses one bounded
transaction/connection, and reads at most 64 KiB from stdin. It prints only command ID,
safe status, resulting revision and replay flag; it never prints input, credentials,
private evidence or database exception details. No HTTP write route is added.
The private `schema/operator.schema.json` contract defines these actions:

- `record_evidence`: append a scoped private reference, digest and observation time.
- `record_collection_decision`: append a rights revision with explicit operation vector,
  evidence, validity, privacy classification and reason.
- `record_verification_review`: append a content revision and all six required material
  coverage records together, including sampling, competence evidence and escalation/exclusion.

Each envelope contains `command_id`, `action`, `collection_id`, `expected_revision`,
`reason` and its typed `payload`. It accepts no actor, role or privilege fields.
The database derives the actor from the session and atomically records the mutation,
audit and immutable retry receipt. An identical retry by that actor returns the original
revision; changed intent or a stale revision returns conflict, and another actor cannot
claim the receipt. Repository methods use savepoints and leave the outer transaction
to their caller. A rollback removes the receipt as well as the decision/evidence.

Appointment/credential expiry, revocation, role-name change or unsafe privilege changes
deny subsequent commands, including retries on an established connection. Revocation
waits for an active command's transaction, records its own private maintenance event and
removes the login's capability group memberships.
These shadow commands also hold the legacy authority fence: frozen/rehearsal phases deny
them. Rights and content use their own sessions/assignments; release preflight still
requires two distinct reviewers. Holding both roles cannot satisfy independent review.
Session expiry closes new writes; earlier review records retain their own validity until
expiry or explicit actor revocation. Assignment additions/removals are audited and
coordinate through the same actor lock; scope removal denies subsequent command retries.
No command approves a text version, performs
acquisition, switches serving authority or enables public text. Real appointment,
authentication/TLS/session policy, reviewer qualification, restore rebinding and real
acquisition activation require separate evidence and implementation.

Migration `0005` and private `operator-command-2` extend the synthetic CLI with
`record_controller`, `record_privacy_review` and `record_acquisition_assessment`.
Controller identity/contact records, purpose-specific privacy clearance and acquisition
vectors append through native rights-reviewer sessions, with exact evidence/controller/
privacy revisions and the same atomic audit/receipt semantics. Positive approvals reject
stale, expired, rejected or mismatched required authority; rejection/denial records can
close authority without pretending an obsolete controller is current. Real controller
identity, legal/privacy assessment and qualified appointments remain production gates.

`rights.acquisition.SyntheticAcquisition` provides caller-owned stage/read/erase operations
for original local synthetic bytes. It performs no fetch, observation, job or parser.
Native staging pins the assessment's exact SHA-256 and purpose, records the acquiring
actor, and never retargets existing lineage. Class-level assessments without a known
hash can be recorded, but cannot authorize this synthetic byte writer. Private retain/
derive reads check current assessment/privacy/controller state and operation permission
before loading bytes. Unknown operations and external processing remain unavailable.
Supersession hides dependent rows immediately; expired or erased bytes cannot be restored
by a retry. Fresh reassessment may create a new explicitly qualified synthetic artifact;
it does not reopen an older lineage. Job resumption and backup/derived-copy erasure
remain future work.

Before native staging, maintenance explicitly calls `declare_synthetic_staging_limit`
with a collection, byte limit, evidence and reason. Nothing is declared at startup or
backfilled. The supported profile is `synthetic_public_min`, at most 100 MiB per declared
collection and 10 MiB per artifact. Database locking serializes concurrent allocations;
all physically retained bytes consume capacity until scoped audited erasure, including
expired/hidden rows. This is a synthetic implementation of the initial public-collection
bound, not a capacity decision for partner feeds or production. Serving/review roles
cannot access the staging interface. Safe acquisition failures and savepoints preserve
caller rollback without rendering private bytes or database parameters.

Old staging/assessment records retain null qualification/hash fields and their existing
fixture compatibility; native readers cannot silently adopt them. Migration `0005`
validates existing guard boundaries and refuses a destructive downgrade once new review
receipts, qualified records or capacity declarations exist. Empty isolated rollback
restores the earlier grants/policy functions. Applied migrations `0001`–`0004` are
unchanged. Production role adoption, network egress, observation authority, retention
automation and restore/denial-journal gates remain unimplemented; public serving stays
on the legacy metadata pilot.

Migration `0006` adds operator-run private lifecycle reconciliation. Submit one
`operator-command-3` lifecycle request to `alvary-operator --reconcile-staging`
(or `python -m rights.cli --reconcile-staging`) using the same explicit PostgreSQL
operator URL and native acquisition assignment. Required inputs are `command_id`,
`collection_id` and `reason`; optional `limit` is 1–100 (default 50), `after` is a UUID
cursor, and `erase_due` is a Boolean (default false). The collection must have an
explicit synthetic staging declaration. Responses contain only status, bounded counts
and `next_after`; no private content, evidence or controller contacts are returned.

`rights.lifecycle.AcquisitionLifecycle.reconcile` uses a caller-owned transaction.
Supersession, expiry and unavailable current authority materialize a hold. Privacy
rejection (including intervening rejected revisions) or a due artifact deadline
materializes `erasure_required`; `erase_due=true` also removes the bytes from the raw artifact row
with an erasure audit. Existing holds never resume automatically, deadlines never
extend, and assessment/purpose/hash lineage never retargets. There is no reviewed
restricted lawful-hold override, derived/index/backup deletion, scheduler or fetcher.
Conservative holds and erasure remain permitted while legacy writes are fenced.

Follow `next_after` with a fresh command ID; restart from a null cursor on a later
pass to catch changes or inserts before an earlier cursor. A pass is bounded traversal,
not a snapshot of a changing collection. Identical requests from the same currently
authorized actor replay their historical counts/cursor; changed requests conflict.
Lifecycle IDs have their own receipt namespace, separate from review command IDs.
States, append-only lineage events, audits and the receipt commit or roll back together;
a failed batch rolls back all its changes. Row and parent-authority locks coordinate
concurrent reconciliation, reviews, reads, erasure and actor revocation through commit.
RLS/current read gates already close access before materialization. Migration `0006`
preserves applied `0001`–`0005`, existing bytes and exact prior trigger definitions;
empty isolated rollback restores them, while recorded lifecycle use requires forward
repair. This synthetic local workflow does not activate production acquisition or text.

Catalogue HTTP handlers delegate to `api.catalogue` services and transaction-scoped
repositories in `api.repositories`. Source lists apply metadata permission before SQL
pagination; single-source reads use bounded lookups. Search and coverage scan permitted
metadata in 100-row windows, preserving the pilot substring-search contract without
loading a private whole corpus. Policy headers are checked before approved canonical
bytes are selected. The wire DTOs are deeply immutable and published in
`schema/public.schema.json` (`pilot-public-1`, serialization schema); JSON fields and
existing source URLs remain compatible. Target metadata paging uses only the eligible
view through `PolicyRepository`/`TargetCatalogue` and is not wired to pilot HTTP or cutover.

The HTTP dependency owns read-transaction lifetime; services/repositories never commit.
Corrupt/unavailable authority returns a safe 503, with no snapshot fallback or raw error
details. Legacy unpaged results have development safety bounds of 1,000 records and
10 MiB canonical text; oversized results return 413 without partial text. These are
prototype compatibility guards, not approved production capacity, transport-byte quotas,
or S2 launch evidence. Versioned pagination, lexical indexes and final emission checks
remain separate work. `api.release_adapter` isolates the existing filesystem/integrity
verifier; its whole-corpus validation remains until the release/publication slice.

Alternatively, `docker compose up --build` starts PostgreSQL 16, the API and the
built web app on the same ports. Stop native preview processes first. Compose
credentials are for local development only. The database has no published port.
No deployment to open.alvary.ai is performed by this repository.

## Repository tree

```text
schema/              Pydantic canonical contracts + generated JSON Schema
rights/              enums (in schema), fail-closed policy, public projection
ingestion/          adapter contract, parsers, normalization, ingestion pipeline
   catalogue/ng.json four metadata candidates; no imported legal text
api/                 FastAPI GET endpoints + SQLAlchemy store
migrations/          Alembic initial migration (PostgreSQL / SQLite)
exports/             immutable JSONL release builder + serving-time verification
releases/2026-10-05/ng/
   corpus/           rights-filtered release (metadata only in this pilot)
   metadata/         explicit metadata-only release
web/                 React + TypeScript + Vite public website
scripts/             seed, schema generation, database verification
tests/              policy, leakage, provenance, parser and release tests
.github/workflows/   tests, schema drift, PostgreSQL migration and web build
```

## Architecture and database

`catalogue → approved fetch → parse → normalize → rights decision → structure →
validate → persist in operator database → gated API/export`

Canonical models describe jurisdictions, authorities, languages, sources, rights
records, immutable source versions, hierarchical legal nodes, citation aliases,
amendment/cross-reference relationships and translation provenance. Tables retain
relational identities and core foreign keys, with a validated JSON payload for
extensible domain attributes. Secondary graph links and byte anchors are checked
by the corpus validator. This is a small-corpus store; production indexed search,
streaming projections and audit history are future work.

Raw-file and normalized-text SHA-256 values are separate. Nodes refer to source,
version, structural locator, optional page and UTF-8 byte offsets. Generic ingestion
creates a document root; jurisdiction-specific legal segmentation is not claimed.

Compatible *concepts* were informed by the existing Alvary schema documentation:
stable source identity, immutable versions, normalized UTF-8 anchors, explicit
currentness and ambiguous citation resolution. All implementation is new. No
proprietary code was copied.

## Rights decision flow

1. Missing record or `RESTRICTED` → no public record.
2. Explicit metadata publication basis → catalogue facts and official link only.
3. `OPEN` / `OPEN_WITH_CONDITIONS` is necessary but insufficient for full text.
4. Require redistribution, commercial reuse and derivative permissions; reviewer,
   date, licence, holder, evidence and legal/database-rights notes; privacy clearance;
   attribution text where required; and satisfied recorded conditions.
5. Require approval of the **exact normalized content hash**, plus separate content
   verification of that version. Future-dated/expired reviews fail closed.
6. Apply the same projection before API search, retrieval, citations and every export.
   Serving a release also checks hashes and current rights; withdrawn, changed or
   invalid releases return HTTP 410 and disappear from the listing.

Rights records are public-facing records. Store privileged legal advice and private
reviewer contact details elsewhere. A licence review must ensure all declared
conditions can actually be fulfilled by the release format and downstream users.

## API

All public operations are GET:

- `/jurisdictions`
- `/sources` and `/sources/{id}`
- `/sources/{id}/versions` (cleared text and nodes only)
- `/search?q=...`
- `/citations/{citation}` (resolved / ambiguous / unresolved)
- `/coverage`
- `/rights/{source_id}`
- `/releases`
- `/releases/{date}/{jurisdiction}/{mode}/{filename}` (allowlisted artifacts)

Source lists and search accept `jurisdiction`, `access` (`all`, `open`, `metadata`), `limit` (maximum 100), and `offset`. Filtering precedes totals and pagination; the reader offers Previous/Next and resets pages when query/filter changes.
Use the generated `/openapi.json` for the deployed API contract.

## Releases and verification

```sh
# Fast checks are explicitly partial; full acceptance below requires PostgreSQL.
pytest --fast -q
ruff check .
ruff format --check .
python -m scripts.generate_schema
npm --prefix web run build
python -m exports.release --date 2026-10-06 --jurisdiction ng
python -m exports.release --date 2026-10-06 --jurisdiction ng --metadata-only
```

Each immutable release contains JSONL entity files, coverage, rights summary,
README and a SHA-256 manifest. Existing releases are never overwritten. Empty
versions/structure files in the pilot are intentional. Synthetic text exists only
in tests; it is not Nigerian law and is never part of the seed release.

## Implemented, limited and deferred

Implemented: schemas and reference checks; rights/publication gate; Nigeria
catalogue; manual/HTML/text-PDF parsing; OCR callback with explicit failure when
unconfigured; persistence and migrations; nine requested read-only endpoints;
search/citation resolution; responsive public pages; deterministic releases and tests.

Limited: generic root-only structure, operator-only ingestion, in-memory substring
search over database rows, one active rights record per source, hash-based version
deduplication. PostgreSQL is supported, while SQLite powers the lightweight preview.

Deferred: live source crawling, concrete OCR service, legal section parsers,
reviewer/admin workflow, append-only rights audit, incremental search indexes,
embeddings/pgvector, MCP, hosted tiers, signed releases, authentication and per-client quotas,
a persistent full-text deployment and exercised incident/withdrawal operations.
The metadata site is live on Vercel; contact is ijidai@ruhu.ai. Docker traffic controls
are available for that alternative profile, not the live Vercel deployment.

## Before full-text publication

Appoint rights and content reviewers; verify source-specific redistribution and
reuse evidence, editorial/database restrictions, privacy, attribution and licence
compatibility. Verify actual official document versions, consolidations, effective
dates, repeal relationships and extraction quality. Original metadata terms and
maintainer identity are approved. Complete review appointments, incident exercises,
monitoring, recovery and controlled full-text publication.
No seed's legal-text rights have been verified. Legal texts require documented source-specific permission and review before release.

## Licensing

The newly written software is provided under the MIT licence in `LICENSE`.
That licence **does not apply to legal source text, external publications, source
permissions or logos**. Data permissions are per record. Original project-authored catalogue metadata is CC BY 4.0 under
[DATA-LICENSE.md](DATA-LICENSE.md). This does not license linked legal texts.

## Pilot status

The public metadata site is https://open.alvary.ai. Full-text legal clearance is not
claimed. The [deployment runbook](deployment/runbook.md) covers deployment and operational
checks. Funding has not been awarded; planned capabilities are not deployed features.

## Manual Vercel deployment

Follow [the Vercel guide](deployment/vercel.md) to deploy the website and API together
from the repository root. This target is restricted to the immutable metadata pilot;
it requires no separate database. Production builds require recorded metadata licence
approval and configured public identity. Live hosting is not implied by configuration.

## Repository scope

This public repository contains the pilot implementation, public setup instructions,
licences and deployment guides. Internal design/specification documents and their
planning-only validation tools are maintained locally and excluded from public commits.

## Persistent configuration and PostgreSQL tests

Container profiles set `ALVARY_RUNTIME_MODE=persistent`. This requires an explicit
`DATABASE_URL` using `postgresql+psycopg` with a database name; it never falls back to
SQLite. The default `development` mode retains the pilot's local SQLite workflow.
The Vercel entrypoint remains an independent, metadata-only in-memory snapshot.

Run real database tests against a **dedicated disposable database whose name ends
in `_test`**. Its harness user needs permission to create schemas and to create,
assume and remove generated NOLOGIN roles for fence tests (the disposable PostgreSQL
container and CI administrator provide this). Application assertions run under those
restricted roles. Each test removes its generated schema/roles; never supply a production URL.

```sh
export TEST_DATABASE_URL='postgresql+psycopg://test_user:test_password@localhost:5432/open_alvary_test'
pytest -q -m postgres --run-postgres
```

CI requires this suite. Without `--run-postgres`, local runs explicitly skip these
tests; with the flag, missing or invalid test configuration fails. SQLite tests do
not establish PostgreSQL behaviour. The populated-upgrade tests exercise 0001 → 0002
with existing records. Restricted-role fence tests cover the migration boundary;
production policy role isolation and authenticated approval concurrency remain future work.

The `open_alvary.requests` logger emits JSON request events when INFO logging is enabled:
server-generated request ID, allowlisted HTTP method, status and time to response headers.
`X-Request-ID` correlates successful HTTP responses with these events. Request paths, query
strings, bodies, client addresses, credentials and exception text are not included by this
logger. Transport/server logging is separate; the production profile disables access logs.
This is diagnostic correlation, not an authenticated actor audit or delivery-byte receipt.
`/healthz` checks database access to the corpus table; it does not certify source clearance,
complete migration compatibility, backups or publication readiness.

Migration `0007` provides private original-synthetic candidate and snapshot proposals through
`rights.assembly.SyntheticAssembly` and `alvary-operator --assembly`. The separate
`schema/assembly.schema.json` contract (`assembly-proposal-2`) defines `record_synthetic_run`,
`record_snapshot`, `record_canonical_proposal` and `reconcile_assembly`. Existing review and raw-lifecycle commands keep their
contracts. The CLI uses the same explicit native PostgreSQL credentials, input and time bounds;
its responses contain IDs, hashes, revisions or counts, without proposal text or credentials.

Synthetic runs pin a qualified raw artifact, collection, exact assessment revision/hash/purpose,
original deadline and fixed original-synthetic producer/profile. Candidate IDs remain private
UUIDs; adapter IDs, hierarchy, region groups and cell proposals are local to a run. Literal text
(including Unicode controls and whitespace) is preserved. Physical pages and geometry require
explicit unavailable reasons; candidate quality stays unassessed. Snapshot edits require both
content and acquisition assignments, select one candidate per declared region, resolve every
alternative with reasons, and order each selection once. Geometry-based overlap detection,
composed corrections, extraction qualification and publication are later work. Proposals mint
no public versions, nodes or anchors and carry no approval status.

Candidates are immutable; snapshot revisions append with an exact current parent and monotonic
head. Candidate text/payload hashes and snapshot payload checks have database integrity checks.
Snapshot manifest hashes bind source/assessment/purpose/deadline, run/profile, selected candidate
hashes, editor, revision, parent and proposal body; parent hashes also have a relational FK.
These PostgreSQL JSONB manifests are private proposal hashes, not OA-text-1 canonical bytes.
Native writes and bounded private reads lock/recheck current derive and retain authority before
loading content, and preserve caller transactions. Identical currently eligible retries reuse
one receipt; changed requests conflict. Snapshot/run receipts have a separate assembly namespace.

The declared synthetic collection limit now accounts for raw bytes and all retained candidate/
snapshot payloads, including held or expired material. Bounds are 64 KiB per command, 50 candidates
per run, four runs and 128 total candidate/snapshot records per raw artifact, 2 MiB of live derived
payload per artifact, and 32 revisions per snapshot. Candidate reads return at most 50 rows.
`reconcile_assembly` scans at most 100 raw-artifact IDs with a UUID cursor and reports newly changed
payload counts. Restart from a null cursor on later passes. It can hold stale derivatives and
erase rejection/deadline-required payloads; it does not change the raw artifact's state.

Existing raw hold/erasure materialization and scoped raw erasure atomically propagate to the
bounded derivative family. Removing raw bytes therefore also removes every stored candidate/
snapshot payload, including prior revisions. Direct SQL cannot mutate payload/lineage or bypass
this guarded audit path. Negative cleanup remains possible while migration writes are fenced;
held/erased proposals never resume automatically. Immutable private lineage, hashes, bridge IDs
and audit metadata remain. PostgreSQL history/WAL/backup and independently retained copies are
outside this row-erasure boundary. Empty isolated `0007` rollback restores the exact `0006`
validator and raw state; populated proposal or receipt history requires forward repair.

Migration `0008` adds bounded private OA-text-1 assembly from an exact current synthetic snapshot.
`record_canonical_proposal` accepts a structural plan referencing selected candidates; independent
Python and PostgreSQL serializers must agree on UTF-8 bytes and projection metadata before storage.
They apply NFC and LF normalization, preserve literal whitespace, controls and hyphenation, and
record candidate spans, normalization hashes and deliberate separator offsets. Tables preserve
row-major positions, merged origins and empty cells; unavailable positions remain explicitly
incomplete. Explicit furniture and matching repeated-header exclusions account for omitted leaves.
Notice containers retain private byte ranges; footnotes follow first-marker order within their
scope, followed by unreferenced notes in snapshot order. No words or physical geometry are supplied
by the assembler. These mechanics do not establish source accuracy or legal approval.

Each proposal binds the snapshot revision/hash and fixed serialization profile/hash. Native
content and acquisition assignments plus current derive/retain authority are required to write;
private reads recheck current input and snapshot head. Snapshot edits hide old output immediately;
reconciliation can materialize conservative holds. Output is limited to 128 KiB and stored plan/
projection metadata to 256 KiB, within the existing shared collection and 2 MiB artifact budget.
Canonical records also count toward the 128-record artifact limit. Parent and derivative cleanup
now clears canonical bytes and metadata atomically while retaining private hashes and audit history.
Empty isolated `0008` rollback restores exact `0007` helpers; populated canonical history requires
forward repair. Public versions, anchors, text routes and atomic independent approval remain future
work; all canonical projections carry `publication_eligible=false`.

Migration `0009` adds private approval of **original synthetic fixtures only**, using the separate
`schema/approval.schema.json` contract (`synthetic-approval-2`) and `alvary-operator --approval`.
It exposes `record_snapshot_review` and `approve_synthetic_version`; it adds no publication command.
An administrator explicitly declares an expression/manifestation/raw-artifact binding through
`rights.approval.declare_synthetic_binding`, referencing existing identified work/expression and
manifestation records with exact collection, digest, size and work-manifestation relationships.
It creates no identities, observations, retrieval facts, legal editions or authenticity claims.
Bound identity facts become immutable; correcting them requires a separate forward workflow.

The current collection content reviewer records an exact source-comparison attestation with private
scoped evidence, binding/snapshot/profile/content hashes and every selected candidate, including
exclusions. The release maintainer must have native release and acquisition assignments and recheck
two distinct current rights/content reviewers, six material coverage classes, collection permission,
source authority and the exact reviewed proposal. The database reconstructs bytes and structure
again; incomplete material content cannot be approved. This initial slice supports complete human
source comparison under collection-policy inheritance and no-personal-data authority. Per-version
exceptions, sampled-native qualification, personal-data derivatives and actual qualified appointments
require later work. Synthetic attestations do not certify real reviewer competence or source accuracy.

One transaction keyed by expression plus approved snapshot hash mints a private approved version,
representation, version-owned nodes/anchors, ordered leaves, table/cell links, footnote targets and
whole-candidate normalization alignment. Physical geometry remains explicitly unavailable; header
semantics and accessible table quality remain unverified. Current expression and per-version
representation pointers use exact revision checks. Concurrent retries reuse the committed record
set. A header-only representation revision can preserve bytes, nodes and anchors; changes to bytes,
canonical order, spans or grid/node identity require a new version. Historical records stay immutable.
Public identity resolution, text APIs, exports and preferred public version selection remain disabled.

Authority is checked before loading private input, under source/reviewer/head locks, and again by a
deferred constraint at caller commit. Expiry or an intervening snapshot edit before commit rolls back
minting, audit and receipt together. Completed versions own their frozen bytes independently of later
staging edits; their private reads still require current source/binding and exact collection authority.
Superseded/revoked authority hides bytes immediately, with reconciliation materializing conservative
holds. Held versions do not resume automatically; fresh restoration and exception workflows remain
unimplemented. Native callers have no direct grants on the new private tables.

All new root payloads and canonical bytes enter the existing shared collection/2 MiB artifact budget
before storage. Identity bindings, source reviews, versions and representation revisions count toward
128 root records per artifact, including erased history. Child records are separately bounded to
200 nodes/anchors per version, 50 source cell groups per representation and 100 footnote markers
per notice/document scope; a representation
has at most 256 KiB of metadata and 32 revisions per version. Row erasure clears binding/review/version/
representation payloads and version bytes atomically with existing raw/candidate/snapshot/canonical
cleanup. Textless private IDs, hashes, offsets, evidence and audit history remain; WAL, backups and other
copies are outside this boundary. Empty isolated `0009` rollback restores exact `0008` helpers;
any new binding/review/receipt/version history requires forward repair. All outputs remain
`publication_eligible=false` and the public pilot still has zero cleared full-text sources.

Migration `0010` adds private correspondence for **original synthetic gazette fixtures only**, through
`schema/gazette.schema.json` (`synthetic-gazette-2`) and `alvary-operator --gazette`. The separate
commands are `record_gazette_item_review` and `approve_gazette_item_correspondence`. An administrator
uses `rights.gazette.declare_synthetic_item_binding` to reference an existing identified item work/
expression, its association with the issue manifestation, and a complete issue-owned root notice.
The declaration creates no identities, versions, nodes, anchors or source facts. It requires separate
issue/item collections, matching language/jurisdiction and an original item expression. Judgment,
gazette and other item classes, nested notices, multiple intervals, translations and per-version
exceptions are outside this bounded workflow. Item identity facts become immutable once bound.

A current item content reviewer explicitly identifies one whole notice by its exact UTF-8 range/hash,
issue version, representation record-set hash, identity binding and serialization profile. Approval
requires release authority for both collections, acquisition assignments for both, and independent
current item rights/content review with retention permission. The existing issue source and approval
checks also apply. Private reads require acquisition authority over the issue and every declared
item collection. Any pending, stale or withheld declared item denies the dependent issue read and
all its item projections. This conservative whole-issue gate prevents another projection from
bypassing the issue owner. Each projection returns an exact substring with its existing issue-owned
anchor; no projected words or item version bytes are retained separately. Public text stays disabled.

Correspondence heads use exact previous revisions; same-command retries require unchanged actor,
intent and current authority. A representation edit can commit under the issue's own private
approval checks, while dependent reads stay denied until a fresh item review/correspondence pins the
new representation. Superseded or held correspondence cannot resume via retry. Source/reviewer/head
locks and deferred checks at caller commit protect review, mapping, audit and receipt together.
All new tables are private under forced RLS, with no direct runtime table grants. The older private
issue-read function alias loses its runtime grant so it cannot bypass item withholding.

Bindings, reviews and correspondence payloads share the issue collection allocation and existing
128-root/2 MiB artifact limits, including retained history; item collections acquire no duplicate
byte allocation. Additional bounds are 50 declarations per issue version, 32 correspondence revisions
per binding, one complete nonempty notice interval, 64 KiB commands/reviews and 8 KiB binding/
correspondence payloads. Shared limits can reject work before individual caps. Raw/due erasure clears
all new root payloads atomically; private identifiers, hashes, offsets and audit history remain.
WAL, backups and independently retained copies remain outside row cleanup. Empty isolated `0010`
rollback restores exact prior function bodies/grants and trigger bindings; new correspondence history
requires forward repair. Existing migrations `0001`–`0009` remain unchanged. This synthetic workflow
supplies no real identity, reviewer qualification, legal approval or publication authority.


Applied migrations `0001`–`0010` remain immutable. Migration `0011` establishes the maintenance path
in `rights/sql/v0011`: one reviewed SQL file per installed function, with pinned bundle, vocabulary,
body and definition hashes. PostgreSQL compiles each definition under `check_function_bodies=on`;
upgrade/downgrade preserve function OIDs, ownership, ACLs and trigger bindings. Unknown installed
edits or unsafe guard/reader grants require boundary review. Keep each applied resource revision
unchanged and add a new resource/migration revision for future SQL changes. To prepare one, point
`DATABASE_URL` at an isolated `*_test` database migrated to the previous head and run
`python -m scripts.sql_bundle init v0014 'function(argtypes)'`. Edit the generated `<function>.sql`
(`prior_<function>.sql` records the installed definition), then run `python -m scripts.sql_bundle pin v0014`.
It compiles each edit in a rolled-back transaction, rewrites it into PostgreSQL's normalized form if needed,
writes `manifest.json` and prints the `RESOURCE_HASH` for the new migration. A revision that creates
functions first records its DDL with `python -m scripts.sql_bundle new v0014 journal.sql 'fn(argtypes)'`;
`pin` runs that DDL before compiling replacements, and the verifier discovers every pinned bundle. Historical Python SQL
is retained for reproducible older installs. New grant capabilities belong in the declarative
`rights/sql/provision` inventory; provisioning validates the installed objects before creating roles.
Those grant files are hash-pinned by `rights.capabilities.PROVISION_HASH`, so a grant edit needs a
deliberate, reviewed pin update. `scripts.verify_sql_resources` also requires every policy function
to belong to the reviewed inventory, share one owner (the guard once provisioned), and grant
EXECUTE only to that owner or the namespace capability groups, never PUBLIC.
`schema/vocabularies/v1.json` supplies permission/material vocabulary, with mandatory drift checks
against contracts and frozen migrations. Independent Python/SQL OA-text-1 algorithms stay separate;
frozen, hand-calculated UTF-8 vectors test both without changing the canonical profile.

Private canonical rejection results now expose allowlisted structural `reason_code` values through
`assembly-proposal-3`. They include grid coverage, marker UTF-8 boundaries, candidate reuse,
exclusion mismatch and independent serializer disagreement; no source words, IDs or SQL diagnostics
are echoed. The compiler separates grid validation, marker mapping and footnote emission.

Encrypted backups use `python -m operations.backup` (`create`, `restore-verify`, `purge`); see
`deployment/runbook.md`. Archives are encrypted to a public OpenPGP key, restore verification only
targets empty `_restore`/`_test` databases, and storage, key custody, retention and alerting are
explicit operator settings without defaults. The backup tests need PostgreSQL client tools no older
than the server; set `ALVARY_TEST_PG_TOOL_PREFIX` (and `ALVARY_TEST_PG_TOOL_HOSTPORT`) to run them
through a version-matched container.

Migration `0014` adds SYS-07 step 1: withdrawing a previously approved operation, suspending
metadata or erasing staged bytes commits a pending `policy.denial_delivery` record with a
publication revision from one locked allocator row, in the same transaction. Records are
append-only except the pending-to-durable receipt. `rights.journal_delivery` delivers them in
revision order to an `operations.journal` backend that assigns gapless global sequences, returns
the original receipt on retry, rejects reused IDs with different payloads and hash-chains every
record. `FileJournal` is a reference backend; `tests/journal_conformance.py` holds the drills a
production journal (D7) must pass. Migration `0015` gates policy-governed metadata serving on an
open serving epoch bound to the database instance (system identifier and database OID), so restored
or cloned databases serve nothing until `rights.recovery` replays the journal and either verifies
continuity or applies a conservative reset that only fresh reviewer decisions can lift.

Migration `0016` and `api.admission` implement ACS-05 anonymous admission: PostgreSQL token
buckets refilled with database time (clock rollback adds and removes nothing), per-identity and
global concurrency leases locked in a stable order in one short transaction, no debit on rejection,
and Retry-After from the longest blocking wait. Clients are grouped by IPv4 address or IPv6 /64 taken
only from the configured trusted-proxy chain and HMAC-pseudonymized; the previous secret's bucket is
also debited during rotation. Requests blocked only by the global ceiling wait in a bounded ingress
queue, then receive `503 capacity_exhausted`; a quota-store failure is `503` without corpus content.
Enforcement is enabled only by `ALVARY_ADMISSION_CONFIG` (a JSON file; see `deployment/runbook.md`),
which must state every allowance and satisfy the connection budget P <= C - R - O. No default
allowance exists; D3 must measure and approve the values before text launch.

Migration `0017` and `rights.bytes` add the BUD absolute-byte ledger: per-scope capacity, held and
spent bytes (available = C - H - S; refill only reduces S), reservations charged atomically across
every scope and idempotent per admission, and a compare-and-swap state machine (reserved →
emitting → settled, reserved → released, emitting → ambiguous). Only provably unsent bytes are
released; a stale or expired reservation cannot begin emission; `reconcile_bytes` (maintenance,
with D3 fence and conservative bounds and no defaults) releases expired unsent reservations, marks
fenced expired emissions ambiguous and settles them at every reserved byte unless durable
transport evidence settles them first.

Responses that can carry text (`/sources/{id}/versions`) and release downloads are buffered and
rechecked in a fresh transaction immediately before the first byte is sent (`api.emission`): content
withdrawn after lookup returns `409` (text) or `410` (release) with no body bytes, and a failed policy
read returns `503`. Bytes already handed to transport cannot be recalled; a withdrawal committed after
the check applies from the next request. With admission configured, text responses reserve their
uncompressed bytes against the identity and global byte budgets before the check, begin emission
durably, and settle at the payload length; without `byte_limits` such responses are refused (`503`)
because missing byte limits block text launch. Single responses are capped at 10 MiB.

Full acceptance runs PostgreSQL by default. Supply an isolated `TEST_DATABASE_URL` ending in `_test`
whose maintenance account can create disposable databases and roles, then run `pytest -q`.
`pytest --fast -q` explicitly skips PostgreSQL and cannot be treated as acceptance. Most tests clone
an owned, unseeded session database migrated once, preserving all qualified function/trigger OIDs;
roles, seeds, appointments and transactions remain per test. Marked fresh-migration cases exercise
empty/older schemas, and `--fresh-postgres` forces fresh migration for every case. CI runs fast and
mandatory PostgreSQL steps separately and verifies installed SQL definitions. Tests create/remove
only their random `oa_*_test` databases; the configured test database and other projects are untouched.

Migration `0012` adds an expression GIN/trigram index over normalized ASCII catalogue metadata.
SQL counts/orders/pages confirmed matches; Unicode and eligible full-text candidates retain exact
Python checks with a declared 10,000-candidate fallback bound. Rights are checked before text loads.
There is no retained search copy of private canonical text. An older schema keeps the bounded
legacy fallback while upgrading. `pg_trgm` is a standard PostgreSQL extension; downgrade removes
only an extension introduced by this migration and refuses changed ownership/dependencies.
Same-day release IDs such as `2026-10-07.1` and `.2` are accepted, alongside unchanged date-only links.
Each revision is immutable and current withdrawal checks still apply to listings and downloads.

Synthetic appointments and source bindings remain a development boundary. A production path requires
a separately reviewed identity/provider profile and migrations, current source/rights/privacy/reviewer
qualification, denial-journal/restore evidence and serving-role adoption. Synthetic rows/bytes cannot
be relabelled or backfilled as qualified real approvals. Revoke the synthetic RPC grants from
production groups when the qualified replacement is adopted; retain old immutable evidence and
isolate fixtures in test environments. No flag activates real appointments or first-text publication.


Migration `0013` gives new private representation records the `rep_` UUIDv4 prefix.
Existing `prp_` identities, references, lifecycle history and exact retries remain valid;
new `prp_` inserts are rejected by the database, including through historical RPC aliases.
The private approval/gazette contracts advance to `synthetic-approval-2` and
`synthetic-gazette-2`, accepting both prefixes for historical references. Clients must
supply `rep_` for new representations. Applied migration and SQL resource bundles remain
unchanged; `v0013` pins the reviewed function replacement and the new insert guard.
Rollback is refused once any `rep_` history exists, including erased history.

Native original-synthetic conformance combines one issue with three independently identified
item works/expressions and distinct item policies. Tests verify NFC/LF, Arabic/bidi/non-BMP
bytes, merged and empty cells, notice-scoped footnotes, repeated-header exclusion,
representation versus byte correction, shared allocation, scoped native sessions, transaction
rollback, conservative sibling withholding and whole-family row erasure. Incomplete material
tables cannot acquire approval. Sampling, personal-data/exception release routes, qualified
real-source review, public serving and operational recovery remain separate gates.
