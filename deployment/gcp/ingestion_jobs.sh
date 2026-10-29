#!/usr/bin/env bash
# The corpus's ingestion jobs in the cloud (project open-alvary-ingestion, EU), step by step:
#
#   deployment/gcp/ingestion_jobs.sh plan            # print what would be created; creates nothing
#   deployment/gcp/ingestion_jobs.sh setup           # APIs, registry, the jobs' service accounts and access
#   deployment/gcp/ingestion_jobs.sh deploy          # build the image from the committed code, deploy both jobs
#   deployment/gcp/ingestion_jobs.sh run ocr 2       # read scanned pages with Google's OCR, in 2 tasks
#   deployment/gcp/ingestion_jobs.sh run layout 1    # read them with the Layout Parser, in 1 task
#   deployment/gcp/ingestion_jobs.sh run check 8     # Google's OCR checks born-digital pages' text layer, in 8
#                                                    # tasks (the work list: python -m ingestion.rwanda check-worklist)
#   deployment/gcp/ingestion_jobs.sh run survey 8 1 ng  # another collection (ingestion.cloud_read): survey, then
#                                                    # layout and ocr from its reading work list
#   deployment/gcp/ingestion_jobs.sh prepare 8       # prepare every stored file for review, in 8 tasks
#   deployment/gcp/ingestion_jobs.sh prepare 8 ng    # Nigeria: the National Industrial Court's stored judgment
#                                                    # pages into collections/ng/ (web pages: no OCR billed)
#   deployment/gcp/ingestion_jobs.sh prepare 4 ng nass-acts,nln-repository,supreme-court
#                                                    # Nigeria's PDFs once read (run layout, then ocr, with
#                                                    # country ng); a file not yet read waits
#   deployment/gcp/ingestion_jobs.sh audit 8         # write no document: account for every printed line of
#                                                    # every stored file (ingestion.accounting), into
#                                                    # work/<country>/audit/lines/<execution>/ (PREPARE_LIMIT=3
#                                                    # in the environment: a trial of 3 files per task)
#   deployment/gcp/ingestion_jobs.sh audit 4 ng nicn-judgments,nass-acts,nln-repository,supreme-court
#   PREPARE_COLLECTIONS=collections-next deployment/gcp/ingestion_jobs.sh prepare 12
#                                                    # a new copy beside the live collections, compared
#                                                    # (python -m workbench.admin impact) before switching
#   deployment/gcp/ingestion_jobs.sh fetch 4         # fetch the approved Nigerian work list, in 4 tasks
#                                                    # (the list: python -m ingestion.nigeria fetch-worklist)
#   deployment/gcp/ingestion_jobs.sh status
#
# scan-reader reads the work list in the corpus bucket, which the operator writes with the approved
# budget (python -m ingestion.rwanda scan-worklist ...); each reading lands in
# gs://$CORPUS_BUCKET/readings/<country>/<ocr|docai>/. corpus-preparer reads the sources, readings and
# state (python -m ingestion.rwanda push-held mirrors the ledger and catalogues) and writes the review
# documents to gs://$CORPUS_BUCKET/collections/<country>/. corpus-fetcher fetches a country's approved
# work list (work/<country>/fetch.json), shared out by host so that one host is never fetched by two
# tasks, politely (robots.txt, at least 2 s between requests to a host), and stores each file once by
# its fingerprint in gs://$CORPUS_BUCKET/sources/<country>/<source>/ with a sidecar. The National
# Industrial Court's ids not yet catalogued are in the list too: the job reads each record page once,
# stores it and builds its catalogue entry from it, so that host is one task's for many hours. Then
# python -m ingestion.nigeria pull-fetched records what it did in the ledger, NICN entries included. Any run that stops is
# run again: work already done is skipped. Billed: Document AI per page read, Cloud Run per
# task-hour (cents); fetching costs Cloud Run time and egress only.
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-ingestion}"
REGION="${GCP_REGION:-europe-west1}"
REPOSITORY="open-alvary"
SCAN_JOB="scan-reader"
PREPARE_JOB="corpus-preparer"
FETCH_JOB="corpus-fetcher"
# The jobs run as this account: Document AI use, and objects in the corpus bucket only.
RUNNER="scan-reader"
RUNNER_EMAIL="${RUNNER}@${PROJECT}.iam.gserviceaccount.com"
# Builds run as their own account: the project's default compute account has no rights here.
BUILDER="image-builder"
BUILDER_EMAIL="${BUILDER}@${PROJECT}.iam.gserviceaccount.com"
BUILD_SOURCES="${PROJECT}_cloudbuild"
CORPUS_BUCKET="${ALVARY_CORPUS_BUCKET:-open-alvary-corpus}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/ingestion-jobs:$(git rev-parse --short HEAD)"
GC=(gcloud --project "$PROJECT" --quiet)

