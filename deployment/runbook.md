# Public pilot deployment runbook

Use a Linux host with Docker Compose, persistent storage, and sufficient memory
(start with at least 2 GiB for this small composition; measure actual usage).
No host, domain, billing plan or DNS change is selected automatically by these files.

## Preflight

1. Publish the reviewed repository; confirm anonymous access and CI results.
2. Confirm metadata licensing in DATA-LICENSE.md; name a maintainer and monitored contact.
3. Copy deployment/.env.example to deployment/.env and fill every field. Generate a
   unique password using `openssl rand -hex 32`; the URL construction expects an
   alphanumeric password. Restrict the environment file to mode 600.
4. Export those values in a shell from your trusted secret manager, then run
   `python -m scripts.check_launch`. Do not source untrusted environment files.
5. Point the approved hostname to the host. Allow inbound 80/443; restrict administrative
   SSH separately. Do not expose the database or API ports. Verify provider log retention.

## Start

From the repository root:

```sh
docker compose --env-file deployment/.env -f deployment/compose.yaml config --quiet
docker compose --env-file deployment/.env -f deployment/compose.yaml up --build -d
python -m scripts.smoke_public https://open.alvary.ai
```

Substitute the actual approved hostname. Caddy automatically obtains TLS after DNS
and port reachability are correct. The one-shot migration service must succeed before
API starts. A failed health check blocks the web dependency at initial startup.
Containers restart after process failure; unhealthy state alone does not trigger repair.
An external uptime monitor must alert a named operator on `/api/healthz` failure.

Do not expose releases through an unguarded static bucket or CDN path. nginx forwards
all release requests through the API's integrity/current-rights checks. Avoid caching
API data and downloaded legal content because permissions can be withdrawn.

## Backup and restore exercise

Backups go to a dedicated **Cloudflare R2** bucket (owner decision, 2026-10-07). `deployment/backup.sh`
runs `operations.backup` in a dedicated image with PostgreSQL 16 client tools, GnuPG and rclone
(`deployment/backup.Dockerfile`, `deployment/backup.yaml`). Each run dumps the database, validates
the archive catalogue, encrypts it to the **public** OpenPGP key, uploads the archive and then its
metadata sidecar (the completion marker) and never overwrites an existing object. rclone reads the
R2 credentials from environment variables only; no rclone configuration file is written.

One-time setup (owner-delegated best practice):

1. In Cloudflare, create a bucket (for example `open-alvary-backups`) in a region/account separate
   from the database host, and an R2 API token scoped to that bucket only (Object Read & Write).
   Enable R2 bucket lock or a lifecycle rule as a second guard if desired; the job's own retention
   is 7 daily and 4 weekly restore points.
2. On a trusted workstation (not the server) run
   `scripts/backup_keygen.sh ~/open-alvary-backup-keys "Open Alvary backups <ijidai@ruhu.ai>"` and set a
   strong passphrase. Copy only `backup-recipient.asc` to the server (`BACKUP_RECIPIENT_FILE`).
   Store `backup-identity.asc` and its passphrase in your password manager, plus one offline copy.
3. Fill the backup section of `deployment/.env` (see `.env.example`) and schedule
   `deployment/backup.sh` daily, for example at 02:30 UTC with cron.
4. Failures run `deployment/alert_github_issue.sh`, which opens a GitHub issue (GitHub emails the
   maintainer). It needs a fine-grained token with Issues: write on this repository only.

Restore drill (before public launch, then on the agreed cadence): on a machine that holds the
private key, create an empty database whose name ends in `_restore` or `_test`, then run
`python -m operations.backup restore-verify <backup> --remote r2:open-alvary-backups/production --identity-file <key>`
with `ALVARY_RESTORE_DATABASE_URL` set. Use `ALVARY_PG_TOOL_PREFIX` if the local PostgreSQL
client is older than the server. The command checks the archive and dump hashes, refuses a
non-empty or non-isolated target, restores without owners or grants, and verifies the
migration revision and every table. Record the backup name, timestamp and result; then run
migrations/seed and public smoke checks against that test deployment. Do not run the seed
smoke check expecting four sources once the corpus has expanded. Backup metadata records the
delivered denial-journal checkpoint (sequence, chain hash and pending count) read before the dump.
A restored database stays closed to serving until `rights.recovery recover` has run on it (below).

## Point-in-time recovery (best-practice database protection, 2026-10-08)

Two independent protections run together:

| Layer | What it gives | Loss window |
|---|---|---|
| pgBackRest WAL archive (`deployment/pitr.yaml`) | Restore to any moment in the last 4 weeks | About 1 minute (`archive_timeout=60`) |
| Daily logical backups (`deployment/backup.sh`) | Independent, public-key-encrypted copy | Up to 24 hours |
| Denial journal (separate provider) | Withdrawals re-applied after any restore | None for committed withdrawals |

Setup:

1. Create a second R2 bucket (for example `open-alvary-pitr`) with its own bucket-scoped token.
   Enable an R2 **bucket lock** rule (for example 35 days) on both backup buckets so a compromised
   server cannot delete history; pgBackRest retention and the logical-backup purge only remove
   objects older than the lock.
