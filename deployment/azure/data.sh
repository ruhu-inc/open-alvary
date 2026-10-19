#!/usr/bin/env bash
# The data from Google Cloud to Azure (deployment/azure/README.md, phase 5), rehearsed on staging
# first. The databases go by dump and restore; the buckets are copied inside Azure, object by object
# (operations/transfer.py). Every step checks itself: the restore compares each table's rows with
# the source's, and verify compares every object's MD5.
#
#   deployment/azure/data.sh plan    staging
#   deployment/azure/data.sh dump    staging public|review   # on this machine, through cloud-sql-proxy
#   deployment/azure/data.sh restore staging public|review   # the restore job, inside the network
#   deployment/azure/data.sh buckets staging                 # the copy jobs, inside Azure
#   deployment/azure/data.sh verify  staging                 # every object, by MD5
#
# dump needs PostgreSQL 16's client tools here (the restore image is 16: a newer pg_dump writes a
# format it cannot read), cloud-sql-proxy listening for the instance, and gcloud and az signed in.
# The dump holds personal data: it lives in a private temporary folder, goes to the transfer
# container (deleted after 14 days) and is removed here at once. Passwords are only ever in a
# temporary password file (PGPASSFILE), never an argument or a variable that is printed.

set -euo pipefail

COMMAND="${1:-}"
ENVIRONMENT="${2:-}"
TARGET="${3:-}"
[[ "$COMMAND" =~ ^(plan|dump|restore|buckets|verify)$ ]] && [[ "$ENVIRONMENT" == staging || "$ENVIRONMENT" == prod ]] || {
  echo "Usage: $0 plan|dump|restore|buckets|verify staging|prod [public|review]" >&2
  exit 2
}
if [[ "$COMMAND" == dump || "$COMMAND" == restore ]] && [[ "$TARGET" != public && "$TARGET" != review ]]; then
  echo "Name the database: public or review" >&2
  exit 2
fi

SHORT="$([[ "$ENVIRONMENT" == staging ]] && echo stg || echo prod)"
GROUP="open-alvary-${ENVIRONMENT}-sc-rg"
HERE="$(cd "$(dirname "$0")" && pwd)"

corpus_account() {
  az storage account list --resource-group "$GROUP" \
    --query "[?starts_with(name, 'openalvary') && contains(name, 'corp')].name | [0]" --output tsv
}

wait_for() {  # job execution container: until it ends; non-zero unless it succeeded
  local job="$1" execution="$2" container="$3" status
  while :; do
    status="$(az containerapp job execution show --name "$job" --resource-group "$GROUP" \
      --job-execution-name "$execution" --query properties.status --output tsv)"
    case "$status" in
      Succeeded) echo "  $job: succeeded"; return 0 ;;
      Failed|Stopped|Degraded) echo "  $job: $status. Its log (counts and names only):" >&2
        echo "  az containerapp job logs show --name $job --resource-group $GROUP --execution $execution --container $container" >&2
        return 1 ;;
    esac
    sleep 15
  done
}

case "$COMMAND" in
plan)
  cat <<PLAN
From Google Cloud (as listed on 2026-10-19) to $GROUP:
  databases   open-alvary-prod:europe-west1:open-alvary-db (Postgres 16, 10 GB disk)  → open-alvary-${ENVIRONMENT}-sc-pg
              open-alvary-ingestion:europe-west1:review-db (Postgres 16, 10 GB disk) → open-alvary-${ENVIRONMENT}-sc-review-pg
  buckets     gs://open-alvary-corpus (18.4 GB)                  → az://<corpus account>/corpus
              gs://open-alvary-ingestion-review-collection (111 MB) → az://<corpus account>/workbench/collections/ng/
  not copied  the two Cloud Build buckets; there is no archive bucket on Google Cloud
Order: freeze writes on Google Cloud (the switchover window), dump public and review, restore both,
copy the buckets (again at switchover: a second run copies only what is new), verify, then DNS.
Needs restore_image, ingestion_image and workbench_image set in ${ENVIRONMENT}.tfvars (release.sh).
PLAN
  ;;