setting() {  # a value from the environment or the git-ignored .env (never printed)
  local name="$1"
  if [[ -n "${!name:-}" ]]; then printf '%s' "${!name}"; return; fi
  grep -E "^${name}=" .env 2>/dev/null | head -1 | cut -d= -f2-
}

grant() {  # an IAM binding; a service account just created takes a moment to be known, so retry
  local attempt
  for attempt in 1 2 3 4 5 6; do
    "$@" >/dev/null 2>&1 && return 0
    sleep 10
  done
  "$@" >/dev/null
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
  - Cloud Run jobs $SCAN_JOB (per reader: ocr, layout) and $PREPARE_JOB, 2 vCPU and 4 GiB per task;
    $FETCH_JOB (fetches an approved work list from official sites), 1 vCPU and 2 GiB per task
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
  grant "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER_EMAIL" \
    --role=roles/documentai.apiUser --condition=None
  grant "${GC[@]}" storage buckets add-iam-policy-binding "gs://$CORPUS_BUCKET" \
    --member="serviceAccount:$RUNNER_EMAIL" --role=roles/storage.objectUser
  "${GC[@]}" iam service-accounts describe "$BUILDER_EMAIL" >/dev/null 2>&1 ||
    "${GC[@]}" iam service-accounts create "$BUILDER" --display-name="Image builds (Cloud Build)"
  "${GC[@]}" storage buckets describe "gs://$BUILD_SOURCES" >/dev/null 2>&1 ||
    "${GC[@]}" storage buckets create "gs://$BUILD_SOURCES" --location=EU --uniform-bucket-level-access \
      --public-access-prevention
  grant "${GC[@]}" storage buckets add-iam-policy-binding "gs://$BUILD_SOURCES" \
    --member="serviceAccount:$BUILDER_EMAIL" --role=roles/storage.objectViewer
  grant "${GC[@]}" artifacts repositories add-iam-policy-binding "$REPOSITORY" --location="$REGION" \
    --member="serviceAccount:$BUILDER_EMAIL" --role=roles/artifactregistry.writer
  grant "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$BUILDER_EMAIL" \
    --role=roles/logging.logWriter --condition=None
  echo "Set up: $RUNNER_EMAIL (runs the jobs) and $BUILDER_EMAIL (builds images)."
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
    --config="$context/deployment/gcp/ingestion-jobs.cloudbuild.yaml" --substitutions="_IMAGE=$IMAGE"
  local env=ALVARY_CORPUS_BUCKET="$CORPUS_BUCKET"
  for name in ALVARY_DOCAI_PROJECT ALVARY_DOCAI_LOCATION ALVARY_DOCAI_PROCESSOR_ID ALVARY_DOCAI_PROCESSOR_VERSION \
    ALVARY_DOCAI_OCR_PROCESSOR_ID ALVARY_DOCAI_OCR_PROCESSOR_VERSION; do
    local value
    value="$(setting "$name")"
    [[ -n "$value" ]] || { echo "Set $name in the environment or .env" >&2; exit 1; }
    env+=",$name=$value"
  done
  "${GC[@]}" run jobs deploy "$SCAN_JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --args=ingestion.scan_job --tasks=1 --max-retries=1 --task-timeout="${SCAN_TASK_TIMEOUT:-168h}" \
    --cpu=2 --memory=4Gi --set-env-vars="$env,SCAN_READER=ocr"
  "${GC[@]}" run jobs deploy "$PREPARE_JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --args=ingestion.prepare_job --tasks=1 --max-retries=1 --task-timeout=24h --cpu=2 --memory=4Gi \
    --set-env-vars="$env"
  # The fetcher names itself to the sites it reads (OpenAlvaryBot), with a contact when one is set.
  local fetch_env="ALVARY_CORPUS_BUCKET=$CORPUS_BUCKET,FETCH_COUNTRY=${FETCH_COUNTRY:-ng}" contact
  contact="$(setting ALVARY_CRAWLER_CONTACT)"
  [[ -z "$contact" ]] || fetch_env+=",ALVARY_CRAWLER_CONTACT=$contact"
  "${GC[@]}" run jobs deploy "$FETCH_JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --args=ingestion.fetch_job --tasks=1 --max-retries=0 --task-timeout="${FETCH_TASK_TIMEOUT:-48h}" \
    --cpu=1 --memory=2Gi --set-env-vars="$fetch_env"
}

