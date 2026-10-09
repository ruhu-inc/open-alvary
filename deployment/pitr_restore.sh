#!/bin/sh
# Restore the database to a point in time into a NEW volume (the live volume is untouched).
#   deployment/pitr_restore.sh "2026-10-08 14:30:00+00"   (omit the time for the latest state)
# Then follow deployment/runbook.md: verify, recover the journal, and promote the volume.
set -eu
umask 077
target=${1:-}
volume="open-alvary_restore_$(date -u +%Y%m%dT%H%M%SZ)"
docker volume create "$volume" >/dev/null
PITR_TARGET=$target PITR_RESTORE_VOLUME=$volume docker compose --env-file deployment/.env \
  -f deployment/compose.yaml -f deployment/pitr.yaml --profile pitr-restore run --rm db-restore
echo "Restored into volume: $volume (serving fenced until rights.recovery recover runs)"
