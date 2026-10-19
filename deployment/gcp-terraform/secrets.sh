#!/usr/bin/env bash
# The application's secrets, into the environment's Secret Manager (deployment/gcp-terraform/README.md,
# phase 2), value for value, from today's Secret Manager or from Azure's Key Vault (the names are the
# same in all three): API keys, feed tokens and webhook secrets keep working only with the same key
# pepper and feed and webhook keys.
#
#   deployment/gcp-terraform/secrets.sh plan staging [gcp|azure]   # what would be copied, and what is missing
#   deployment/gcp-terraform/secrets.sh copy staging [gcp|azure]   # copy them (each value through a pipe, never printed)
#
# gcp (the default) reads open-alvary-prod and open-alvary-ingestion (GCP_PROJECT, GCP_REVIEW_PROJECT);
# azure reads the live Azure environment's vault (AZURE_VAULT, default open-alvary-prod-sc-kv), for
# the move back. Terraform made each secret's container, so a value lands only where it is expected;
# a value already there unchanged is not added again. Needs gcloud signed in with access to both
# projects' secrets (and az, for azure). The target is the project in env/<environment>.tfvars.

set -euo pipefail

COMMAND="${1:-}"
ENVIRONMENT="${2:-}"
SOURCE="${3:-gcp}"
[[ "$COMMAND" == plan || "$COMMAND" == copy ]] && [[ "$ENVIRONMENT" == staging || "$ENVIRONMENT" == prod ]] \
  && [[ "$SOURCE" == gcp || "$SOURCE" == azure ]] || {
  echo "Usage: $0 plan|copy staging|prod [gcp|azure]" >&2
  exit 2
}

HERE="$(cd "$(dirname "$0")" && pwd)"
tfvar() {  # name: its quoted value in env/<environment>.tfvars, else nothing
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$HERE/env/${ENVIRONMENT}.tfvars" 2>/dev/null | head -1
}
PROJECT="$(tfvar project_id)"
[[ -n "$PROJECT" ]] || { echo "No project_id in env/${ENVIRONMENT}.tfvars" >&2; exit 1; }
[[ "$PROJECT" != open-alvary-prod && "$PROJECT" != open-alvary-ingestion ]] \
  || { echo "$PROJECT is a source, never the target (README)" >&2; exit 1; }
PUBLIC_PROJECT="${GCP_PROJECT:-open-alvary-prod}"             # the public service's secrets today
REVIEW_PROJECT="${GCP_REVIEW_PROJECT:-open-alvary-ingestion}" # the workbench's
VAULT="${AZURE_VAULT:-open-alvary-prod-sc-kv}"

PUBLIC=(feed-keys webhook-keys admission-config site-admission-config access-role-password)
ACCOUNTS=(key-pepper consent-secret console-client-secret resend-api-key)
OPTIONAL=(resend-webhook-secret auth0-deletion-client-id auth0-deletion-client-secret)
REVIEW=(review-oidc-client-secret)

where() {  # name: the place its value is read from (project/name, or vault/name)
  if [[ "$SOURCE" == azure ]]; then printf '%s/%s' "$VAULT" "$1"
  elif [[ " ${REVIEW[*]} " == *" $1 "* ]]; then printf '%s/%s' "$REVIEW_PROJECT" "$1"
  else printf '%s/%s' "$PUBLIC_PROJECT" "$1"; fi
}

exists() {  # place
  if [[ "$SOURCE" == azure ]]; then
    az keyvault secret show --vault-name "${1%%/*}" --name "${1#*/}" --query id --output tsv >/dev/null 2>&1
  else
    gcloud secrets describe "${1#*/}" --project="${1%%/*}" >/dev/null 2>&1
  fi
}

value() {  # place: the latest value, exactly, to standard output (only ever into a pipe)
  if [[ "$SOURCE" == azure ]]; then
    az keyvault secret show --vault-name "${1%%/*}" --name "${1#*/}" --query value --output json \
      | python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin))'
  else
    gcloud secrets versions access latest --secret="${1#*/}" --project="${1%%/*}"
  fi
}

container() { gcloud secrets describe "$1" --project="$PROJECT" >/dev/null 2>&1; }

copy_one() {  # place: into the same name here, unless the latest version already holds it
  local place="$1" name="${1#*/}" theirs ours
  theirs="$(value "$place" | shasum -a 256)"
  ours="$(gcloud secrets versions access latest --secret="$name" --project="$PROJECT" 2>/dev/null | shasum -a 256 || true)"
  if [[ "$theirs" == "$ours" ]]; then
    echo "  unchanged $name"
  else
    value "$place" | gcloud secrets versions add "$name" --project="$PROJECT" --data-file=- >/dev/null
    echo "  copied $name"
  fi
}

copied=() missing=() uncontained=()
for name in "${PUBLIC[@]}" "${ACCOUNTS[@]}" "${REVIEW[@]}"; do
  place="$(where "$name")"
  if exists "$place"; then copied+=("$place"); else missing+=("$place"); fi
done
for name in "${OPTIONAL[@]}"; do
  place="$(where "$name")"
  exists "$place" && copied+=("$place")
done
for place in "${copied[@]}"; do
  container "${place#*/}" || uncontained+=("${place#*/}")
done

if [[ "$COMMAND" == plan ]]; then
  echo "Into Secret Manager in $PROJECT, by name:"
  printf '  %s\n' "${copied[@]}"
  ((${#missing[@]})) && { echo "Not in the source (accounts may be off):"; printf '  %s\n' "${missing[@]}"; }
  ((${#uncontained[@]})) && { echo "No container here yet (optional ones: list them in optional_secrets and apply):"; printf '  %s\n' "${uncontained[@]}"; }
  cat <<'PLAN'
Made elsewhere, not copied:
  database-url, database-password, review-database-url   Terraform, from the new instances
  access-database-url                                    copy composes it: the access role's password, the new host
  the workbench's IAP sign-in client                     the owner, gcloud iap settings set (README, phase 4)
Not needed on Google Cloud: google-wif-config (Document AI is reached natively).
PLAN
  exit 0
fi

((${#uncontained[@]} == 0)) || { echo "No container in $PROJECT for: ${uncontained[*]} (optional_secrets, then apply)" >&2; exit 1; }
for place in "${copied[@]}"; do
  copy_one "$place"
done

# The workbench's Access section connects to the public database as open_alvary_access: the same
# password, the new host. Composed in the pipe; the password is never in a variable or argument.
access="$(where access-role-password)"
if exists "$access" && container access-database-url; then
  host="$(gcloud sql instances describe "open-alvary-${ENVIRONMENT}-pg" --project="$PROJECT" --format=json \
    | python3 -c 'import json, sys; print(next(a["ipAddress"] for a in json.load(sys.stdin)["ipAddresses"] if a["type"] == "PRIVATE"))')"
  value "$access" \
    | python3 -c 'import sys, urllib.parse; host = sys.argv[1]; pw = urllib.parse.quote(sys.stdin.read().strip(), safe=""); sys.stdout.write(f"postgresql+psycopg://open_alvary_access:{pw}@{host}:5432/open_alvary?sslmode=require")' "$host" \
    | gcloud secrets versions add access-database-url --project="$PROJECT" --data-file=- >/dev/null
  echo "  composed access-database-url"
fi
echo "Done. Next: deployment/gcp-terraform/release.sh $ENVIRONMENT public"