run() {
  local reader="${1:?ocr, layout, check or survey}" tasks="${2:-1}" files="${3:-}" country="${4:-rw}"
  # The Layout Parser's quota is per minute for the whole project: one file at a time is enough.
  [[ -n "$files" ]] || { [[ "$reader" == "layout" ]] && files=1 || files=4; }
  "${GC[@]}" run jobs execute "$SCAN_JOB" --region="$REGION" --tasks="$tasks" --async \
    --update-env-vars="SCAN_READER=$reader,SCAN_FILES_AT_ONCE=$files,SCAN_COUNTRY=$country"
}

# The variables are separated by ";" (gcloud's "^;^" form), as a list of sources holds commas.
prepare() {  # prepare [tasks] [country] [sources]: rw by default; ng prepares the National Industrial Court
  local tasks="${1:-8}" country="${2:-rw}" sources="${3:-}"
  local env="PREPARE_COUNTRY=$country;PREPARE_MODE=prepare;PREPARE_COLLECTIONS=${PREPARE_COLLECTIONS:-collections}"
  if [[ "$country" == ng ]]; then env+=";PREPARE_SOURCES=${sources:-nicn-judgments}"; fi
  "${GC[@]}" run jobs execute "$PREPARE_JOB" --region="$REGION" --tasks="$tasks" --async --update-env-vars="^;^$env"
}

audit() {  # audit [tasks] [country] [sources]: as prepare, writing no document; each line's account instead
  # (AUDIT=details: each work's first draft of details measured instead, workbench.details_audit)
  local tasks="${1:-8}" country="${2:-rw}" sources="${3:-}"
  local env="PREPARE_COUNTRY=$country;PREPARE_MODE=${AUDIT:-audit}"
  if [[ "$country" == ng ]]; then env+=";PREPARE_SOURCES=${sources:-nicn-judgments}"; fi
  [[ -z "${PREPARE_LIMIT:-}" ]] || env+=";PREPARE_LIMIT=$PREPARE_LIMIT"
  "${GC[@]}" run jobs execute "$PREPARE_JOB" --region="$REGION" --tasks="$tasks" --async --update-env-vars="^;^$env"
}

fetch() {  # more tasks than the work list has hosts only leaves some idle: each host is one task's
  local tasks="${1:-4}" country="${2:-ng}"
  "${GC[@]}" run jobs execute "$FETCH_JOB" --region="$REGION" --tasks="$tasks" --async \
    --update-env-vars="FETCH_COUNTRY=$country"
}

status() {
  for job in "$SCAN_JOB" "$PREPARE_JOB" "$FETCH_JOB"; do
    "${GC[@]}" run jobs executions list --job="$job" --region="$REGION" --limit=5 2>/dev/null || true
  done
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
  deploy) deploy ;;
  run) shift; run "$@" ;;
  prepare) shift; prepare "$@" ;;
  audit) shift; audit "$@" ;;
  fetch) shift; fetch "$@" ;;
  status) status ;;
  *) echo "usage: $0 plan|setup|deploy|run <ocr|layout> [tasks] [files-at-once]|prepare [tasks] [rw|ng] [sources]|audit [tasks] [rw|ng] [sources]|fetch [tasks] [country]|status" >&2; exit 2 ;;
esac
