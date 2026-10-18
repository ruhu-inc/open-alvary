#!/usr/bin/env bash
# Review workbench on Google Cloud (project open-alvary-ingestion, EU), step by step:
#
#   deployment/gcp/workbench.sh plan       # print what would be created; creates nothing
#   deployment/gcp/workbench.sh setup      # APIs, registry, bucket, database, secrets, service account
#   deployment/gcp/workbench.sh upload     # reconciled readings and page images -> bucket (read-only to the app)
#   deployment/gcp/workbench.sh deploy     # build the image, deploy Cloud Run behind Identity-Aware Proxy
#   deployment/gcp/workbench.sh operator <email> "<name>" reviewer,approver   # allow-list an operator
#   deployment/gcp/workbench.sh access     # once: the Access section's connection to the accounts database
#   ACCESS=1 deployment/gcp/workbench.sh deploy                              # deploy with the Access section
#   deployment/gcp/workbench.sh enrol <email> [--replace]   # a one-time code for an access_admin's passkey
#   deployment/gcp/workbench.sh iap_group <group@domain>    # once: the operators' Google group through IAP
#   deployment/gcp/workbench.sh iap_external   # once: IAP admits accounts outside the organisation (Gmail)
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
# The corpus collections reviewed beside the first, read from the corpus bucket (read-only).
CORPUS_BUCKET="${ALVARY_CORPUS_BUCKET:-open-alvary-corpus}"
CORPUS_COLLECTIONS="${WORKBENCH_CORPUS_COLLECTIONS:-gs://${CORPUS_BUCKET}/collections/rw/}"
# Builds run as their own account (deployment/gcp/ingestion_jobs.sh setup creates it).
BUILDER_EMAIL="image-builder@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/workbench:$(git rev-parse --short HEAD)"
GC=(gcloud --project "$PROJECT" --quiet)
# The Access section (MAC-11) reaches the public accounts database in the public project.
ACCESS="${ACCESS:-0}"
PUBLIC_PROJECT="${PUBLIC_PROJECT:-open-alvary-prod}"
PUBLIC_CONNECTION="${PUBLIC_PROJECT}:${REGION}:${PUBLIC_SQL_INSTANCE:-open-alvary-db}"

secret() {  # create a secret once; later runs keep it
  local name="$1" value="$2"
  if ! "${GC[@]}" secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | "${GC[@]}" secrets create "$name" --replication-policy=user-managed --locations="$REGION" --data-file=-
  fi
  "${GC[@]}" secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUNNER_EMAIL" --role=roles/secretmanager.secretAccessor >/dev/null
}

