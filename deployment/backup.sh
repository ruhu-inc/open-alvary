#!/bin/sh
# Run from the repository root with the host's scheduler (daily). Creates an encrypted backup in
# Cloudflare R2, then applies the configured retention. Settings live in deployment/.env.
set -eu
umask 077
compose="docker compose --env-file deployment/.env -f deployment/compose.yaml -f deployment/backup.yaml --profile backup"
set -a
. deployment/.env
set +a
: "${BACKUP_REMOTE:?Set BACKUP_REMOTE, for example r2:open-alvary-backups/production}"
$compose run --rm backup create --recipient-file /run/keys/recipient.asc --remote "$BACKUP_REMOTE"
if [ -n "${BACKUP_KEEP_DAILY:-}" ] && [ -n "${BACKUP_KEEP_WEEKLY:-}" ]; then
  $compose run --rm backup purge --remote "$BACKUP_REMOTE" \
    --keep-daily "$BACKUP_KEEP_DAILY" --keep-weekly "$BACKUP_KEEP_WEEKLY"
else
  echo "WARNING: retention not configured; no backups were purged" >&2
fi
