#!/bin/sh
# Run from the repository root with the host's scheduler (daily). Creates an encrypted backup in
# Cloudflare R2, then applies the configured retention. Settings live in deployment/.env.
# The env file is read as data (never sourced), so unquoted values cannot execute.
set -eu
umask 077
env_file=deployment/.env
setting() {
  sed -n "s/^$1=//p" "$env_file" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}
remote=$(setting BACKUP_REMOTE)
keep_daily=$(setting BACKUP_KEEP_DAILY)
keep_weekly=$(setting BACKUP_KEEP_WEEKLY)
: "${remote:?Set BACKUP_REMOTE in deployment/.env, for example r2:open-alvary-backups/production}"
compose="docker compose --env-file $env_file -f deployment/compose.yaml -f deployment/backup.yaml --profile backup"
$compose run --rm backup create --recipient-file /run/keys/recipient.asc --remote "$remote"
if [ -n "$keep_daily" ] && [ -n "$keep_weekly" ]; then
  $compose run --rm backup purge --remote "$remote" --keep-daily "$keep_daily" --keep-weekly "$keep_weekly"
else
  echo "WARNING: retention not configured; no backups were purged" >&2
fi
