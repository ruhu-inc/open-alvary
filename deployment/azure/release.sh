#!/usr/bin/env bash
# A release from the commit (deployment/azure/README.md, phase 3): the image is built in the
# environment's registry from `git archive HEAD`, so nothing untracked or private enters it, as Cloud
# Build did. For the public service, the migrations job then runs on the new image and must succeed
# before Terraform gives the image to the service and its other jobs.
#
#   deployment/azure/release.sh staging public       # build, then migrate, then print the apply
#   deployment/azure/release.sh staging workbench    # build
#   deployment/azure/release.sh staging ingestion    # build
#
# Needs az signed in, with AcrPush on the registry and Contributor on the resource group.

set -euo pipefail

ENVIRONMENT="${1:-}"
WHAT="${2:-}"
[[ "$ENVIRONMENT" == staging || "$ENVIRONMENT" == prod ]] && [[ "$WHAT" =~ ^(public|workbench|ingestion)$ ]] || {
  echo "Usage: $0 staging|prod public|workbench|ingestion" >&2
  exit 2
}
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo "Commit first: the image is the commit" >&2; exit 1; }

SHORT="$([[ "$ENVIRONMENT" == staging ]] && echo stg || echo prod)"
GROUP="open-alvary-${ENVIRONMENT}-sc-rg"
REGISTRY="$(az acr list --resource-group "$GROUP" --query "[0].name" --output tsv)"
[[ -n "$REGISTRY" ]] || { echo "No registry in $GROUP: apply env/ first" >&2; exit 1; }
COMMIT="$(git rev-parse --short HEAD)"

case "$WHAT" in
  public) NAME=public-service DOCKERFILE=deployment/public-service.Dockerfile ;;
  workbench) NAME=workbench DOCKERFILE=deployment/workbench.Dockerfile ;;
  ingestion) NAME=ingestion DOCKERFILE=deployment/ingestion-jobs.Dockerfile ;;
esac
[[ -f "$DOCKERFILE" ]] || { echo "No $DOCKERFILE at this commit" >&2; exit 1; }

context="$(mktemp -d)"
trap 'rm -rf "$context"' EXIT
git archive HEAD | tar -x -C "$context"
az acr build --registry "$REGISTRY" --image "$NAME:$COMMIT" --file "$context/$DOCKERFILE" "$context" --output none
IMAGE="$(az acr show --name "$REGISTRY" --query loginServer --output tsv)/$NAME:$COMMIT"
echo "Built $IMAGE"

if [[ "$WHAT" == public ]]; then
  JOB="open-alvary-${SHORT}-sc-migrate"
  if az containerapp job show --name "$JOB" --resource-group "$GROUP" --output none 2>/dev/null; then
    az containerapp job update --name "$JOB" --resource-group "$GROUP" --image "$IMAGE" --output none
    execution="$(az containerapp job start --name "$JOB" --resource-group "$GROUP" --query name --output tsv)"
    echo "Migrating ($execution)…"
    while :; do
      status="$(az containerapp job execution show --name "$JOB" --resource-group "$GROUP" \
        --job-execution-name "$execution" --query properties.status --output tsv)"
      case "$status" in
        Succeeded) echo "Migrations succeeded"; break ;;
        Failed|Stopped|Degraded) echo "Migrations $status: the service keeps its image. See the job's logs." >&2; exit 1 ;;
      esac
      sleep 10
    done
  else
    echo "First release: create the migrations job alone, then run this script again:"
    echo "  terraform apply -var-file=${ENVIRONMENT}.tfvars -var public_image=$IMAGE -target='azurerm_container_app_job.public[\"migrate\"]'"
    exit 0
  fi
fi

echo "Next, in deployment/azure/env: terraform apply -var-file=${ENVIRONMENT}.tfvars -var ${WHAT}_image=$IMAGE"
