#!/bin/sh
# Run from the repository root. Schedule daily; upload encrypted copies separately.
set -eu
umask 077
mkdir -p backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
output="backups/open-alvary-$stamp.dump"
docker compose --env-file deployment/.env -f deployment/compose.yaml exec -T db \
  pg_dump -U open_alvary -d open_alvary -Fc > "$output.partial"
# Validate the archive header/catalogue before promoting the file.
docker compose --env-file deployment/.env -f deployment/compose.yaml exec -T db \
  pg_restore --list < "$output.partial" > /dev/null
mv "$output.partial" "$output"
printf 'Backup created: %s\n' "$output"