dump)
  [[ "$(pg_dump --version | grep -oE '[0-9]+' | head -1)" == 16 ]] \
    || { echo "pg_dump must be PostgreSQL 16's (brew install postgresql@16; put its bin first in PATH)" >&2; exit 1; }
  if [[ "$TARGET" == public ]]; then
    INSTANCE=open-alvary-prod:europe-west1:open-alvary-db PORT="${PROXY_PORT:-55432}"
    DATABASE=open_alvary USER_NAME=open_alvary
  else
    INSTANCE=open-alvary-ingestion:europe-west1:review-db PORT="${PROXY_PORT:-55433}"
  fi
  pg_isready --host=127.0.0.1 --port="$PORT" >/dev/null \
    || { echo "Start the proxy first: cloud-sql-proxy $INSTANCE --port $PORT" >&2; exit 1; }
  folder="$(mktemp -d)"
  chmod 700 "$folder"
  trap 'rm -rf "$folder"' EXIT
  export PGPASSFILE="$folder/pgpass"
  ( umask 077
    if [[ "$TARGET" == public ]]; then
      gcloud secrets versions access latest --secret=database-password --project=open-alvary-prod \
        | python3 -c 'import sys; pw = sys.stdin.read().strip().replace("\\", "\\\\").replace(":", "\\:"); print(f"127.0.0.1:'"$PORT"':*:open_alvary:{pw}")' > "$PGPASSFILE"
    else
      gcloud secrets versions access latest --secret=review-database-url --project=open-alvary-ingestion \
        | python3 -c 'import sys, urllib.parse as u; p = u.urlsplit(sys.stdin.read().strip()); esc = lambda v: u.unquote(v).replace("\\", "\\\\").replace(":", "\\:"); print(f"127.0.0.1:'"$PORT"':{p.path.lstrip(chr(47))}:{esc(p.username)}:{esc(p.password)}")' > "$PGPASSFILE"
    fi )
  if [[ "$TARGET" == review ]]; then  # the user and database, from the password file's line
    IFS=: read -r _ _ DATABASE USER_NAME _ < "$PGPASSFILE"
  fi
  SOURCE="host=127.0.0.1 port=$PORT dbname=$DATABASE user=$USER_NAME sslmode=disable"
  echo "Dumping $TARGET ($INSTANCE)…"
  pg_dump --format=custom --no-password --file="$folder/database.dump" "$SOURCE"
  SOURCE_URL="postgresql://$USER_NAME@127.0.0.1:$PORT/$DATABASE?sslmode=disable" python3 "$HERE/restore.py" describe "$folder"
  account="$(corpus_account)"
  for name in database.dump roles.json counts.json; do
    az storage blob upload --auth-mode login --account-name "$account" --container-name transfer \
      --name "$TARGET/$name" --file "$folder/$name" --overwrite --output none
  done
  echo "Uploaded to $account/transfer/$TARGET/ (deleted there after 14 days). Next: $0 restore $ENVIRONMENT $TARGET"
  ;;

restore)
  JOB="open-alvary-${SHORT}-sc-restore-${TARGET}"
  az containerapp job show --name "$JOB" --resource-group "$GROUP" --output none 2>/dev/null \
    || { echo "No $JOB: set restore_image (release.sh $ENVIRONMENT restore) and apply" >&2; exit 1; }
  execution="$(az containerapp job start --name "$JOB" --resource-group "$GROUP" --query name --output tsv)"
  echo "Restoring $TARGET ($execution)…"
  wait_for "$JOB" "$execution" restore
  echo "Then run the migrations job (release.sh $ENVIRONMENT public): it checks the restored schema."
  ;;

buckets|verify)
  JOB="open-alvary-${SHORT}-sc-transfer"
  account="$(corpus_account)"
  az containerapp job show --name "$JOB" --resource-group "$GROUP" --output none 2>/dev/null \
    || { echo "No $JOB: set ingestion_image (release.sh $ENVIRONMENT ingestion) and apply" >&2; exit 1; }
  action="$([[ "$COMMAND" == buckets ]] && echo copy || echo verify)"
  image="$(az containerapp job show --name "$JOB" --resource-group "$GROUP" --query "properties.template.containers[0].image" --output tsv)"
  status=0
  for pair in "gs://open-alvary-corpus az://$account/corpus" \
    "gs://open-alvary-ingestion-review-collection az://$account/workbench/collections/ng/"; do
    read -r source target <<<"$pair"
    execution="$(az containerapp job start --name "$JOB" --resource-group "$GROUP" --image "$image" \
      --container-name transfer --command python --args "-m" "operations.transfer" "$action" "$source" "$target" \
      --query name --output tsv)"
    echo "$action $source → $target ($execution)…"
    wait_for "$JOB" "$execution" transfer || status=1
  done
  exit "$status"
  ;;
esac
