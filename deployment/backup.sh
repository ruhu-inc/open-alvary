#!/bin/sh
# Run from the repository root with the host's approved scheduler. Creates an encrypted
# backup on the mounted off-host destination, then applies the configured retention.
# Required in deployment/.env: BACKUP_DESTINATION, BACKUP_RECIPIENT_FILE (public key only).
# Retention runs only when BACKUP_KEEP_DAILY and BACKUP_KEEP_WEEKLY are both set.
set -eu
umask 077
compose="docker compose --env-file deployment/.env -f deployment/compose.yaml -f deployment/backup.yaml --profile backup"
$compose run --rm backup create --recipient-file /run/keys/recipient.asc --destination /backups
set -a
. deployment/.env
set +a
if [ -n "${BACKUP_KEEP_DAILY:-}" ] && [ -n "${BACKUP_KEEP_WEEKLY:-}" ]; then
  $compose run --rm backup purge --destination /backups \
    --keep-daily "$BACKUP_KEEP_DAILY" --keep-weekly "$BACKUP_KEEP_WEEKLY"
else
  echo "WARNING: retention not configured; no backups were purged" >&2
fi
