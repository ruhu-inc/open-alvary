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
A restored database must stay closed to serving until journal replay and reconciliation from that
checkpoint exist and pass; that recovery workflow is not yet implemented.

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