setting() {  # a value from the environment or the git-ignored .env (never printed)
  local name="$1"
  if [[ -n "${!name:-}" ]]; then printf '%s' "${!name}"; return; fi
  { grep -E "^${name}=" .env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "'\""
}

deployed_image() {  # the image the service runs now: one-off jobs must match it, not the local commit
  local image
  image="$("${GC[@]}" run services describe "$SERVICE" --region="$REGION" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  [[ -n "$image" ]] || { echo "The workbench is not deployed in $PROJECT yet: run $0 setup, then deploy." >&2; exit 1; }
  printf '%s' "$image"
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
  - Service account $RUNNER_EMAIL: Cloud SQL client, read on the bucket and on gs://$CORPUS_BUCKET,
    access to the two secrets only
  - Cloud Run service $SERVICE (2 GiB, 0-2 instances), not public: Identity-Aware Proxy in front;
    it reviews $CORPUS_COLLECTIONS and draws page images on demand
Billed monthly at low use: Cloud SQL ~US\$10-15 (the main cost), Cloud Run ~\$0 (scales to zero),
storage, registry and secrets a few cents.
EOF
}

setup() {
  "${GC[@]}" services enable run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com iap.googleapis.com cloudbuild.googleapis.com storage.googleapis.com \
    cloudresourcemanager.googleapis.com
  "${GC[@]}" artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1 ||
    "${GC[@]}" artifacts repositories create "$REPOSITORY" --repository-format=docker --location="$REGION"
  "${GC[@]}" storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 ||
    "${GC[@]}" storage buckets create "gs://$BUCKET" --location=EU --uniform-bucket-level-access --public-access-prevention
  "${GC[@]}" iam service-accounts describe "$RUNNER_EMAIL" >/dev/null 2>&1 ||
    "${GC[@]}" iam service-accounts create "$RUNNER" --display-name="Review workbench (Cloud Run)"
  "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER_EMAIL" --role=roles/cloudsql.client >/dev/null
  "${GC[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$RUNNER_EMAIL" --role=roles/storage.objectViewer >/dev/null
  "${GC[@]}" storage buckets add-iam-policy-binding "gs://$CORPUS_BUCKET" --member="serviceAccount:$RUNNER_EMAIL" --role=roles/storage.objectViewer >/dev/null
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
  local client_secret
  client_secret="$(setting ALVARY_REVIEW_OIDC_CLIENT_SECRET)"
  if [ -n "$client_secret" ]; then
    secret review-oidc-client-secret "$client_secret"
  else
    echo "Set ALVARY_REVIEW_OIDC_CLIENT_SECRET (from the OAuth client) in .env and rerun setup to store it."
  fi
}

upload() {
  test -d "$COLLECTION" || { echo "No collection at $COLLECTION"; exit 1; }
  # The project's own Python (3.12, with its packages), not whichever python the shell finds first.
  "${PYTHON:-.venv/bin/python}" -m workbench.prepare
  # Only what the workbench reads: reconciled readings, the positional reader's output, page images.
  "${GC[@]}" storage rsync "$COLLECTION" "gs://$BUCKET" --recursive --delete-unmatched-destination-objects \
    --exclude='.*\.pdf$|.*\.txt$|.*\.pages\.json$|.*\.extraction\.json$|.*\.provenance\.json$|.*\.layout\.json$|(.*\.)?review\.html$|^pages/.*|^runs/.*|^readings/[^/]+/(docai|mineru)\.json$|^readings/\..*|.*/\..*'
}

deploy() {
  local client_id group
  client_id="$(setting ALVARY_REVIEW_OIDC_CLIENT_ID)"
  group="$(setting ALVARY_REVIEW_IAP_GROUP)"
  [[ -n "$client_id" ]] || { echo "Set ALVARY_REVIEW_OIDC_CLIENT_ID in .env (the OAuth client of type Web application)" >&2; exit 1; }
  "${GC[@]}" secrets describe review-oidc-client-secret >/dev/null 2>&1 \
    || { echo "Run $0 setup first (with ALVARY_REVIEW_OIDC_CLIENT_SECRET in .env)." >&2; exit 1; }
  if ! git diff --quiet HEAD; then
    echo "Commit first: the image is tagged with the commit it is built from." >&2
    exit 1
  fi
  # Built from the commit itself (git archive): nothing untracked or private can enter the build.
  local context
  context="$(mktemp -d)"
  trap 'rm -rf "$context"' RETURN
  git archive HEAD | tar -x -C "$context"
  "${GC[@]}" builds submit "$context" --region="$REGION" \
    --service-account="projects/$PROJECT/serviceAccounts/$BUILDER_EMAIL" \
    --config="$context/deployment/gcp/workbench.cloudbuild.yaml" --substitutions="_IMAGE=$IMAGE"
  local number url
  number="$("${GC[@]}" projects describe "$PROJECT" --format='value(projectNumber)')"
  url="https://${SERVICE}-${number}.${REGION}.run.app"
  local instances="${PROJECT}:${REGION}:${SQL_INSTANCE}" secrets="ALVARY_REVIEW_DATABASE_URL=review-database-url:latest,ALVARY_REVIEW_OIDC_CLIENT_SECRET=review-oidc-client-secret:latest"
  if [[ "$ACCESS" == 1 ]]; then  # every later deploy must keep ACCESS=1, or the Access section goes dark
    instances+=",${PUBLIC_CONNECTION}"
    secrets+=",ALVARY_ACCESS_DATABASE_URL=access-database-url:latest,RESEND_API_KEY=resend-api-key:latest"
  fi
  "${GC[@]}" run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --no-allow-unauthenticated --iap --memory=2Gi --cpu=1 --min-instances=0 --max-instances=2 \
    --set-cloudsql-instances="$instances" \
    --add-volume=name=collection,type=cloud-storage,bucket="$BUCKET",readonly=true \
    --add-volume-mount=volume=collection,mount-path=/data/collection \
    --set-env-vars="^|^ALVARY_REVIEW_STAGING=/data/collection|ALVARY_REVIEW_COLLECTIONS=${CORPUS_COLLECTIONS}|ALVARY_REVIEW_BASE_URL=${url}|ALVARY_REVIEW_ALLOWED_HOSTS=${SERVICE}-${number}.${REGION}.run.app|ALVARY_REVIEW_OIDC_CLIENT_ID=${client_id}${group:+|ALVARY_REVIEW_IAP_GROUP=${group}}" \
    --set-secrets="$secrets" \
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
  local image
  image="$(deployed_image)"
  # "^|^" makes | the separator, so the comma-separated roles stay one argument (gcloud topic escaping).
  "${GC[@]}" run jobs deploy workbench-admin --image="$image" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --set-cloudsql-instances="${PROJECT}:${REGION}:${SQL_INSTANCE}" \
    --set-secrets="ALVARY_REVIEW_DATABASE_URL=review-database-url:latest" \
    --command=python --args="^|^-m|workbench.admin|add|${email}|--name|${name}|--roles|${roles}" --max-retries=0
  "${GC[@]}" run jobs execute workbench-admin --region="$REGION" --wait
  "${GC[@]}" iap web add-iam-policy-binding --resource-type=cloud-run --service="$SERVICE" --region="$REGION" \
    --member="user:${email}" --role=roles/iap.httpsResourceAccessor
}

access() {  # once: the Access section's connection to the public accounts database (MAC-11)
  # Run 'deployment/gcp/public_service.sh access_role' first. Copies, never prints, two secrets
  # from the public project: the access role's password (inside a connection URL) and the mail key.
  "${GC[@]}" iam service-accounts describe "$RUNNER_EMAIL" >/dev/null 2>&1 \
    || { echo "The workbench is not set up in $PROJECT yet: run $0 setup (and deploy) first." >&2; exit 1; }
  local password key
  password="$(gcloud --project "$PUBLIC_PROJECT" secrets versions access latest --secret=access-role-password)"
  secret access-database-url "postgresql+psycopg://open_alvary_access:${password}@/open_alvary?host=/cloudsql/${PUBLIC_CONNECTION}"
  key="$(gcloud --project "$PUBLIC_PROJECT" secrets versions access latest --secret=resend-api-key)"
  secret resend-api-key "$key"
  cat <<NEXT
Stored access-database-url and resend-api-key. The owner then grants this service account the Cloud
SQL client role in $PUBLIC_PROJECT (once):
  gcloud projects add-iam-policy-binding $PUBLIC_PROJECT --member=serviceAccount:$RUNNER_EMAIL --role=roles/cloudsql.client
Then: ACCESS=1 $0 deploy
  $0 operator $(gcloud config get-value account 2>/dev/null) "Your Name" reviewer,approver,access_admin
  $0 enrol $(gcloud config get-value account 2>/dev/null)
NEXT
}

enrol() {  # a one-time code (24 hours) with which an access_admin registers a first passkey
  local email="${1:?email}" replace=""
  [[ "${2:-}" == "--replace" ]] && replace=",--replace"  # after revoking their passkeys (one was lost)
  local image
  image="$(deployed_image)"
  "${GC[@]}" run jobs deploy workbench-enrol --image="$image" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --set-cloudsql-instances="${PROJECT}:${REGION}:${SQL_INSTANCE}" \
    --set-secrets="ALVARY_REVIEW_DATABASE_URL=review-database-url:latest" \
    --command=python --args="-m,workbench.admin,enrol,${email}${replace}" --max-retries=0
  local execution line="" attempt
  execution="$("${GC[@]}" run jobs execute workbench-enrol --region="$REGION" --wait --format='value(metadata.name)')"
  # The code is only in this execution's log, which only the project's owners read; give it to the
  # operator yourself. Logs arrive a few seconds late, so wait for this execution's own line.
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    line="$("${GC[@]}" logging read "resource.type=\"cloud_run_job\" AND labels.\"run.googleapis.com/execution_name\"=\"$execution\" AND textPayload:\"enrolment code\"" \
      --limit=1 --freshness=1h --format='value(textPayload)')"
    [[ -n "$line" ]] && break
    sleep 5
  done
  echo "${line:-The code is in the log of execution $execution, which has not reached Cloud Logging yet: read it there.}"
}

