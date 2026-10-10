#!/usr/bin/env bash
# Review workbench on Google Cloud (project open-alvary-ingestion, EU), step by step:
#
#   deployment/gcp/workbench.sh plan       # print what would be created; creates nothing
#   deployment/gcp/workbench.sh setup      # APIs, registry, bucket, database, secrets, service account
#   deployment/gcp/workbench.sh upload     # reconciled readings and page images -> bucket (read-only to the app)
#   deployment/gcp/workbench.sh deploy     # build the image, deploy Cloud Run behind Identity-Aware Proxy
#   deployment/gcp/workbench.sh operator <email> "<name>" reviewer,approver   # allow-list an operator
#   deployment/gcp/workbench.sh status
#
# Cloud Run (scales to zero) reads a Cloud Storage bucket mounted read-only and a Cloud SQL
# PostgreSQL database through the Cloud SQL connector; secrets live in Secret Manager; the app
# runs as its own service account with only what it needs. Identity-Aware Proxy admits only the
# listed Google accounts before the app's own operator sign-in.
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-ingestion}"
REGION="${GCP_REGION:-europe-west1}"
SERVICE="${WORKBENCH_SERVICE:-review-workbench}"
REPOSITORY="open-alvary"
BUCKET="${WORKBENCH_BUCKET:-${PROJECT}-review-collection}"
SQL_INSTANCE="${WORKBENCH_SQL_INSTANCE:-review-db}"
SQL_TIER="${WORKBENCH_SQL_TIER:-db-f1-micro}"
RUNNER="workbench-runner"
RUNNER_EMAIL="${RUNNER}@${PROJECT}.iam.gserviceaccount.com"
COLLECTION="${WORKBENCH_COLLECTION_DIR:-private-staging/first-collection}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/workbench:$(git rev-parse --short HEAD)"
GC=(gcloud --project "$PROJECT" --quiet)

secret() {  # create a secret once; later runs keep it
  local name="$1" value="$2"
  if ! "${GC[@]}" secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | "${GC[@]}" secrets create "$name" --replication-policy=user-managed --locations="$REGION" --data-file=-
  fi
  "${GC[@]}" secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUNNER_EMAIL" --role=roles/secretmanager.secretAccessor >/dev/null
}

plan() {
  cat <<EOF
Project $PROJECT, region $REGION. Would create (if absent):
  - APIs: Cloud Run, Cloud SQL Admin, Artifact Registry, Secret Manager, IAP, Cloud Build, Cloud Storage
  - Artifact Registry repository $REPOSITORY (Docker images of the workbench)
  - Cloud Storage bucket gs://$BUCKET (EU, uniform access, public access prevented), read-only to the app
  - Cloud SQL PostgreSQL 16 instance $SQL_INSTANCE ($SQL_TIER, 10 GB SSD, daily backups + point-in-time recovery),
    database 'review', user 'review' with a generated password
  - Secrets: review-database-url, review-oidc-client-secret (values never printed)
  - Service account $RUNNER_EMAIL: Cloud SQL client, read on the bucket, access to the two secrets only
  - Cloud Run service $SERVICE (512 MiB, 0-2 instances), not public: Identity-Aware Proxy in front
Billed monthly at low use: Cloud SQL ~US\$10-15 (the main cost), Cloud Run ~\$0 (scales to zero),
storage, registry and secrets a few cents.
EOF
}

setup() {
  "${GC[@]}" services enable run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com iap.googleapis.com cloudbuild.googleapis.com storage.googleapis.com
  "${GC[@]}" artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1 ||
    "${GC[@]}" artifacts repositories create "$REPOSITORY" --repository-format=docker --location="$REGION"
  "${GC[@]}" storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 ||
    "${GC[@]}" storage buckets create "gs://$BUCKET" --location=EU --uniform-bucket-level-access --public-access-prevention
  "${GC[@]}" iam service-accounts describe "$RUNNER_EMAIL" >/dev/null 2>&1 ||
    "${GC[@]}" iam service-accounts create "$RUNNER" --display-name="Review workbench (Cloud Run)"
  "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER_EMAIL" --role=roles/cloudsql.client >/dev/null
  "${GC[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$RUNNER_EMAIL" --role=roles/storage.objectViewer >/dev/null
  if ! "${GC[@]}" sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
    "${GC[@]}" sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE \
      --tier="$SQL_TIER" --region="$REGION" --storage-size=10 --storage-type=SSD --storage-auto-increase \
      --backup-start-time=02:00 --enable-point-in-time-recovery --require-ssl
  fi
  "${GC[@]}" sql databases describe review --instance="$SQL_INSTANCE" >/dev/null 2>&1 ||
    "${GC[@]}" sql databases create review --instance="$SQL_INSTANCE"
  if ! "${GC[@]}" secrets describe review-database-url >/dev/null 2>&1; then
    local password
    password="$(openssl rand -hex 24)"
    "${GC[@]}" sql users create review --instance="$SQL_INSTANCE" --password="$password"
    local connection="${PROJECT}:${REGION}:${SQL_INSTANCE}"
    secret review-database-url "postgresql+psycopg://review:${password}@/review?host=/cloudsql/${connection}"
  fi
  if [ -n "${ALVARY_REVIEW_OIDC_CLIENT_SECRET:-}" ]; then
    secret review-oidc-client-secret "$ALVARY_REVIEW_OIDC_CLIENT_SECRET"
  else
    echo "Set ALVARY_REVIEW_OIDC_CLIENT_SECRET (from the OAuth client) and rerun setup to store it."
  fi
}

