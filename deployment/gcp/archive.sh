#!/usr/bin/env bash
# The write-once archive of fetched originals (CPR-05), step by step:
#
#   deployment/gcp/archive.sh plan      # print what would be created; creates nothing
#   deployment/gcp/archive.sh setup     # the two buckets, with a retention policy (not yet locked)
#   deployment/gcp/archive.sh lock      # lock the archive bucket's retention: irreversible, asks first
#   deployment/gcp/archive.sh status
#
# The archive bucket holds originals whose acquisition assessment allows long-term keeping (official
# public texts): once locked, no role can delete or overwrite an object before its retention ends, not
# even the project owner. Originals with personal data or an uncertain privacy status go to the
# unlocked tier, which keeps their assessment's deadline so lawful erasure stays possible.
# Objects are written by python -m operations.archive copy (run against the mounted buckets).
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-ingestion}"
LOCATION="${ARCHIVE_LOCATION:-EU}"
ARCHIVE="${ALVARY_ARCHIVE_BUCKET:-open-alvary-archive}"
UNLOCKED="${ALVARY_ARCHIVE_UNLOCKED_BUCKET:-open-alvary-archive-unlocked}"
RETENTION="${ARCHIVE_RETENTION:-10y}"
WRITER="${ARCHIVE_WRITER:-scan-reader@${PROJECT}.iam.gserviceaccount.com}"
GC=(gcloud --project "$PROJECT" --quiet)

plan() {
  cat <<PLAN
Project $PROJECT, location $LOCATION. Would create (if absent):
  - gs://$ARCHIVE: uniform access, public access prevented, retention $RETENTION (unlocked until
    'lock'); $WRITER may create objects but not delete or overwrite them
  - gs://$UNLOCKED: uniform access, public access prevented, no retention policy; $WRITER may
    create objects; erasure follows each object's assessment deadline
Locking (a separate, confirmed step) makes the retention permanent for gs://$ARCHIVE.
PLAN
}

setup() {
  if ! "${GC[@]}" storage buckets describe "gs://$ARCHIVE" >/dev/null 2>&1; then
    "${GC[@]}" storage buckets create "gs://$ARCHIVE" --location="$LOCATION" --uniform-bucket-level-access \
      --public-access-prevention --retention-period="$RETENTION"
  fi
  if ! "${GC[@]}" storage buckets describe "gs://$UNLOCKED" >/dev/null 2>&1; then
    "${GC[@]}" storage buckets create "gs://$UNLOCKED" --location="$LOCATION" --uniform-bucket-level-access \
      --public-access-prevention
  fi
  for bucket in "$ARCHIVE" "$UNLOCKED"; do
    "${GC[@]}" storage buckets add-iam-policy-binding "gs://$bucket" \
      --member="serviceAccount:$WRITER" --role=roles/storage.objectCreator >/dev/null
    "${GC[@]}" storage buckets add-iam-policy-binding "gs://$bucket" \
      --member="serviceAccount:$WRITER" --role=roles/storage.objectViewer >/dev/null
  done
  echo "Created. The retention policy on gs://$ARCHIVE is not locked: run 'lock' when the owner decides."
}

lock() {
  echo "Locking the retention policy of gs://$ARCHIVE ($RETENTION) is permanent: no object can then be"
  echo "deleted before its retention ends, by anyone, and the policy can never be shortened or removed."
  read -r -p "Type the bucket name to lock it: " typed
  if [[ "$typed" != "$ARCHIVE" ]]; then
    echo "Not locked."
    exit 1
  fi
  "${GC[@]}" storage buckets update "gs://$ARCHIVE" --lock-retention-period
  echo "Locked."
}

status() {
  for bucket in "$ARCHIVE" "$UNLOCKED"; do
    "${GC[@]}" storage buckets describe "gs://$bucket" \
      --format='yaml(name,location,retention_policy,public_access_prevention)' 2>/dev/null ||
      echo "gs://$bucket does not exist"
  done
}

case "${1:-plan}" in
  plan) plan ;;
  setup) setup ;;
  lock) lock ;;
  status) status ;;
  *) echo "Usage: $0 plan|setup|lock|status" >&2; exit 2 ;;
esac
