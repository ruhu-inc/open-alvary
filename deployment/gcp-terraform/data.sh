#!/usr/bin/env bash
# The data into the Google Cloud environment (deployment/gcp-terraform/README.md, phase 5), rehearsed
# first where possible. The databases go by dump and restore; the buckets are used in place or copied
# object by object (operations/transfer.py). Every step checks itself: the restore compares each
# table's rows with the source's, and verify compares every object's MD5.
#
#   deployment/gcp-terraform/data.sh plan    staging
#   deployment/gcp-terraform/data.sh backup  staging public|review   # from today's Google Cloud: its backup, restored here
#   deployment/gcp-terraform/data.sh dump    staging public|review [gcp|azure]
#   deployment/gcp-terraform/data.sh restore staging public|review   # the restore job, inside the network
#   deployment/gcp-terraform/data.sh buckets staging [gcp|azure]     # the copy jobs
#   deployment/gcp-terraform/data.sh verify  staging [gcp|azure]     # every object, by MD5
#
# backup is how the databases move from today's Google Cloud: Cloud SQL restores the source instance's
# latest backup (FRESH=1: one taken now) into this environment's instance, across projects. It is an
# exact copy, with the roles, the row-level security and every row. A dump cannot be: the private
# tables' policies admit only the guard role (USING current_user = '<prefix>_guard', FORCE ROW LEVEL
# SECURITY), so pg_dump, run by any user Cloud SQL gives a customer, stops at the first of them. The
# copy brings the source's passwords; the apply this command prints gives this environment's users
# their own again and rewrites the URLs in Secret Manager.
#
# gcp (the default) is today's live system: dump reads its Cloud SQL instances through cloud-sql-proxy
# on this machine, as deployment/azure/data.sh does, and needs PostgreSQL 16's client tools here.
# azure is the move back (README, "Moving back from Azure"): Azure's servers have no public address, so
# Azure's own restore job dumps its database (restore.py dump) into Azure's transfer container, and
# this script carries the three files across; Azure's copy job writes the buckets, admitted here by
# azure_federation. Set AZURE_ENVIRONMENT (default prod) for the Azure environment that is live.
#
# A dump holds personal data: it lives in a private temporary folder here, goes to the transfer bucket
# (deleted after 14 days) and is removed here at once. Passwords are only ever in a temporary password
# file (PGPASSFILE), never an argument or a variable that is printed.

set -euo pipefail

COMMAND="${1:-}"
ENVIRONMENT="${2:-}"
[[ "$COMMAND" =~ ^(plan|backup|dump|restore|buckets|verify)$ ]] && [[ "$ENVIRONMENT" == staging || "$ENVIRONMENT" == prod ]] || {
  echo "Usage: $0 plan|backup|dump|restore|buckets|verify staging|prod [public|review] [gcp|azure]" >&2
  exit 2
}
if [[ "$COMMAND" == backup || "$COMMAND" == dump || "$COMMAND" == restore ]]; then
  TARGET="${3:-}"
  SOURCE="${4:-gcp}"
  [[ "$TARGET" == public || "$TARGET" == review ]] || { echo "Name the database: public or review" >&2; exit 2; }
else
  TARGET=""
  SOURCE="${3:-gcp}"
fi
[[ "$SOURCE" == gcp || "$SOURCE" == azure ]] || { echo "The source is gcp or azure" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
tfvar() {  # name: its quoted value in env/<environment>.tfvars, else nothing
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$HERE/env/${ENVIRONMENT}.tfvars" 2>/dev/null | head -1
}
PROJECT="$(tfvar project_id)"
REGION="$(tfvar region)"
REGION="${REGION:-europe-west1}"
[[ -n "$PROJECT" ]] || { echo "No project_id in env/${ENVIRONMENT}.tfvars" >&2; exit 1; }
[[ "$PROJECT" != open-alvary-prod && "$PROJECT" != open-alvary-ingestion ]] \
  || { echo "$PROJECT is a source, never the target (README)" >&2; exit 1; }
GC=(gcloud --project "$PROJECT" --quiet)
PREFIX="open-alvary-${ENVIRONMENT}"
TRANSFER="${PREFIX}-transfer"
CORPUS="$(tfvar existing_corpus_bucket)"
COLLECTION="$(tfvar existing_review_bucket)"
IN_PLACE_CORPUS="${CORPUS:+yes}"
IN_PLACE_COLLECTION="${COLLECTION:+yes}"
CORPUS="${CORPUS:-${PREFIX}-corpus}"
COLLECTION="${COLLECTION:-${PREFIX}-review-collection}"
ARCHIVE="${PREFIX}-archive"

AZ_ENV="${AZURE_ENVIRONMENT:-prod}"
AZ_SHORT="$([[ "$AZ_ENV" == staging ]] && echo stg || echo prod)"
AZ_GROUP="open-alvary-${AZ_ENV}-sc-rg"
azure_account() {  # kind (corp or arch): the Azure storage account's name
  az storage account list --resource-group "$AZ_GROUP" \
    --query "[?starts_with(name, 'openalvary') && contains(name, '$1')].name | [0]" --output tsv
}

azure_wait() {  # job execution: until it ends; non-zero unless it succeeded
  local job="$1" execution="$2" status
  while :; do
    status="$(az containerapp job execution show --name "$job" --resource-group "$AZ_GROUP" \
      --job-execution-name "$execution" --query properties.status --output tsv)"
    case "$status" in
      Succeeded) echo "  $job: succeeded"; return 0 ;;
      Failed|Stopped|Degraded) echo "  $job: $status (its log holds counts and names only)" >&2; return 1 ;;
    esac
    sleep 15
  done
}

