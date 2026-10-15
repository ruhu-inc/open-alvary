#!/usr/bin/env bash
# The identity-provider check server (MAC-16) on Cloud Run, for Claude and ChatGPT to reach. Test
# data only; deleted after the checks.
#
#   deployment/gcp/idp_check.sh deploy    # build from the commit, deploy the public test service
#   deployment/gcp/idp_check.sh url       # print its address
#   deployment/gcp/idp_check.sh logs      # its recent check events (never a token)
#   deployment/gcp/idp_check.sh delete    # remove the service and its account
#
# One instance, kept warm during the checks so the grants made on its consent page survive between
# requests (cents a day). It runs as its own service account, which holds no roles at all.
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-ingestion}"
REGION="${GCP_REGION:-europe-west1}"
SERVICE="idp-check"
RUNNER_EMAIL="${SERVICE}@${PROJECT}.iam.gserviceaccount.com"
BUILDER_EMAIL="image-builder@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/open-alvary/${SERVICE}:$(git rev-parse --short HEAD)"
GC=(gcloud --project "$PROJECT" --quiet)

setting() {  # a value from the environment or the git-ignored .env (never printed)
  local name="$1"
  if [[ -n "${!name:-}" ]]; then printf '%s' "${!name}"; return; fi
  grep -E "^${name}=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "'\""
}

url() {
  local number
  number="$("${GC[@]}" projects describe "$PROJECT" --format='value(projectNumber)')"
  printf 'https://%s-%s.%s.run.app\n' "$SERVICE" "$number" "$REGION"
}

deploy() {
  "${GC[@]}" iam service-accounts describe "$RUNNER_EMAIL" >/dev/null 2>&1 \
    || "${GC[@]}" iam service-accounts create "$SERVICE" --display-name="Identity-provider check server (no roles)"
  local context base secret
  context="$(mktemp -d)"
  trap 'rm -rf "$context"' RETURN
  git archive HEAD | tar -x -C "$context"
  "${GC[@]}" builds submit "$context" --region="$REGION" \
    --service-account="projects/$PROJECT/serviceAccounts/$BUILDER_EMAIL" \
    --config="$context/trials/idp_check/cloudbuild.yaml" --substitutions="_IMAGE=$IMAGE"
  base="$(url)"
  secret="$(cat private-staging/idp-check/consent-secret)"
  "${GC[@]}" run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --allow-unauthenticated --min-instances=1 --max-instances=1 --cpu=1 --memory=512Mi \
    --set-env-vars="AUTH0_DOMAIN=$(setting AUTH0_DOMAIN),IDP_CHECK_BASE_URL=$base,IDP_CHECK_CONSENT_SECRET=$secret"
  echo "Deployed at $base/mcp. Next: python -m trials.idp_check.setup --public-url $base"
}

logs() {
  "${GC[@]}" logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\" AND textPayload:\"\\\"check\\\": \\\"idp\\\"\"" \
    --limit="${1:-30}" --freshness=1d --format='value(textPayload)' | tail -r
}

delete() {
  "${GC[@]}" run services delete "$SERVICE" --region="$REGION" || true
  "${GC[@]}" iam service-accounts delete "$RUNNER_EMAIL" || true
}

"${1:?deploy, url, logs or delete}" "${@:2}"