2. Generate `PITR_CIPHER_PASS` with `openssl rand -base64 48`, store it in the password manager,
   and fill the `PITR_*` settings in `deployment/.env`. The server needs this key to archive WAL;
   anyone who controls the server already controls the live database.
3. Run the stack with the overlay (`-f deployment/compose.yaml -f deployment/pitr.yaml`), then
   `deployment/pitr.sh init`.
4. Schedule `deployment/pitr.sh full` weekly (Sunday 01:30 UTC) and `deployment/pitr.sh diff` daily
   (01:30 UTC). Failures open an `operations-alert` issue. `deployment/pitr.sh info` shows the window.
5. Host the denial journal (`ALVARY_JOURNAL_DATABASE_URL`) with a **different provider** from both
   this server and R2, for example Neon or Supabase, so one provider failure cannot affect both.

Restore drill (quarterly, and before launch): `scripts/pitr_drill.sh` runs a complete local drill.

Restore procedure:

1. `deployment/pitr_restore.sh "2026-10-08 14:30:00+00"` (omit the time for the latest state). It
   restores into a **new** volume, replays WAL, promotes and fences serving; the live volume is untouched.
2. Start a verification stack on that volume (`CORPUS_DB_VOLUME=<restored volume>`), then run
   `python -m rights.recovery restore-grants` if roles changed, and
   `python -m rights.recovery recover --point-in-time --reason "<incident>"` (replays the whole journal).
3. Before anything serves from it, start a new change-feed epoch:
   `python -m changes.admin new-epoch --reason "<incident>"`. Readers holding resume tokens are then
   told to resynchronise (409 `feed_reset`) instead of silently missing changes lost with the restore.
4. When satisfied, set `CORPUS_DB_VOLUME` to the restored volume in `deployment/.env`, restart the
   stack, deliver pending denials and open a serving epoch. Keep the old volume until the incident
   is closed.

## Public change log