pairs() {  # source and target places for the copies, one pair a line
  if [[ "$SOURCE" == gcp ]]; then
    [[ -n "$IN_PLACE_CORPUS" ]] || echo "gs://open-alvary-corpus gs://$CORPUS"
    [[ -n "$IN_PLACE_COLLECTION" ]] || echo "gs://open-alvary-ingestion-review-collection gs://$COLLECTION/"
  else
    local corpus archive
    corpus="$(azure_account corp)"
    archive="$(azure_account arch)"
    echo "az://$corpus/corpus gs://$CORPUS"
    [[ -n "$IN_PLACE_COLLECTION" ]] || echo "az://$corpus/workbench/collections/ng/ gs://$COLLECTION/"
    echo "az://$archive/originals gs://$ARCHIVE"
  fi
}

case "$COMMAND" in
plan)
  cat <<PLAN
Into $PROJECT ($ENVIRONMENT), from today's Google Cloud (gcp) or from Azure (azure):
  databases   open-alvary-prod:europe-west1:open-alvary-db, or Azure's open-alvary-${AZ_ENV}-sc-pg      → ${PREFIX}-pg
              open-alvary-ingestion:europe-west1:review-db, or Azure's open-alvary-${AZ_ENV}-sc-review-pg → ${PREFIX}-review-pg
  buckets     corpus     gs://$CORPUS$([[ -n "$IN_PLACE_CORPUS" ]] && echo " (used in place: nothing to copy from gcp)")
              collection gs://$COLLECTION$([[ -n "$IN_PLACE_COLLECTION" ]] && echo " (used in place: nothing to copy)")
              archive    gs://$ARCHIVE (from azure only: there is no archive on Google Cloud today)
Order: freeze writes in the live system (the switchover window), dump public and review, restore both,
copy the buckets (again at switchover: a second run copies only what is new), verify, then DNS.
Needs restore_image (and ingestion_image, workbench_image) set in ${ENVIRONMENT}.tfvars (release.sh).
PLAN
  for pair in $(pairs | tr ' ' '|'); do echo "  copy ${pair/|/ → }"; done
  ;;

dump)
  folder="$(mktemp -d)"
  chmod 700 "$folder"
  trap 'rm -rf "$folder"' EXIT
  if [[ "$SOURCE" == gcp ]]; then
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
    echo "Dumping $TARGET ($INSTANCE)…"
    pg_dump --format=custom --no-password --file="$folder/database.dump" \
      "host=127.0.0.1 port=$PORT dbname=$DATABASE user=$USER_NAME sslmode=disable"
    SOURCE_URL="postgresql://$USER_NAME@127.0.0.1:$PORT/$DATABASE?sslmode=disable" \
      python3 "$HERE/../azure/restore.py" describe "$folder"
  else
    # Inside Azure: its restore job, run with dump, writes its own database to Azure's transfer
    # container. Its identity needs write there for this (README, Moving back from Azure).
    JOB="open-alvary-${AZ_SHORT}-sc-restore-${TARGET}"
    image="$(az containerapp job show --name "$JOB" --resource-group "$AZ_GROUP" \
      --query "properties.template.containers[0].image" --output tsv)" \
      || { echo "No $JOB in $AZ_GROUP: set restore_image there and apply" >&2; exit 1; }
    execution="$(az containerapp job start --name "$JOB" --resource-group "$AZ_GROUP" --image "$image" \
      --container-name restore --command python3 --args /opt/open-alvary/restore.py dump --query name --output tsv)"
    echo "Dumping $TARGET inside Azure ($execution)…"
    azure_wait "$JOB" "$execution"
    account="$(azure_account corp)"
    for name in database.dump roles.json counts.json; do
      az storage blob download --auth-mode login --account-name "$account" --container-name transfer \
        --name "$TARGET/$name" --file "$folder/$name" --overwrite --output none
    done
  fi
  for name in database.dump roles.json counts.json; do
    gcloud storage cp "$folder/$name" "gs://$TRANSFER/$TARGET/$name" --project="$PROJECT" --quiet
  done
  echo "In gs://$TRANSFER/$TARGET/ (deleted there after 14 days). Next: $0 restore $ENVIRONMENT $TARGET"
  ;;

