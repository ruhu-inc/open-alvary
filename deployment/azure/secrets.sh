#!/usr/bin/env bash
# The application's secrets, from Google Cloud's Secret Manager to the environment's Key Vault
# (deployment/azure/README.md, phase 2), value for value: API keys, feed tokens and webhook secrets
# made on Google Cloud keep working only with the same key pepper and feed and webhook keys.
#
#   deployment/azure/secrets.sh plan staging    # what would be copied, and what is missing; copies nothing
#   deployment/azure/secrets.sh copy staging    # copy them (each value goes through a pipe, never printed)
#
# Needs gcloud signed in with access to the secrets, and az signed in with Key Vault Secrets Officer on
# the vault (Terraform grants it to whoever applied env/). A staging rehearsal copies production's
# values; staging is then as sensitive as production until its copy is deleted.

set -euo pipefail

COMMAND="${1:-}"
ENVIRONMENT="${2:-}"
[[ "$COMMAND" == plan || "$COMMAND" == copy ]] && [[ "$ENVIRONMENT" == staging || "$ENVIRONMENT" == prod ]] || {
  echo "Usage: $0 plan|copy staging|prod" >&2
  exit 2
}

SHORT="$([[ "$ENVIRONMENT" == staging ]] && echo stg || echo prod)"
VAULT="open-alvary-${SHORT}-sc-kv"
PUBLIC_PROJECT="${GCP_PROJECT:-open-alvary-prod}"         # the public service's secrets
REVIEW_PROJECT="${GCP_REVIEW_PROJECT:-open-alvary-ingestion}" # the workbench's

# Secret name in Key Vault = its name in Secret Manager (both allow letters, digits and dashes).
PUBLIC=(feed-keys webhook-keys admission-config site-admission-config access-role-password)
ACCOUNTS=(key-pepper consent-secret console-client-secret resend-api-key)
OPTIONAL=(resend-webhook-secret auth0-deletion-client-id auth0-deletion-client-secret)
REVIEW=(review-oidc-client-secret)

exists() { gcloud secrets describe "$2" --project="$1" >/dev/null 2>&1; }

copy_one() {  # project name: the latest version's value into the vault, through a pipe
  local project="$1" name="$2"
  gcloud secrets versions access latest --secret="$name" --project="$project" \
    | az keyvault secret set --vault-name "$VAULT" --name "$name" --file /dev/stdin --encoding utf-8 --output none
  echo "  copied $name"
}

copied=() missing=()
for name in "${PUBLIC[@]}" "${ACCOUNTS[@]}"; do
  if exists "$PUBLIC_PROJECT" "$name"; then copied+=("$PUBLIC_PROJECT/$name"); else missing+=("$PUBLIC_PROJECT/$name"); fi
done
for name in "${OPTIONAL[@]}"; do
  exists "$PUBLIC_PROJECT" "$name" && copied+=("$PUBLIC_PROJECT/$name")
done
for name in "${REVIEW[@]}"; do
  if exists "$REVIEW_PROJECT" "$name"; then copied+=("$REVIEW_PROJECT/$name"); else missing+=("$REVIEW_PROJECT/$name"); fi
done

if [[ "$COMMAND" == plan ]]; then
  echo "Into Key Vault $VAULT, by name:"
  printf '  %s\n' "${copied[@]}"
  ((${#missing[@]})) && { echo "Not in Secret Manager (accounts may be off):"; printf '  %s\n' "${missing[@]}"; }
  cat <<'PLAN'
Made elsewhere, not copied:
  database-url, database-password, review-database-url   Terraform, from the new servers' passwords
  access-database-url                 copy composes it: the access role's password, the new host
  workbench-entra-client-secret       the owner, from the workbench's Entra app registration
  google-wif-config                   the owner, gcloud iam workload-identity-pools create-cred-config
List the optional ones copied in optional_secrets (env/*.tfvars).
PLAN
  exit 0
fi

az keyvault show --name "$VAULT" --output none # the vault is there and reachable from here
for entry in "${copied[@]}"; do
  copy_one "${entry%%/*}" "${entry#*/}"
done

# The workbench's Access section connects to the public database as open_alvary_access: the same
# password, the new host. Composed in the pipe; the password is never in a variable or argument.
if exists "$PUBLIC_PROJECT" access-role-password; then
  host="$(az postgres flexible-server show --resource-group "open-alvary-${ENVIRONMENT}-sc-rg" \
    --name "open-alvary-${ENVIRONMENT}-sc-pg" --query fullyQualifiedDomainName --output tsv)"
  gcloud secrets versions access latest --secret=access-role-password --project="$PUBLIC_PROJECT" \
    | python3 -c 'import sys, urllib.parse; host = sys.argv[1]; pw = urllib.parse.quote(sys.stdin.read().strip(), safe=""); sys.stdout.write(f"postgresql+psycopg://open_alvary_access:{pw}@{host}:5432/open_alvary?sslmode=require")' "$host" \
    | az keyvault secret set --vault-name "$VAULT" --name access-database-url --file /dev/stdin --encoding utf-8 --output none
  echo "  composed access-database-url"
fi
echo "Done. Next: deployment/azure/release.sh $ENVIRONMENT public"