Every write to the public catalogue records its changes (`changes/record.py`, migration `0023`;
contract in the specification's changes-contracts.md). With maintenance `DATABASE_URL` set:

- `python -m changes.admin status`: the feed epoch, baseline time, events and highest revision.
- `python -m changes.admin sweep`: records changes nobody wrote, such as an expired rights record.
  The deploy's seed step runs it; it should also run daily.
- `python -m changes.admin purge`: removes whole publication revisions older than 400 days.
- `python -m changes.admin new-epoch --reason "<why>"`: after any database restore (above), or if
  the cursor key is lost or exposed.

The cursor key is the `feed-keys` secret (`{"k1": "<base64 32 bytes>"}`), created by
`deployment/gcp/public_service.sh setup` and passed as `ALVARY_FEED_KEYS`, with
`ALVARY_FEED_KEY_ID` naming the key that seals new tokens. To rotate: add a secret version holding
both the old and a new key, deploy with `FEED_KEY_ID=<new id>` (it sets `ALVARY_FEED_KEY_ID`). Keep old keys:
readers' resume tokens sealed with them stay valid. If a key must go (exposure), remove it and start
a new epoch, so readers are told to resynchronise rather than refused.
Without the secret, a persistent deployment keeps the Changes page and the feeds but answers replay
requests with 503.

## Switching on activation B (GCP)

Accounts, the console, keys, REST v1, MCP and the closing of the anonymous API go live together
(MAC-15), after acceptance B passes on staging. With `.env` holding `AUTH0_DOMAIN`, `RESEND_API_KEY`
and, once Resend's webhook exists, `RESEND_WEBHOOK_SECRET`:

1. `python deployment/auth0/configure.py` (production URLs) writes `private-staging/auth0/*`.
2. `deployment/gcp/public_service.sh accounts` stores the secrets; the API-key pepper is made once
   and must never be replaced.
3. `ACCOUNTS=1 deployment/gcp/public_service.sh deploy` deploys the service with activation B and
   the worker jobs. Every later deploy must keep `ACCOUNTS=1`, or the console and keys go dark.
4. `deployment/gcp/public_service.sh api_edge`, then add its two DNS records for `api.<domain>`.
   Then `deployment/gcp/public_service.sh request_logs` (once): Cloud Logging stops storing Cloud Run's
   and the load balancer's request logs for REST v1 and MCP, so their queries are never kept (the
   privacy notice's promise; OA-A2). It changes the project's logging configuration, so nothing
   changes until it is run; a deployment from before 2026-10-18 needs it once. `status` lists the
   exclusion.
5. `deployment/gcp/public_service.sh schedule` (once): the worker every 5 minutes, the daily sweep.
6. In Resend, a webhook to `https://<domain>/console/mail-events` for `email.bounced` and
   `email.complained`; its signing secret into `.env`, then step 2 again and a deploy.
7. `deployment/gcp/public_service.sh audit_seals` (once): the log bucket that keeps the audit seals
   outside the database; lock its retention once the first seals are in it.
   Then `deployment/gcp/public_service.sh alerts` (once, `ALERT_EMAIL` in `.env`): an email when a
   scheduled job fails.
8. `ACCOUNTS=1 deployment/gcp/public_service.sh verify`.

Turning activation B off again is a deploy without `ACCOUNTS=1`: the anonymous API reopens and the
console, keys and connected apps stop answering; nothing is deleted.

## Publishing approved works (catalogue records)

`python -m operations.publish` takes works approved in the review workbench into the public
catalogue as records only (title, number, year, citation, status, issuer, official link); never their
text. Each run takes down records whose approval has lapsed. The change log records both.

1. Once per jurisdiction, the owner records the collection decision, then commits the file:
   `python -m operations.publish decide --jurisdiction rw --by "Full Name" --evidence <url>`
   (`ingestion/decisions/rw-catalogue.json`, 12 months by default; run again to renew).
2. With both databases reachable (for example two Cloud SQL Auth Proxy ports: the review database
   in open-alvary-ingestion, the public one in open-alvary-prod) and read access to the collection:
   `ALVARY_REVIEW_DATABASE_URL=… ALVARY_REVIEW_COLLECTIONS=gs://open-alvary-corpus/collections/rw/
   DATABASE_URL=… python -m operations.publish records --jurisdiction rw --dry-run`, then without
   `--dry-run`. The review database is only read.

Publishing text needs more first: the copyright check of official texts (Law N° 055/2024 for
Rwanda), a rights decision verified against the law itself (POL-02) and the public reader (MAC-01).

## Account deletion and retention

People export their data and delete their account in the console (Security); owners export an
organisation (Settings). Deleting revokes keys and apps and ends memberships at once; the profile and
sign-in identities are purged 30 days later by `python -m accounts.privacy purge` (in the daily job),
which first deletes the person's Auth0 user with the `Open Alvary account deletion` client
(`configure.py` creates it; `accounts` stores its secrets). Without that client, closed accounts wait
(`provider_pending` in the job's output) rather than losing the link to their Auth0 user. The same
purge applies the rest of the privacy notice's schedule: sessions and invitations after 30 days,
revoked keys and apps after a year, usage and audit entries after two years (whole UTC days, by the
database's clock).

## Audit log seals (MAC-13.7)

The daily job first seals every UTC day of the audit log that ended over an hour ago, and last
verifies them all: `python -m accounts.seals seal`, then `verify`. A seal covers the day's entries
(count, first and last sequence numbers, a SHA-256 over each entry) and the previous day's seal, so
a changed, removed or back-dated entry, or a missing day, fails the check. Seals are kept in
`accounts.audit_seal` (add-only, never purged) and each is logged as an `audit seal` line, which the
`audit-seals` log bucket keeps for 800 days (`public_service.sh audit_seals`).

- A failed check prints an ERROR line `audit seal check` naming the days and problems
  (`entries_changed`, `missing_seal`, `chain_broken`, `seal_altered`, `entries_before_first_seal`,
  `sealing_behind`), and the daily run fails. `public_service.sh alerts` (once, with `ALERT_EMAIL`
  in `.env`) emails that address whenever a run of `open-alvary-daily` or `open-alvary-worker` fails.
- On a failure: do not repair seals or entries. Compare the day's seal in the database with the copy
  in the `audit-seals` bucket, restore the day's entries from a backup to see what changed, and treat
  it as a security incident until explained.
- Days older than the two-year retention have no entries left; they verify through the chain.

## Monitors and webhooks

`python -m changes.worker run` (every 5 minutes, as a scheduled job) matches new changes to
monitors, sends digests, delivers webhooks, pauses monitors above a plan's limit and purges by
retention. After a new feed epoch, matching stops until `python -m changes.worker confirm-epoch`.

Webhook signing secrets are sealed with the `webhook-keys` secret (`{"w1": "<base64 32 bytes>"}`,
created by `setup`, passed as `ALVARY_WEBHOOK_KEYS` with `ALVARY_WEBHOOK_KEY_ID`). Losing it makes
every endpoint's secret unreadable: endpoints must then rotate their secrets. To rotate the sealing
key, add a version holding both keys and deploy with `WEBHOOK_KEY_ID=<new id>`; existing secrets stay
readable under the old key. Without the secret, webhooks are off (503) and the worker skips delivery.

## Security page and security.txt (MAC-13)

`/security` describes how accounts and data are protected and gives the vulnerability disclosure
policy; `/.well-known/security.txt` (RFC 9116) points to it, on the website's domain and, after
activation B, on the API's.

- Reports arrive at hello@ruhu.ai with "Security" in the subject. Acknowledge each within 3 working
  days, keep the reporter informed, and agree the publication date with them.
- Renew `EXPIRES` in `api/security_txt.py` every six months (next by 2027-03-31). A test in
  `tests/test_site.py` fails 30 days before it lapses; `public_service.sh verify` prints the live value.
- Keep the page true: a change to sign-in methods, second-factor rules, providers, retention or
  encryption updates `web/src/security.tsx` in the same commit. Its disclosure terms take effect
  with the legal pages (`APPROVED` in `web/src/legal.tsx`).
- `.github/workflows/security.yml` checks pinned Python dependencies (pip-audit), what ships to
  browsers (npm audit, high and above) and the whole history for secrets (gitleaks), on every push
  and weekly. On a failure, upgrade or replace the dependency; for a secret, revoke it first, then
  remove it. In the repository's settings, also turn on GitHub's secret scanning with push
  protection and Dependabot alerts (owner action).

## Serving epochs and restore recovery

Policy-governed public serving (migration `0015`; currently the `eligible_metadata` view, not the
legacy pilot API, which remains authoritative until cutover) requires an open serving epoch bound
to the exact database instance. A restore or clone is a new instance and serves nothing until
recovered. With maintenance `DATABASE_URL` set:

- First, keep the restored database network-isolated: `pg_restore --no-privileges` drops the
  migrations' `REVOKE ... FROM PUBLIC`, so privileged policy functions are executable by any role
  until `python -m rights.recovery restore-grants --journal <dir>` revokes PUBLIC access and
  re-applies the pinned grant bundles and guard ownership (capability roles must already exist on
  the cluster; otherwise provision them). Then run `python -m scripts.verify_sql_resources`.
- `python -m rights.recovery recover --journal <dir> --backup-metadata <backup>.json --reason <incident>`
  verifies the journal chain and backup checkpoint, evaluates continuity (every epoch since the
  backup cleanly closed, every pending denial journaled), replays denials since the checkpoint,
  raises the revision allocator floor, abandons copied epochs and rebinds the instance. The
  predicate inputs and decision are stored in `policy.recovery_record`.
- Always restore into a new database. An in-place restore keeps the instance identity; journal
  delivery and epoch opening then detect that the journal is ahead, fence serving and require
  `recover`.
- `conservative_reset` means continuity could not be proven: every collection stays closed until
  a rights reviewer records a fresh decision revision. No operator command reopens it.
- Then deliver pending denials, and `python -m rights.recovery open --journal <dir>` opens a new
  epoch (journal marker first). Before planned maintenance or shutdown, `close` fences new denials,
  requires none pending and journals the clean-close checkpoint. A database lost while an epoch is
  open (no close marker) always recovers through conservative reset.

Re-provision capability roles and revalidate operator identities before reopening operator access;
restore does not carry those boundaries over.

## Anonymous admission (ACS-05)

Set `ALVARY_ADMISSION_CONFIG` to a private JSON file to enforce admission. Every key is required and
is a D3 decision with recorded measurements; none has a default:
`policy_revision`, `capacity`, `refill_per_minute`, `identity_concurrency`, `global_concurrency` (G),
`lease_seconds`, `queue_size`, `queue_deadline_seconds`, `trusted_proxies` (hops whose
`X-Forwarded-For` entries are trusted; the edge must strip client-supplied values),
`hmac_secret` (64+ hex characters; optional `previous_hmac_secret` during rotation), and the
connection budget `db_connections` (C), `operator_reserve` (R), `other_workers` (O) and
`public_pool_size` (P, aggregate across `api_replicas` API instances), which must satisfy
P <= C - R - O; each instance opens at most P / replicas connections with no overflow. `ALVARY_POLICY_SCHEMA` selects the
policy namespace (default `public`). Without the file, the metadata pilot keeps its existing nginx
rate limit and no ACS-05 enforcement; that is not acceptable for text launch.

Optional `byte_limits` (`capacity` and `global_capacity` of at least 10 MiB, their
`refill_per_minute` values and `emission_lease_seconds`) enable text delivery under the BUD byte
ledger; they are D3/D18 decisions. Run `ByteLedger.reconcile(fence_seconds, conservative_seconds)`
under maintenance authority at least once per minute with the approved D3 bounds; there are no
defaults. Ambiguous deliveries are charged in full unless durable transport evidence settles them.

## Retention (owner-delegated D20 policy, 2026-10-07)

Run hourly under maintenance authority:
`python -m rights.retention --idle-bucket-hours 24 --reservation-days 90`. It deletes expired
admission leases, admission and byte buckets idle and fully refilled for 24 hours (never while a
lease or reservation references them) and released/settled byte reservations older than 90 days.
Denial-delivery records, journal events, command/release receipts and audit events are retained
indefinitely: they are minimal recovery tombstones and governance history with no personal data,
and must outlive every recoverable backup (35 days with 7 daily + 4 weekly restore points).
Backups themselves follow 7 daily and 4 weekly. The legacy request log keeps no client identity.

## Denial journal delivery

Every withdrawal of a previously approved operation, metadata suspension and staged-byte erasure
commits a pending `policy.denial_delivery` record in the same transaction (migration `0014`) and
takes effect immediately. `python -m rights.journal_delivery` (maintenance `DATABASE_URL`) appends
pending records to the independent journal in revision order and marks them durable. Exit status 1
means denials remain pending: they still deny but are not yet durable outside the database. Run it
every minute from the host scheduler and after each withdrawal.

Production journal (owner-delegated best practice, 2026-10-07): a **separate PostgreSQL database on
a different provider or host** from the corpus database, for example a small managed Neon or
Supabase instance. Set it up once as its administrator:

1. `python -c "from sqlalchemy import create_engine; from operations.journal_postgres import install; install(create_engine('<admin-url>'), writer_role='journal_writer')"`
   after creating a `journal_writer` LOGIN role with only that access.
2. Give the deployment `ALVARY_JOURNAL_DATABASE_URL` for `journal_writer`, with TLS (`sslmode=require`)
   and encryption at rest provided by the host. The table is append-only even for its owner.
3. The `--journal <dir>` file journal remains a reference backend for drills only.

## Monitoring and alerts (owner decision, 2026-10-07)

Alerts reach the maintainer (ijidai@ruhu.ai) by email through GitHub issues, so no mail server is
needed. Keep GitHub notifications for this repository enabled.

- `.github/workflows/monitor.yml` probes `https://open.alvary.ai` (homepage identity and
  `/api/healthz`) every 5 minutes. Two consecutive failures one minute apart open an `outage`
  issue assigned to the repository owner; recovery closes it with a timestamp.
- `.github/workflows/backup-freshness.yml` checks daily that a completed backup newer than 26 hours
  exists in R2. Configure repository secrets `R2_ENDPOINT`, `R2_READONLY_ACCESS_KEY_ID`,
  `R2_READONLY_SECRET_ACCESS_KEY` (a read-only token for the backup bucket) and the variable
  `BACKUP_BUCKET_PREFIX` (for example `open-alvary-backups/production`).
- Host jobs (backup failures) open `operations-alert` issues via `deployment/alert_github_issue.sh`.
- Both workflows run only once these files are on GitHub; GitHub may delay scheduled runs, and
  it disables scheduled workflows after 60 days without repository activity. Check the Actions
  tab monthly (or after any quiet period) and re-enable them if GitHub has paused them.
  Primary operator: the maintainer; name a backup operator before advertising any SLA.

## Batch reading of a collection (operator workstation)

Reading gazette PDFs is an offline operator job; the public service never calls Google, Azure or
the local models. Each Act is read three ways and reconciled into a private review pack:

- **Document AI Layout Parser** (Google project `open-alvary-ingestion`, processor
  `legal-corpus-layout-parser`, region `eu`): the primary reading. Pin
  `ALVARY_DOCAI_PROCESSOR_VERSION` (batch runs refuse an unpinned processor). Sign in with
  `gcloud auth application-default login`; no key file is used.
- **MinerU** (local, `--tier advanced`, MinerU2.5-Pro weights): an independent check run offline
  (`HF_HUB_OFFLINE=1`; on macOS `sandbox-exec` denies all network except loopback). Set
  `ALVARY_MINERU_KIT` to its `mineru-kit`; weight hashes are part of every reading's version.
- **Azure Document Intelligence** `prebuilt-layout` (resource `open-alvary-docintel-sc`,
  Sweden Central, S0): a second independent check. `AZURE_DI_ENDPOINT` and `AZURE_DI_KEY` live
  only in the git-ignored `.env`.

```bash
python -m ingestion.batch plan  --act <manifest id>   # pages to read and to bill; nothing is called
python -m ingestion.batch run   --act <manifest id>   # read, reconcile, rebuild the review pack
python -m ingestion.batch status                      # flags per reconciled Act
```

- Readings are cached by PDF SHA-256 and exact reader version under
  `private-staging/<collection>/readings/`; a rerun resumes and never bills a page twice. Changing
  the processor version, pages per request or MinerU weights re-reads deliberately.
- Billed pages (Document AI plus Azure) are counted before any call and refused above
  `ALVARY_BATCH_MAX_BILLED_PAGES` (default 500); raise it per run with `--max-billed-pages`.
- Document AI requests carry `ALVARY_DOCAI_PAGES_PER_REQUEST` pages (default 2) with
  `ALVARY_DOCAI_WORKERS` in parallel (default 8). Throttling and server errors are retried;
  a request past its deadline is not (it may already be billed) and fails the reading instead.
- A failed reader is isolated: the other readings are kept and the run log under `runs/` records
  the error. Exit status is non-zero until every Act has every reading.
- Reconciliation keeps Document AI's words; check readings only restore dropped list markers when
  they agree, and flag what they do not support. Review high flags (numbers, dates, words no check
  reading saw, conflicting markers) on every page before recording a content decision.

## Corpus ingestion jobs on Google Cloud (Rwanda, 2026-10-13)

A country's whole archive is read and prepared in the cloud, not on the operator's machine. The
sources are already in the private corpus bucket `gs://open-alvary-corpus` (EU), fetched by
`python -m ingestion.rwanda fetch --to-bucket`. Two Cloud Run jobs in `europe-west1`, one image
(`deployment/ingestion-jobs.Dockerfile`), both running as `scan-reader@…` (Document AI use; read
and write objects in the corpus bucket only); images are built from the commit by `image-builder@…`.

```bash
deployment/gcp/ingestion_jobs.sh plan      # what would be created; creates nothing
deployment/gcp/ingestion_jobs.sh setup     # APIs, registry, the two service accounts (owner runs it)
deployment/gcp/ingestion_jobs.sh deploy    # commit first: builds from `git archive HEAD`
deployment/gcp/ingestion_jobs.sh run ocr 2       # scanned pages: Google Enterprise Document OCR
deployment/gcp/ingestion_jobs.sh run layout 1    # the same pages: the Layout Parser (second reading)
deployment/gcp/ingestion_jobs.sh prepare 10      # every stored file prepared for review
deployment/gcp/ingestion_jobs.sh status
python -m ingestion.rwanda scan-progress         # files and pages each reader has read
```

- **Budget first.** The scan jobs read only the work list `work/rw/scan-reading.json`, written by
  `python -m ingestion.rwanda scan-worklist --approved-by … --max-pages … --basis …` from the
  survey (each scanned file once, largest first) with the operator's approval; no task reads past
  its share. List prices (2026-10-13): Enterprise OCR $1.50, Layout Parser $10 per 1,000 pages.
- **Two readings of every scanned page.** OCR gives each word with its place, so the page is laid
  out like a born-digital one; the Layout Parser's reading is aligned with it. OCR's confident
  words stand (the Gazette's own slips and numbering as printed); where OCR was unsure (broken or
  faint type), the Layout Parser's word is taken; every difference is flagged with both readings.
- **Quotas.** The Layout Parser `v1.6-pro` is limited to 15 pages a minute per project in the EU
  (about 10 achieved): 30,000 pages take about two days. Ask for more under IAM & Admin → Quotas
  ("online process document pages (EU)"). OCR is not the bottleneck.
- **Resuming.** Readings land in `readings/rw/ocr/` and `readings/rw/docai/`; a file already read
  is skipped, so a stopped execution is simply run again. A failed task is retried once.
- **Preparing.** `corpus-preparer` reads each stored file (pdftotext runs in the job's container),
  splits it into its instruments and language versions with both readings of its scans, and
  writes the review documents to `collections/rw/`: per document a reading, provenance and
  `<id>.document.json` (the details suggested from the text), per file an index, and a light
  `manifest.json` assembled from the indexes. A file is prepared again when the pipeline version
  (`rw_gazette.PIPELINE`) changes or a scan's reading arrives after it was prepared; a file that
  fails is logged (`failed …`) and the task goes on. Mirror the ledger and catalogues first:
  `python -c "from ingestion import rwanda, corpus_store; rwanda.mirror_state(corpus_store.CorpusStore())"`.
- **Memory.** Tasks have 4 GiB. OCR asks only for text, words and page sizes: unasked, the service
  returns an image of every page, which ran 2 GiB tasks out of memory on large issues.

## Review workbench (operator-only web app)

Reviewers check each reconciled Act page by page against the gazette image, correct it, resolve
flags and, with the approver role, approve the Act. It is separate from the public site and
never writes to the public corpus: an approved Act is exported (`/api/acts/<id>/export`) for the
audited policy commands to record.

- Database: `ALVARY_REVIEW_DATABASE_URL`, PostgreSQL in production (its own database; SQLite is
  for a local trial only). Every page save is a new immutable revision (author, time, comment,
  flag decisions); approvals and an activity log are append-only.
- Operators are added on the server only:
  `python -m workbench.admin add <email> --name "<name>" --roles reviewer,approver` (and
  `deactivate`, `list`, `enrol`). Roles: reviewer edits and checks pages; approver approves Acts;
  access_admin uses the Access section (below); one person may hold several.
- Sign-in on the web is OpenID Connect (Google by default): create an OAuth client of type *Web
  application* in project `open-alvary-ingestion`, consent screen *Internal* if the operators are
  in one Workspace, redirect URI `https://<host>/auth/callback`. Set `ALVARY_REVIEW_BASE_URL`
  (the public https address), `ALVARY_REVIEW_ALLOWED_HOSTS`, `ALVARY_REVIEW_OIDC_CLIENT_ID` and
  `ALVARY_REVIEW_OIDC_CLIENT_SECRET` (in `.env` only). Only active operators on the allow-list are
  admitted; email must be verified by the provider.
- Run behind TLS: `uvicorn workbench.app:app --host 127.0.0.1 --port 8090 --proxy-headers` with a
  reverse proxy or tunnel in front. Never set `ALVARY_REVIEW_DEV_LOGIN` on a reachable server (it
  answers only on loopback regardless).
- Sessions: HttpOnly, SameSite=Strict, Secure cookies; 1 hour idle, 12 hours maximum; every
  write needs the session CSRF token. Pages carry a strict Content-Security-Policy.
- Deploy with the overlay, on its own hostname behind the same Caddy edge:
  `docker compose --env-file deployment/.env -f deployment/compose.yaml -f deployment/workbench.yaml up -d`.
  Set `REVIEW_DOMAIN`, `REVIEW_DB_PASSWORD`, `REVIEW_COLLECTION_DIR` (host path of the reconciled
  collection, mounted read-only) and the OpenID Connect client in `deployment/.env`. Run
  `python -m workbench.prepare` on the operator machine first: the container cannot render page
  images. Add operators with `docker compose ... run --rm workbench python -m workbench.admin add …`.
- **On Google Cloud** (owner decision, 2026-10-10: GCP, project `open-alvary-ingestion`, EU):
  `deployment/gcp/workbench.sh plan` shows what is created; then `setup` (APIs, Artifact Registry,
  an EU bucket read-only to the app, Cloud SQL PostgreSQL 16 with backups and point-in-time
  recovery, Secret Manager secrets, a service account with only Cloud SQL client, bucket read and
  its two secrets), `upload` (reconciled readings, the positional reader's output and page images;
  never PDFs, keys or other readings) and `deploy` (Cloud Build image, Cloud Run scaling to zero,
  not public, Identity-Aware Proxy in front, then the app's own operator sign-in). Add operators
  with `deployment/gcp/workbench.sh operator <email> "<name>" reviewer,approver`, which both
  allow-lists them in the app and grants them through Identity-Aware Proxy. `.gcloudignore` keeps
  private staging, `.env` and databases out of every build upload, and the image is built from
  `git archive HEAD` by `image-builder@…` (run `deployment/gcp/ingestion_jobs.sh setup` once
  first). Cloud SQL's own backups cover this database on GCP.
- The deployed workbench also reviews the corpus collections in the bucket
  (`WORKBENCH_CORPUS_COLLECTIONS`, default `gs://open-alvary-corpus/collections/rw/`, read-only):
  it lists tens of thousands of documents from the light manifest (searched and paged on the
  server), fetches a document's reading and details when it is opened, and draws page images on
  demand with pdftoppm in its own container (`deployment/workbench.Dockerfile`, Liberation and
  DejaVu fonts); downloads and images are cached in `/tmp`, kept within `ALVARY_REVIEW_CACHE_MB`.
  The service has 2 GiB.
- Back up the review database (every correction and approval) with the same encrypted R2 job,
  under its own prefix: `python -m operations.backup create --kind review --remote r2:<bucket>/review
  --recipient-file …` with `ALVARY_BACKUP_DATABASE_URL` pointing at `review_db`; verify with
  `restore-verify` into an isolated `*_restore` database as for the policy database.
- Approval requires every page saved as checked, every high flag resolved, no page whose machine
  reading changed since it was checked, all check readings present, the six verification checks
  confirmed and a rights basis. Any later edit makes the approval out of date (shown in the app
  and in the export).

## Operator access: the workbench's Access section (MAC-11)

Operators with the `access_admin` role answer access requests, grant, change and revoke access,
suspend and restore organisations and people, restore an owner to an organisation no active person
owns (with the evidence checked), revoke a leaked key or a connected app by ID, and read usage and
the whole accounts audit log, all at `#/access` in the review workbench. Every change takes a
reason (and evidence where it rests on something checked) and is in the accounts audit log under
the operator's email; organisations see it as "Open Alvary". The same actions are on the command
line: `python -m accounts.operator --operator <email> …` (`--help` lists them).

Every change also needs a passkey check in that session within 10 minutes, verified by the
workbench itself. A first passkey needs a one-time enrolment code from the server, so a taken-over
Google account cannot register one; a further passkey needs a passkey check.

Setting it up (after activation B is live):

1. `deployment/gcp/public_service.sh access_role` (right after a deploy, from the same commit):
   the database role `open_alvary_access`, with a generated password in `access-role-password`. It
   reads no key hashes, session tokens or provider subjects, and writes only what the actions
   change.
2. `deployment/gcp/workbench.sh access`: stores the workbench's connection (`access-database-url`)
   and the mail key (`resend-api-key`) in the ingestion project, and prints the one IAM grant the
   owner runs: Cloud SQL client for `workbench-runner` in `open-alvary-prod`.
3. `ACCESS=1 deployment/gcp/workbench.sh deploy`. Every later workbench deploy keeps `ACCESS=1`.
4. Per operator: `deployment/gcp/workbench.sh operator <email> "<name>" reviewer,access_admin`,
   then `deployment/gcp/workbench.sh enrol <email>`, which prints a code valid for 24 hours, once.
   Give it to the operator yourself; they register a passkey (or security key) at `#/access`.

A lost passkey: the operator removes it at `#/access` after checking with another of theirs. If
they have none left, check who is asking, then `deployment/gcp/workbench.sh enrol <email> --replace`
revokes their passkeys and issues a new code (a code is accepted only while no passkey remains).

## Workbench operators: who may use the workbench

Administrators (role `admin`) manage operators on the workbench's **Operators** page: add a person
by the Google account they sign in with (a Workspace address or a personal one such as Gmail), set
their roles (reviewer, approver, admin, access_admin), deactivate or reactivate them, and issue the
one-time code an administrator or access administrator needs to register a passkey (shown once, on
the page). Every change needs the administrator's own passkey within the last 10 minutes and is in
the workbench's audit log; nobody can deactivate themselves, and the last administrator keeps the
role.

The first administrator is set up from the command line:
`deployment/gcp/workbench.sh operator <email> "<name>" reviewer,approver,admin`, then
`deployment/gcp/workbench.sh enrol <email>` for their code.

Reaching the workbench also takes Google's IAP gate. Rather than a grant per person, let one Google
group through (set up once):

1. In the Google Workspace admin console, allow groups to have members outside the organisation
   (Apps > Google Workspace > Groups for Business > Sharing settings) if anyone uses a personal
   account. Create the group (for example `workbench-operators@ruhu.ai`); in its settings, allow
   members outside the organisation. A group is one address: members keep their own accounts.
2. `deployment/gcp/workbench.sh iap_group workbench-operators@ruhu.ai`; put
   `ALVARY_REVIEW_IAP_GROUP=workbench-operators@ruhu.ai` in `.env` and deploy again
   (`ACCESS=1 deployment/gcp/workbench.sh deploy`), so the Operators page names the group.
3. For accounts outside the organisation (Gmail): Google's own IAP sign-in admits only the
   organisation's accounts. `deployment/gcp/workbench.sh iap_external` makes IAP use the workbench's
   OAuth client instead; then, in the console (APIs & Services > Credentials and the OAuth consent
   screen, project `open-alvary-ingestion`), add the redirect URI it prints to that client and set
   the consent screen's audience to External (it asks only for name and email, which needs no
   verification by Google).

Adding someone is then: their account into the group, and the person on the Operators page.
Removing them: deactivate on the page and take them out of the group.

## Upgrade and rollback

Record the deployed commit and image IDs. Take and validate a backup before migration.
Run CI and clean-container checks for the proposed revision. Deploy immutable reviewed
revisions; do not edit code inside running containers. Verify `/api/healthz`, coverage,
search and download hashes. If a code-only regression occurs, redeploy the previous
known-good revision when schema-compatible. Never blindly downgrade a live schema.
For destructive migration failure, stop writes and plan restoration from the verified
backup with the operator; this requires an explicit data-loss assessment.

## Rights complaint or incident

Do not publish private evidence. The maintainer first records the affected source/version
and severity in a private operations channel. Suspend public visibility or exact-hash
approval through a controlled, validated rights update. API projections take effect on
new requests; old bundles become unavailable. Verify source, search, citation and release
routes all deny the affected data. Do not delete historical evidence to hide an error.
If the scope is uncertain, stop public API distribution while reviewing. Previously
downloaded copies cannot be recalled. Publish a suitable correction/withdrawal notice.

## Local clean-container verification

Run `scripts/container_smoke.sh` from the repository root with the project's Python
environment on PATH. It generates a disposable environment file and unique Compose
project, exposes web on an assigned loopback port, verifies the metadata-only pilot,
then removes that project's containers and volumes. Existing projects are untouched.
It starts only `web` and its dependencies so Caddy does not request a real certificate.
This validates the production images and internal routing, not live DNS/TLS or an
actual hosting provider.

## Public service on Google Cloud (activation A, MAC-12, D7)

`deployment/gcp/public_service.sh` puts the website and its data path behind one Google load balancer
(the "one front door"): one Cloud Run service (`api.service` with the built website,
`deployment/public-service.Dockerfile`), Cloud SQL PostgreSQL 16, Cloud Armor on the website paths,
and a certificate for `open.alvary.ai` by DNS authorisation. The service has no default address and
accepts traffic only from the load balancer; the visitor's address is the entry the load balancer
appends to `X-Forwarded-For` (`trusted_proxies` 2, confirmed by `verify`).

1. `plan` prints the resources, the required settings (`PUBLIC_REPOSITORY_URL`, `PUBLIC_MAINTAINER`,
   `PUBLIC_CONTACT_EMAIL`, `METADATA_LICENCE` in the environment or `.env`) and the monthly estimate.
2. The owner runs `project` (`open-alvary-prod`, and later `open-alvary-staging` with `GCP_PROJECT`, billed with the ingestion project's account) and
   `setup` (APIs, registry, service accounts, database, secrets with fresh HMAC secrets).
3. `deploy` builds from the commit, runs launch checks, migrations and the seed as a Cloud Run job,
   and deploys the service; `edge` creates the load balancer and prints the DNS records.
4. The owner adds the certificate's DNS record; `status` shows the certificate becoming active.
5. `verify` runs the MAC-15 acceptance A checks through the load balancer's address before any visitor
   moves: pages, `no-store`, credential and page-26 refusals, the old download redirect, no CORS, a
   forged `X-Forwarded-For` landing in the caller's own bucket, and no default service address.
6. Cutover: the owner points `open.alvary.ai` at the load balancer (`dns` prints the record) and the
   Vercel deployment is switched to a redirect to `https://open.alvary.ai` (MAC-02).