backup)
  [[ "$SOURCE" == gcp ]] || { echo "backup restores a Google Cloud backup; from Azure, use dump and restore" >&2; exit 2; }
  if [[ "$TARGET" == public ]]; then
    FROM=open-alvary-db FROM_PROJECT=open-alvary-prod TO="${PREFIX}-pg" PASSWORD="random_password.postgres"
  else
    FROM=review-db FROM_PROJECT=open-alvary-ingestion TO="${PREFIX}-review-pg" PASSWORD="random_password.review[0]"
  fi
  "${GC[@]}" sql instances describe "$TO" --format="value(name)" >/dev/null \
    || { echo "No $TO in $PROJECT: apply env/ first (restore_image or workbench_image makes the review one)" >&2; exit 1; }
  if [[ -n "${FRESH:-}" ]]; then
    echo "Taking a backup of $FROM_PROJECT:$FROM now…"
    gcloud sql backups create --instance="$FROM" --project="$FROM_PROJECT" --description="copy to $PROJECT" --quiet
  fi
  read -r backup taken <<<"$(gcloud sql backups list --instance="$FROM" --project="$FROM_PROJECT" \
    --filter="status=SUCCESSFUL" --sort-by=~windowStartTime --limit=1 --format="value(id,windowStartTime)")"
  [[ -n "$backup" ]] || { echo "No successful backup of $FROM_PROJECT:$FROM" >&2; exit 1; }
  echo "Restoring $FROM_PROJECT:$FROM's backup of $taken into $PROJECT:$TO (everything in $TO is replaced)…"
  "${GC[@]}" sql backups restore "$backup" --restore-instance="$TO" \
    --backup-instance="$FROM" --backup-project="$FROM_PROJECT"
  echo "Restored. Now give $TO's users this environment's passwords again, in deployment/gcp-terraform/env:"
  echo "  terraform apply -var-file=${ENVIRONMENT}.tfvars -replace='$PASSWORD'"
  [[ "$TARGET" == public ]] && echo "Then release the public service (release.sh $ENVIRONMENT public): its migrations check the schema."
  ;;

restore)
  JOB="${PREFIX}-restore-${TARGET}"
  "${GC[@]}" run jobs describe "$JOB" --region="$REGION" >/dev/null 2>&1 \
    || { echo "No $JOB: set restore_image (release.sh $ENVIRONMENT restore) and apply" >&2; exit 1; }
  echo "Restoring $TARGET…"
  "${GC[@]}" run jobs execute "$JOB" --region="$REGION" --wait \
    || { echo "The restore failed: its log has the counts and names (Cloud Run, job $JOB)." >&2; exit 1; }
  echo "Then run the migrations job (release.sh $ENVIRONMENT public): it checks the restored schema."
  ;;

buckets|verify)
  action="$([[ "$COMMAND" == buckets ]] && echo copy || echo verify)"
  work="$(pairs)"
  [[ -n "$work" ]] || { echo "The buckets are used in place: nothing to $action."; exit 0; }
  status=0
  while read -r source target; do
    if [[ "$SOURCE" == gcp ]]; then
      JOB="${PREFIX}-transfer"
      "${GC[@]}" run jobs describe "$JOB" --region="$REGION" >/dev/null 2>&1 \
        || { echo "No $JOB: set ingestion_image (release.sh $ENVIRONMENT ingestion) and apply" >&2; exit 1; }
      echo "$action $source → $target…"
      "${GC[@]}" run jobs execute "$JOB" --region="$REGION" --wait \
        --args="-m,operations.transfer,$action,$source,$target" || status=1
    else
      JOB="open-alvary-${AZ_SHORT}-sc-transfer"
      image="$(az containerapp job show --name "$JOB" --resource-group "$AZ_GROUP" \
        --query "properties.template.containers[0].image" --output tsv)"
      execution="$(az containerapp job start --name "$JOB" --resource-group "$AZ_GROUP" --image "$image" \
        --container-name transfer --command python --args "-m" "operations.transfer" "$action" "$source" "$target" \
        --query name --output tsv)"
      echo "$action $source → $target, inside Azure ($execution)…"
      azure_wait "$JOB" "$execution" || status=1
    fi
  done <<<"$work"
  exit "$status"
  ;;
esac