upload() {
  test -d "$COLLECTION" || { echo "No collection at $COLLECTION"; exit 1; }
  python -m workbench.prepare
  # Only what the workbench reads: reconciled readings, the positional reader's output, page images.
  "${GC[@]}" storage rsync "$COLLECTION" "gs://$BUCKET" --recursive --delete-unmatched-destination-objects \
    --exclude='.*\.pdf$|.*\.txt$|.*\.pages\.json$|.*\.extraction\.json$|.*\.provenance\.json$|.*\.layout\.json$|.*\.review\.html$|^pages/.*|^runs/.*|^readings/[^/]+/(docai|mineru)\.json$|^readings/\..*|.*/\..*'
}

deploy() {
  : "${ALVARY_REVIEW_OIDC_CLIENT_ID:?Set ALVARY_REVIEW_OIDC_CLIENT_ID (the OAuth client of type Web application)}"
  "${GC[@]}" builds submit --tag "$IMAGE" .
  local number url
  number="$("${GC[@]}" projects describe "$PROJECT" --format='value(projectNumber)')"
  url="https://${SERVICE}-${number}.${REGION}.run.app"
  "${GC[@]}" run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --no-allow-unauthenticated --iap --memory=512Mi --cpu=1 --min-instances=0 --max-instances=2 \
    --add-cloudsql-instances="${PROJECT}:${REGION}:${SQL_INSTANCE}" \
    --add-volume=name=collection,type=cloud-storage,bucket="$BUCKET",readonly=true \
    --add-volume-mount=volume=collection,mount-path=/data/collection \
    --set-env-vars="ALVARY_REVIEW_STAGING=/data/collection,ALVARY_REVIEW_BASE_URL=${url},ALVARY_REVIEW_ALLOWED_HOSTS=${SERVICE}-${number}.${REGION}.run.app,ALVARY_REVIEW_OIDC_CLIENT_ID=${ALVARY_REVIEW_OIDC_CLIENT_ID}" \
    --set-secrets="ALVARY_REVIEW_DATABASE_URL=review-database-url:latest,ALVARY_REVIEW_OIDC_CLIENT_SECRET=review-oidc-client-secret:latest" \
    --command=uvicorn --args="workbench.app:app,--host,0.0.0.0,--port,8080,--proxy-headers,--forwarded-allow-ips,*,--no-access-log"
  # Identity-Aware Proxy calls the service as Google's IAP service agent.
  "${GC[@]}" run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
    --member="serviceAccount:service-${number}@gcp-sa-iap.iam.gserviceaccount.com" --role=roles/run.invoker >/dev/null
  echo "Deployed: $url  (OAuth redirect URI: ${url}/auth/callback)"
  echo "Allow an operator through Identity-Aware Proxy:"
  echo "  gcloud --project $PROJECT iap web add-iam-policy-binding --resource-type=cloud-run --service=$SERVICE --region=$REGION --member=user:<email> --role=roles/iap.httpsResourceAccessor"
}

operator() {
  local email="${1:?email}" name="${2:?name}" roles="${3:?roles}"
  "${GC[@]}" run jobs deploy workbench-admin --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --add-cloudsql-instances="${PROJECT}:${REGION}:${SQL_INSTANCE}" \
    --set-secrets="ALVARY_REVIEW_DATABASE_URL=review-database-url:latest" \
    --command=python --args="-m,workbench.admin,add,${email},--name,${name},--roles,${roles}" --max-retries=0
  "${GC[@]}" run jobs execute workbench-admin --region="$REGION" --wait
  "${GC[@]}" iap web add-iam-policy-binding --resource-type=cloud-run --service="$SERVICE" --region="$REGION" \
    --member="user:${email}" --role=roles/iap.httpsResourceAccessor
}

status() {
  "${GC[@]}" run services describe "$SERVICE" --region="$REGION" --format='value(status.url,status.conditions[0].status)' || true
  "${GC[@]}" sql instances describe "$SQL_INSTANCE" --format='value(state,settings.tier)' || true
}

case "${1:-plan}" in
  plan) plan ;;
  setup) setup ;;
  upload) upload ;;
  deploy) deploy ;;
  operator) shift; operator "$@" ;;
  status) status ;;
  *) echo "usage: $0 plan|setup|upload|deploy|operator <email> <name> <roles>|status"; exit 2 ;;
esac