iap_group() {  # once: let a Google group's members through IAP; administrators then manage people there
  local group="${1:?the group address, e.g. workbench-operators@ruhu.ai}"
  "${GC[@]}" iap web add-iam-policy-binding --resource-type=cloud-run --service="$SERVICE" --region="$REGION" \
    --member="group:${group}" --role=roles/iap.httpsResourceAccessor
  echo "Put ALVARY_REVIEW_IAP_GROUP=${group} in .env and deploy again, so the Operators page names the group."
}

iap_external() {  # once: IAP admits Google accounts outside the organisation (personal Gmail ones)
  # Google's own OAuth client admits only the organisation's accounts; a custom one of type External
  # admits any Google account IAP allows. The workbench's client serves, with a second redirect URI.
  local id secret file
  id="$(setting ALVARY_REVIEW_OIDC_CLIENT_ID)"
  secret="$(setting ALVARY_REVIEW_OIDC_CLIENT_SECRET)"
  [[ -n "$id" && -n "$secret" ]] || { echo "Set ALVARY_REVIEW_OIDC_CLIENT_ID and _SECRET in .env" >&2; exit 1; }
  file="$(mktemp)"
  chmod 600 "$file"
  trap 'rm -f "$file"' RETURN
  printf 'accessSettings:\n  oauthSettings:\n    clientId: %s\n    clientSecret: %s\n' "$id" "$secret" >"$file"
  "${GC[@]}" iap settings set "$file" --resource-type=cloud-run --service="$SERVICE" --region="$REGION" >/dev/null
  cat <<NEXT
IAP now signs people in with the workbench's OAuth client. In the console, on that client, add the
authorised redirect URI below, and set the consent screen's audience to External:
  https://iap.googleapis.com/v1/oauth/clientIds/${id}:handleRedirect
NEXT
}

status() {
  "${GC[@]}" run services describe "$SERVICE" --region="$REGION" --format='value(status.url,status.conditions[0].status)' || true
  "${GC[@]}" sql instances describe "$SQL_INSTANCE" --format='value(state,settings.tier)' || true
}

# A gcloud whose sign-in has lapsed fails every call, which the checks below would read as a missing
# secret or resource: say so first. ("plan" only prints.)
signed_in() {
  gcloud auth print-access-token >/dev/null 2>&1 \
    || { echo "gcloud isn't signed in, or its sign-in expired: run gcloud auth login, then try again." >&2; exit 1; }
}

[[ "${1:-plan}" == plan ]] || signed_in
case "${1:-plan}" in
  plan) plan ;;
  setup) setup ;;
  upload) upload ;;
  deploy) deploy ;;
  operator) shift; operator "$@" ;;
  access) access ;;
  enrol) shift; enrol "$@" ;;
  iap_group) shift; iap_group "$@" ;;
  iap_external) iap_external ;;
  status) status ;;
  *) echo "usage: $0 plan|setup|upload|deploy|operator <email> <name> <roles>|access|enrol <email>|iap_group <group>|iap_external|status"; exit 2 ;;
esac
