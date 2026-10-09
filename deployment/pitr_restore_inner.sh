#!/bin/sh
# Runs inside the db-restore container: restore to $PITR_TARGET into the empty mounted volume,
# replay WAL, promote, fence serving, stop. The live database volume is never mounted here.
set -eu
data=/var/lib/postgresql/data
[ -z "$(ls -A "$data" 2>/dev/null)" ] || { echo "Restore volume is not empty" >&2; exit 2; }
chown postgres:postgres "$data" && chmod 700 "$data"
if [ -n "${PITR_TARGET:-}" ]; then
  su-exec postgres pgbackrest --stanza=main --type=time "--target=$PITR_TARGET" --target-action=promote restore
else
  su-exec postgres pgbackrest --stanza=main --type=default restore
fi
# Verification instance: no archiving (the restored timeline must not mix into the live repository
# until promoted deliberately), local socket only.
su-exec postgres postgres -c archive_mode=off -c listen_addresses='' &
user=${POSTGRES_USER:-postgres}
wait_for() {  # wait_for <description> <command...>: give up after 30 minutes
  label=$1; shift; waited=0
  until "$@" >/dev/null 2>&1; do
    waited=$((waited + 1)); [ "$waited" -lt 1800 ] || { echo "Timed out waiting for $label" >&2; exit 3; }
    sleep 1
  done
}
promoted() { [ "$(su-exec postgres psql -U "$user" -XAtq -d "$POSTGRES_DB" -c 'SELECT pg_is_in_recovery()')" = "f" ]; }
wait_for "PostgreSQL to start" su-exec postgres pg_isready -q
wait_for "WAL replay and promotion" promoted
# Fence: a physical restore keeps the instance identity, so invalidate the binding. Nothing serves
# until `python -m rights.recovery recover` has replayed the denial journal.
su-exec postgres psql -U "$user" -X -v ON_ERROR_STOP=1 -d "$POSTGRES_DB" -c \
  "DO \$\$ BEGIN IF to_regclass('policy.instance_binding') IS NOT NULL THEN UPDATE policy.instance_binding SET database_oid=0; END IF; END \$\$;"
su-exec postgres psql -U "$user" -XAtq -d "$POSTGRES_DB" -c "SELECT 'restored at ' || now()::text"
su-exec postgres pg_ctl -D "$data" stop -m fast
