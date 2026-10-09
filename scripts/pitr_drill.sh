#!/bin/sh
# Local point-in-time recovery drill: real pgBackRest archiving and a time-targeted restore into a
# new volume, using a directory repository instead of R2. Creates and removes only oa-pitr-* objects.
set -eu
image=open-alvary-db:16
tag=$(openssl rand -hex 4)
repo=oa-pitr-repo-$tag; data=oa-pitr-data-$tag; restored=oa-pitr-restore-$tag; db=oa-pitr-db-$tag
cleanup() {
  docker rm -f "$db" "$db-check" >/dev/null 2>&1 || true
  docker volume rm -f "$repo" "$data" "$restored" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
env="-e PGBACKREST_STANZA=main -e PGBACKREST_PG1_PATH=/var/lib/postgresql/data -e PGBACKREST_PG1_USER=open_alvary -e PGBACKREST_REPO1_TYPE=posix
 -e PGBACKREST_REPO1_PATH=/repo -e PGBACKREST_REPO1_CIPHER_TYPE=aes-256-cbc -e PGBACKREST_REPO1_CIPHER_PASS=drill-only
 -e PGBACKREST_LOCK_PATH=/tmp/pgbackrest -e PGBACKREST_LOG_LEVEL_FILE=off -e PGBACKREST_LOG_LEVEL_CONSOLE=warn"
docker build -q -t "$image" -f deployment/db.Dockerfile . >/dev/null
docker volume create "$repo" >/dev/null; docker volume create "$data" >/dev/null; docker volume create "$restored" >/dev/null
docker run --rm --entrypoint sh -v "$repo:/repo" "$image" -c 'chown postgres:postgres /repo'
# shellcheck disable=SC2086
docker run -d --name "$db" $env -e POSTGRES_USER=open_alvary -e POSTGRES_DB=open_alvary -e POSTGRES_PASSWORD=drill \
  -v "$data:/var/lib/postgresql/data" -v "$repo:/repo" "$image" \
  postgres -c wal_level=replica -c archive_mode=on -c "archive_command=pgbackrest --stanza=main archive-push %p" \
  -c archive_timeout=60 >/dev/null
until docker exec "$db" pg_isready -U open_alvary -q 2>/dev/null && docker exec "$db" psql -U open_alvary -XAtq -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done
sql() { docker exec "$db" psql -U open_alvary -d open_alvary -XAtq -v ON_ERROR_STOP=1 -c "$1"; }
sql "CREATE SCHEMA policy; CREATE TABLE policy.instance_binding(singleton boolean PRIMARY KEY DEFAULT true, system_identifier numeric, database_oid oid);
     INSERT INTO policy.instance_binding VALUES (true, 1, 12345); CREATE TABLE marker(label text, at timestamptz DEFAULT clock_timestamp());"
docker exec -u postgres "$db" pgbackrest stanza-create
docker exec -u postgres "$db" pgbackrest check
docker exec -u postgres "$db" pgbackrest backup --type=full
sql "INSERT INTO marker(label) VALUES ('before-target')"
sleep 2; target=$(sql "SELECT clock_timestamp()::text"); sleep 2
sql "INSERT INTO marker(label) VALUES ('after-target')"
sql "SELECT pg_switch_wal()" >/dev/null
docker exec -u postgres "$db" pgbackrest check   # waits until the latest WAL is archived
docker stop "$db" >/dev/null
echo "Restoring to $target"
# shellcheck disable=SC2086
docker run --rm $env -e PITR_TARGET="$target" -e POSTGRES_DB=open_alvary -e POSTGRES_USER=open_alvary -v "$restored:/var/lib/postgresql/data" \
  -v "$repo:/repo" -v "$PWD/deployment/pitr_restore_inner.sh:/pitr_restore_inner.sh:ro" \
  --entrypoint sh "$image" /pitr_restore_inner.sh
# shellcheck disable=SC2086
docker run -d --name "$db-check" -e POSTGRES_PASSWORD=drill -v "$restored:/var/lib/postgresql/data" "$image" >/dev/null
until docker exec "$db-check" psql -U open_alvary -d open_alvary -XAtq -c 'SELECT 1' >/dev/null 2>&1; do sleep 1; done
labels=$(docker exec "$db-check" psql -U open_alvary -d open_alvary -XAtq -c "SELECT string_agg(label, ',' ORDER BY at) FROM marker")
fence=$(docker exec "$db-check" psql -U open_alvary -d open_alvary -XAtq -c "SELECT database_oid FROM policy.instance_binding")
echo "Rows after restore: $labels; instance binding oid: $fence"
[ "$labels" = "before-target" ] && [ "$fence" = "0" ] && echo "PASS: point-in-time restore and serving fence" || { echo "FAIL"; exit 1; }
