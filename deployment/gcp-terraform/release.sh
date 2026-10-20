#!/usr/bin/env bash
# A release from the commit (deployment/gcp-terraform/README.md, phase 3): the image is built by Cloud
# Build in the environment's project from `git archive HEAD`, so nothing untracked or private enters
# it, as deployment/gcp/public_service.sh deploy does today. For the public service, the migrations job
# then runs on the new image and must succeed before Terraform gives the image to the service and its
# other jobs (today's deploy: build, migrate, then the new revision).
#
#   deployment/gcp-terraform/release.sh staging public       # build, then migrate, then print the apply
#   deployment/gcp-terraform/release.sh staging workbench    # build
#   deployment/gcp-terraform/release.sh staging ingestion    # build
#   deployment/gcp-terraform/release.sh staging restore      # build the database restore job (phase 5)
#   COMMIT=2163537 deployment/gcp-terraform/release.sh staging public   # an earlier commit: the one live today
#
# COMMIT builds that commit instead of HEAD (it must be on main). The build instructions are this
# tree's (cloudbuild.yaml), since an earlier commit may not have them.
#
# The project is read from env/<environment>.tfvars. Needs gcloud signed in as someone who may act as
# the builder account and run the project's jobs (the project's owner may).

set -euo pipefail

ENVIRONMENT="${1:-}"
WHAT="${2:-}"
[[ "$ENVIRONMENT" == staging || "$ENVIRONMENT" == prod ]] && [[ "$WHAT" =~ ^(public|workbench|ingestion|restore)$ ]] || {
  echo "Usage: $0 staging|prod public|workbench|ingestion|restore" >&2
  exit 2
}
REVISION="${COMMIT:-HEAD}"
if [[ "$REVISION" == HEAD ]]; then
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo "Commit first: the image is the commit" >&2; exit 1; }
else
  git merge-base --is-ancestor "$REVISION" main 2>/dev/null || { echo "$REVISION is not on main" >&2; exit 1; }
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
tfvar() {  # name: its quoted value in env/<environment>.tfvars, else nothing
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$HERE/env/${ENVIRONMENT}.tfvars" 2>/dev/null | head -1
}
PROJECT="$(tfvar project_id)"
REGION="$(tfvar region)"
REGION="${REGION:-europe-west1}"
[[ -n "$PROJECT" ]] || { echo "No project_id in env/${ENVIRONMENT}.tfvars" >&2; exit 1; }
[[ "$PROJECT" != open-alvary-prod && "$PROJECT" != open-alvary-ingestion ]] \
  || { echo "$PROJECT was built by deployment/gcp/*.sh: never release into it from here (README)" >&2; exit 1; }
GC=(gcloud --project "$PROJECT" --quiet)
"${GC[@]}" artifacts repositories describe open-alvary --location="$REGION" >/dev/null 2>&1 \
  || { echo "No registry in $PROJECT: apply env/ first" >&2; exit 1; }
COMMIT="$(git rev-parse --short "$REVISION")"
BUILDER="open-alvary-${ENVIRONMENT}-builder@${PROJECT}.iam.gserviceaccount.com"

case "$WHAT" in
  public) NAME=public-service DOCKERFILE=deployment/public-service.Dockerfile ;;
  workbench) NAME=workbench DOCKERFILE=deployment/workbench.Dockerfile ;;
  ingestion) NAME=ingestion DOCKERFILE=deployment/ingestion-jobs.Dockerfile ;;
  restore) NAME=restore DOCKERFILE=deployment/azure/restore.Dockerfile ;;  # the same job for both clouds
esac
git cat-file -e "$REVISION:$DOCKERFILE" 2>/dev/null || { echo "No $DOCKERFILE at $COMMIT" >&2; exit 1; }
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/open-alvary/${NAME}:${COMMIT}"

context="$(mktemp -d)"
trap 'rm -rf "$context"' EXIT
git archive "$REVISION" | tar -x -C "$context"
"${GC[@]}" builds submit "$context" --region="$REGION" \
  --service-account="projects/$PROJECT/serviceAccounts/$BUILDER" \
  --gcs-source-staging-dir="gs://${PROJECT}_cloudbuild/source" \
  --config="$HERE/cloudbuild.yaml" \
  --substitutions="_IMAGE=$IMAGE,_DOCKERFILE=$DOCKERFILE"
echo "Built $IMAGE"

if [[ "$WHAT" == public ]]; then
  JOB="open-alvary-${ENVIRONMENT}-migrate"
  if "${GC[@]}" run jobs describe "$JOB" --region="$REGION" >/dev/null 2>&1; then
    "${GC[@]}" run jobs update "$JOB" --region="$REGION" --image="$IMAGE" >/dev/null
    echo "Migrating…"
    "${GC[@]}" run jobs execute "$JOB" --region="$REGION" --wait \
      || { echo "Migrations failed: the service keeps its image. See the job's logs." >&2; exit 1; }
    echo "Migrations succeeded"
  else
    echo "First release: create the migrations job alone, then run this script again:"
    echo "  terraform apply -var-file=${ENVIRONMENT}.tfvars -var public_image=$IMAGE -target='google_cloud_run_v2_job.public[\"migrate\"]'"
    exit 0
  fi
fi

echo "Next, in deployment/gcp-terraform/env: terraform apply -var-file=${ENVIRONMENT}.tfvars -var ${WHAT}_image=$IMAGE"
