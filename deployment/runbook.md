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

`deployment/backup.sh` runs `operations.backup` in a dedicated image with PostgreSQL 16
client tools and GnuPG (`deployment/backup.Dockerfile`, `deployment/backup.yaml`). Each run
dumps the database, validates the archive catalogue, encrypts it to the configured **public**
OpenPGP key and writes the archive and then its metadata sidecar (the completion marker)
atomically to `BACKUP_DESTINATION`. The plaintext dump exists only in the job's private
temporary directory. Nothing is pushed anywhere else: mount separate, off-host storage at
`BACKUP_DESTINATION`, writable by the image's `postgres` user (uid 70).

These are operator decisions with no defaults; record who made each one:

- Off-host storage location and its access controls (`BACKUP_DESTINATION`).
- Key custody: generate the key pair away from the backup host and keep the private key
  offline. `BACKUP_RECIPIENT_FILE` must contain only the public key; a secret key is refused.
- Retention: `BACKUP_KEEP_DAILY` and `BACKUP_KEEP_WEEKLY` (the earlier proposal was 7 and 4).
  Without both values the job warns and purges nothing.
- Failure alerting: `BACKUP_ALERT_COMMAND` runs inside the backup container with the
  arguments `backup-failed <stage>`. Without it, failures are only on the job's exit status.
- Scheduling with the host's approved scheduler, and the responsible operator.

Restore drill (before public launch, then on the agreed cadence): on a machine that holds the
private key, create an empty database whose name ends in `_restore` or `_test`, then run
`python -m operations.backup restore-verify <backup> --destination <dir> --identity-file <key>`
with `ALVARY_RESTORE_DATABASE_URL` set. Use `ALVARY_PG_TOOL_PREFIX` if the local PostgreSQL
client is older than the server. The command checks the archive and dump hashes, refuses a
non-empty or non-isolated target, restores without owners or grants, and verifies the
migration revision and every table. Record the backup name, timestamp and result; then run
migrations/seed and public smoke checks against that test deployment. Do not run the seed
smoke check expecting four sources once the corpus has expanded. Backup metadata records the
delivered denial-journal checkpoint (sequence, chain hash and pending count) read before the dump.
A restored database stays closed to serving until `rights.recovery recover` has run on it (below).

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

## Denial journal delivery

Every withdrawal of a previously approved operation, metadata suspension and staged-byte erasure
commits a pending `policy.denial_delivery` record in the same transaction (migration `0014`), and
takes effect immediately in policy. `python -m rights.journal_delivery --journal <dir>` (maintenance
`DATABASE_URL`) appends pending records to the independent journal in revision order and marks
them durable. Exit status 1 means denials remain pending: they still deny, but are not yet
durable outside the database. Run it after each withdrawal and on a schedule; alert on pending
records. The file journal is a reference backend: its location must be a separate failure domain,
and the production journal service, access control and encryption are decision D7.

The policy foundation currently binds synthetic actors to PostgreSQL role OIDs. A
database dump does not preserve authenticated subjects or recreate their role OIDs
on another cluster. Keep capability logins and serving stopped during a foundation
restore. Identity revalidation, role/grant reprovisioning and current denial-state
replay need their own implemented and tested workflow before restored policy can
be activated. A successful seed smoke check does not certify those boundaries.

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
