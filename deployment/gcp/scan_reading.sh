#!/usr/bin/env bash
# Reading the corpus's scanned pages in the cloud (project open-alvary-ingestion, EU), step by step:
#
#   deployment/gcp/scan_reading.sh plan            # print what would be created; creates nothing
#   deployment/gcp/scan_reading.sh setup           # APIs, registry, the job's service account and its access
#   deployment/gcp/scan_reading.sh deploy          # build the image from the committed code, deploy the job
#   deployment/gcp/scan_reading.sh run ocr 2       # read with Google's OCR, in 2 tasks
#   deployment/gcp/scan_reading.sh run layout 1    # read with the Layout Parser, in 1 task
#   deployment/gcp/scan_reading.sh status
#
# The job reads the work list in the corpus bucket, which the operator writes with the approved
# budget (python -m ingestion.rwanda scan-worklist ...); each reading lands in
# gs://$CORPUS_BUCKET/readings/<country>/<ocr|docai>/. A run that stops is run again: files already
# read are skipped. Billed per page read (Document AI), plus a few cents of Cloud Run time.
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-ingestion}"
REGION="${GCP_REGION:-europe-west1}"
REPOSITORY="open-alvary"
JOB="scan-reader"
RUNNER="scan-reader"
RUNNER_EMAIL="${RUNNER}@${PROJECT}.iam.gserviceaccount.com"
# Builds run as their own account: the project's default compute account has no rights here.
BUILDER="image-builder"
BUILDER_EMAIL="${BUILDER}@${PROJECT}.iam.gserviceaccount.com"
BUILD_SOURCES="${PROJECT}_cloudbuild"
CORPUS_BUCKET="${ALVARY_CORPUS_BUCKET:-open-alvary-corpus}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/${JOB}:$(git rev-parse --short HEAD)"
GC=(gcloud --project "$PROJECT" --quiet)

setting() {  # a value from the environment or the git-ignored .env (never printed)
  local name="$1"
  if [[ -n "${!name:-}" ]]; then printf '%s' "${!name}"; return; fi
  grep -E "^${name}=" .env 2>/dev/null | head -1 | cut -d= -f2-
}

plan() {
  cat <<PLAN
Project $PROJECT, region $REGION. Would create (if absent):
  - APIs: Cloud Run, Artifact Registry, Cloud Build
  - Artifact Registry repository $REPOSITORY (Docker images)
  - Service account $RUNNER_EMAIL: Document AI API user on the project; read and write objects
    in gs://$CORPUS_BUCKET only
  - Service account $BUILDER_EMAIL for image builds: read the uploaded sources in
    gs://$BUILD_SOURCES, push to the $REPOSITORY registry, write build logs
  - Cloud Run job $JOB (2 vCPU, 2 GiB per task), run per reader: ocr, then layout
Billed: Document AI per page read (Enterprise OCR \$1.50, Layout Parser \$10 per 1,000 pages);
Cloud Run a few cents per hour of a task; the registry a few cents a month.
PLAN
}

setup() {
  "${GC[@]}" services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
  "${GC[@]}" artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1 ||
    "${GC[@]}" artifacts repositories create "$REPOSITORY" --repository-format=docker --location="$REGION"
  "${GC[@]}" iam service-accounts describe "$RUNNER_EMAIL" >/dev/null 2>&1 ||
    "${GC[@]}" iam service-accounts create "$RUNNER" --display-name="Scan reading (Cloud Run job)"
  "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER_EMAIL" \
    --role=roles/documentai.apiUser --condition=None >/dev/null
  "${GC[@]}" storage buckets add-iam-policy-binding "gs://$CORPUS_BUCKET" \
    --member="serviceAccount:$RUNNER_EMAIL" --role=roles/storage.objectUser >/dev/null
  "${GC[@]}" iam service-accounts describe "$BUILDER_EMAIL" >/dev/null 2>&1 ||
    "${GC[@]}" iam service-accounts create "$BUILDER" --display-name="Image builds (Cloud Build)"
  "${GC[@]}" storage buckets describe "gs://$BUILD_SOURCES" >/dev/null 2>&1 ||
    "${GC[@]}" storage buckets create "gs://$BUILD_SOURCES" --location=EU --uniform-bucket-level-access \
      --public-access-prevention
  "${GC[@]}" storage buckets add-iam-policy-binding "gs://$BUILD_SOURCES" \
    --member="serviceAccount:$BUILDER_EMAIL" --role=roles/storage.objectViewer >/dev/null
  "${GC[@]}" artifacts repositories add-iam-policy-binding "$REPOSITORY" --location="$REGION" \
    --member="serviceAccount:$BUILDER_EMAIL" --role=roles/artifactregistry.writer >/dev/null
  "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$BUILDER_EMAIL" \
    --role=roles/logging.logWriter --condition=None >/dev/null
  echo "Set up: $RUNNER_EMAIL (reads scans) and $BUILDER_EMAIL (builds images)."
}

deploy() {
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
    --config="$context/deployment/gcp/scan-reader.cloudbuild.yaml" --substitutions="_IMAGE=$IMAGE"
  local env=ALVARY_CORPUS_BUCKET="$CORPUS_BUCKET"
  for name in ALVARY_DOCAI_PROJECT ALVARY_DOCAI_LOCATION ALVARY_DOCAI_PROCESSOR_ID ALVARY_DOCAI_PROCESSOR_VERSION \
    ALVARY_DOCAI_OCR_PROCESSOR_ID ALVARY_DOCAI_OCR_PROCESSOR_VERSION; do
    local value
    value="$(setting "$name")"
    [[ -n "$value" ]] || { echo "Set $name in the environment or .env" >&2; exit 1; }
    env+=",$name=$value"
  done
  "${GC[@]}" run jobs deploy "$JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --tasks=1 --max-retries=1 --task-timeout="${SCAN_TASK_TIMEOUT:-168h}" --cpu=2 --memory=2Gi \
    --set-env-vars="$env,SCAN_READER=ocr"
}

run() {
  local reader="${1:?ocr or layout}" tasks="${2:-1}" files="${3:-}"
  # The Layout Parser's quota is per minute for the whole project: one file at a time is enough.
  [[ -n "$files" ]] || { [[ "$reader" == "layout" ]] && files=1 || files=4; }
  "${GC[@]}" run jobs execute "$JOB" --region="$REGION" --tasks="$tasks" --async \
    --update-env-vars="SCAN_READER=$reader,SCAN_FILES_AT_ONCE=$files"
}

status() {
  "${GC[@]}" run jobs executions list --job="$JOB" --region="$REGION" --limit=10 || true
}

case "${1:-plan}" in
  plan) plan ;;
  setup) setup ;;
  deploy) deploy ;;
  run) shift; run "$@" ;;
  status) status ;;
  *) echo "usage: $0 plan|setup|deploy|run <ocr|layout> [tasks] [files-at-once]|status" >&2; exit 2 ;;
esac
