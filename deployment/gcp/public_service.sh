#!/usr/bin/env bash
# The public service on Google Cloud (MAC-12, D7: one front door), step by step:
#
#   deployment/gcp/public_service.sh plan      # what would be created, the settings, the monthly cost
#   deployment/gcp/public_service.sh project   # create the project, linked to the ingestion project's billing
#   deployment/gcp/public_service.sh setup     # APIs, registry, service accounts, database, secrets
#   deployment/gcp/public_service.sh deploy    # build from the commit, migrate and seed, deploy the service
#   deployment/gcp/public_service.sh edge      # load balancer, Cloud Armor, certificate for the domain
#   deployment/gcp/public_service.sh dns       # the DNS records the owner adds (certificate, then cutover)
#   deployment/gcp/public_service.sh verify    # MAC-15 acceptance A checks through the load balancer
#   deployment/gcp/public_service.sh status
#
# One Cloud Run service (api.service with the built website) reached only through one external HTTPS
# load balancer with Cloud Armor; its default address is off, so the edge is the only way in. The
# database is Cloud SQL PostgreSQL, reached through the Cloud SQL connector (no authorised networks).
# Nothing here changes DNS: the owner adds the records `dns` prints, the certificate's first, and
# switches open.alvary.ai over when `verify` passes. Every step can be run again.
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-prod}"  # staging later: GCP_PROJECT=open-alvary-staging
BILLING_FROM="${GCP_BILLING_FROM:-open-alvary-ingestion}"
REGION="${GCP_REGION:-europe-west1}"
DOMAIN="${PUBLIC_DOMAIN:-open.alvary.ai}"
REPOSITORY="open-alvary"
SERVICE="open-alvary-public"
MIGRATE_JOB="open-alvary-migrate"
RUNNER="public-api"
RUNNER_EMAIL="${RUNNER}@${PROJECT}.iam.gserviceaccount.com"
BUILDER="image-builder"
BUILDER_EMAIL="${BUILDER}@${PROJECT}.iam.gserviceaccount.com"
BUILD_SOURCES="${PROJECT}_cloudbuild"
SQL_INSTANCE="open-alvary-db"
SQL_TIER="${SQL_TIER:-db-g1-small}"
DATABASE="open_alvary"
DB_USER="open_alvary"
# The address chain: the load balancer appends "<client>,<load balancer>" to X-Forwarded-For, so the
# visitor is second from the right. `verify` confirms it before the domain is switched over.
TRUSTED_PROXIES="${TRUSTED_PROXIES:-2}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"
FEED_KEY_ID="${FEED_KEY_ID:-k1}"  # the key in the feed-keys secret that seals new feed tokens
WEBHOOK_KEY_ID="${WEBHOOK_KEY_ID:-w1}"  # the key in the webhook-keys secret that seals new webhook secrets
# Activation B (MAC-15): ACCOUNTS=1 on `deploy` switches on the console, keys, REST v1 and MCP, and
# closes the anonymous API, together. Run `accounts` (secrets) first; `schedule` once afterwards.
ACCOUNTS="${ACCOUNTS:-0}"
API_DOMAIN="${API_DOMAIN:-api.$DOMAIN}"
AUTH0_FILES="private-staging/auth0"  # written by deployment/auth0/configure.py; never committed
WORKER_JOB="open-alvary-worker"
DAILY_JOB="open-alvary-daily"
SCHEDULER="scheduler"
SCHEDULER_EMAIL="${SCHEDULER}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/public-service:$(git rev-parse --short HEAD)"
GC=(gcloud --project "$PROJECT" --quiet)
SETTINGS=(PUBLIC_REPOSITORY_URL PUBLIC_MAINTAINER PUBLIC_CONTACT_EMAIL METADATA_LICENCE)

setting() {  # a value from the environment or the git-ignored .env (never printed)
  local name="$1"
  if [[ -n "${!name:-}" ]]; then printf '%s' "${!name}"; return; fi
  grep -E "^${name}=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "'\""
}

grant() {  # an IAM binding; a service account just created takes a moment to be known, so retry
  local attempt
  for attempt in 1 2 3 4 5 6; do
    "$@" >/dev/null 2>&1 && return 0
    sleep 10
  done
  "$@" >/dev/null
}

connection() { printf '%s:%s:%s' "$PROJECT" "$REGION" "$SQL_INSTANCE"; }

plan() {
  local name missing=()
  for name in "${SETTINGS[@]}"; do [[ -n "$(setting "$name")" ]] || missing+=("$name"); done
  cat <<PLAN
Project $PROJECT (billing as $BILLING_FROM), region $REGION, domain $DOMAIN. Would create (if absent):
  - APIs: Cloud Run, Cloud SQL Admin, Compute, Certificate Manager, Secret Manager, Artifact Registry,
    Cloud Build
  - Artifact Registry repository $REPOSITORY; service accounts $RUNNER (Cloud SQL client, its own
    secrets only) and $BUILDER (image builds)
  - Cloud SQL PostgreSQL 16 instance $SQL_INSTANCE ($SQL_TIER, 10 GB SSD growing on demand, daily
    backups kept 7 days, point-in-time recovery, deletion protection; no authorised networks)
  - Secrets: database-password, database-url, admission-config and site-admission-config (fresh
    HMAC secrets; trusted_proxies $TRUSTED_PROXIES; D3 values from deployment/*admission.example.json)
  - Cloud Run job $MIGRATE_JOB (launch checks, migrations, seed) and service $SERVICE (1 to
    $MAX_INSTANCES instances, 1 vCPU, 1 GiB; ingress from the load balancer only; no default address)
  - Global external HTTPS load balancer: static IP, serverless endpoint group, backend with Cloud
    Armor policy open-alvary-edge (per-address throttle on website paths, D23), certificate for
    $DOMAIN by DNS authorisation, HTTPS on 443 and a redirect from 80

Monthly cost, estimated (europe-west1, list prices, low traffic):
  Cloud SQL $SQL_TIER with storage and backups   ~ \$28
  Cloud Run, one instance kept warm                ~ \$13
  Load balancer forwarding rules                   ~ \$18
  Cloud Armor policy, rules, requests              ~ \$8
  Secrets, registry, certificate, egress           ~ \$2
  Total                                            ~ \$69 a month

Owner actions: run 'project' and 'setup' (they create billed resources and IAM bindings); add the DNS
records 'dns' prints (certificate first; open.alvary.ai only when 'verify' passes).

Activation B adds (owner's go-ahead): secrets console-client-secret, consent-secret, key-pepper,
resend-api-key, resend-webhook-secret ('accounts'); Cloud Run jobs $WORKER_JOB and $DAILY_JOB
('ACCOUNTS=1 deploy'); service account $SCHEDULER and two Cloud Scheduler jobs, every 5 minutes and
daily ('schedule'); a certificate for $API_DOMAIN ('api_edge').
  Worker runs, ~8,600 a month of a few seconds   ~ \$1-3 (largely within Cloud Run's free tier)
  Cloud Scheduler, two jobs                        ~ \$0 (three jobs a month are free)
  Five more secrets                                ~ \$0.30
PLAN
  if ((${#missing[@]})); then
    echo "Settings missing (environment or .env): ${missing[*]}"
  else
    echo "Settings present: ${SETTINGS[*]}"
  fi
}

project() {
  if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
    gcloud projects create "$PROJECT" --name="Open Alvary ${PROJECT##*-}"
  fi
  local account
  account="$(gcloud billing projects describe "$BILLING_FROM" --format='value(billingAccountName)')"
  [[ -n "$account" ]] || { echo "No billing account found on $BILLING_FROM" >&2; exit 1; }
  gcloud billing projects link "$PROJECT" --billing-account="${account#billingAccounts/}" >/dev/null
  echo "Project $PROJECT ready, billed with $BILLING_FROM's account."
}

secret_put() {  # create or add a version, from standard input
  local name="$1"
  if "${GC[@]}" secrets describe "$name" >/dev/null 2>&1; then
    "${GC[@]}" secrets versions add "$name" --data-file=- >/dev/null
  else
    "${GC[@]}" secrets create "$name" --replication-policy=user-managed --locations="$REGION" --data-file=- >/dev/null
  fi
}

admission_json() {  # an admission class from its example, with a fresh secret and this deployment's pool
  python3 - "$1" "$TRUSTED_PROXIES" "$MAX_INSTANCES" <<'PY'
import json, secrets, sys
config = json.load(open(sys.argv[1]))
config.update(hmac_secret=secrets.token_hex(32), previous_hmac_secret=None, trusted_proxies=int(sys.argv[2]),
              api_replicas=int(sys.argv[3]), db_connections=50, operator_reserve=5, other_workers=5,
              public_pool_size=10 * int(sys.argv[3]))
print(json.dumps(config))
PY
}

setup() {
  "${GC[@]}" services enable run.googleapis.com sqladmin.googleapis.com compute.googleapis.com \
    certificatemanager.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
    cloudbuild.googleapis.com
  "${GC[@]}" artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1 \
    || "${GC[@]}" artifacts repositories create "$REPOSITORY" --repository-format=docker --location="$REGION"
  local account
  for account in "$RUNNER" "$BUILDER"; do
    "${GC[@]}" iam service-accounts describe "${account}@${PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1 \
      || "${GC[@]}" iam service-accounts create "$account"
  done
  grant "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER_EMAIL" \
    --role=roles/cloudsql.client --condition=None
  grant "${GC[@]}" artifacts repositories add-iam-policy-binding "$REPOSITORY" --location="$REGION" \
    --member="serviceAccount:$BUILDER_EMAIL" --role=roles/artifactregistry.writer
  grant "${GC[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$BUILDER_EMAIL" \
    --role=roles/logging.logWriter --condition=None
  gcloud storage buckets describe "gs://$BUILD_SOURCES" >/dev/null 2>&1 \
    || gcloud storage buckets create "gs://$BUILD_SOURCES" --project="$PROJECT" --location="$REGION" \
      --uniform-bucket-level-access
  grant gcloud storage buckets add-iam-policy-binding "gs://$BUILD_SOURCES" \
    --member="serviceAccount:$BUILDER_EMAIL" --role=roles/storage.objectViewer

  if ! "${GC[@]}" sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
    "${GC[@]}" sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE \
      --tier="$SQL_TIER" --region="$REGION" --storage-type=SSD --storage-size=10 --storage-auto-increase \
      --backup-start-time=02:00 --retained-backups-count=7 --enable-point-in-time-recovery \
      --deletion-protection
  fi
  "${GC[@]}" sql databases describe "$DATABASE" --instance="$SQL_INSTANCE" >/dev/null 2>&1 \
    || "${GC[@]}" sql databases create "$DATABASE" --instance="$SQL_INSTANCE"
  if ! "${GC[@]}" secrets describe database-password >/dev/null 2>&1; then
    # Alphanumeric, 40 characters: what scripts.check_launch requires. Never printed.
    local password
    password="$(python3 -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(40)))')"
    printf '%s' "$password" | secret_put database-password
    printf 'postgresql+psycopg://%s:%s@/%s?host=/cloudsql/%s' "$DB_USER" "$password" "$DATABASE" "$(connection)" \
      | secret_put database-url
    if "${GC[@]}" sql users list --instance="$SQL_INSTANCE" --format='value(name)' | grep -qx "$DB_USER"; then
      "${GC[@]}" sql users set-password "$DB_USER" --instance="$SQL_INSTANCE" --password="$password"
    else
      "${GC[@]}" sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$password"
    fi
  fi
  "${GC[@]}" secrets describe admission-config >/dev/null 2>&1 \
    || admission_json deployment/admission.example.json | secret_put admission-config
  "${GC[@]}" secrets describe site-admission-config >/dev/null 2>&1 \
    || admission_json deployment/site-admission.example.json | secret_put site-admission-config
  # The change feed's cursor key (changes-contracts.md): one shared random key, never printed.
  "${GC[@]}" secrets describe feed-keys >/dev/null 2>&1 \
    || python3 -c 'import base64,json,secrets; print(json.dumps({"k1": base64.b64encode(secrets.token_bytes(32)).decode()}))' \
      | secret_put feed-keys
  # Seals webhook signing secrets at rest (they must be recoverable to sign); never printed.
  "${GC[@]}" secrets describe webhook-keys >/dev/null 2>&1 \
    || python3 -c 'import base64,json,secrets; print(json.dumps({"w1": base64.b64encode(secrets.token_bytes(32)).decode()}))' \
      | secret_put webhook-keys
  local secret
  for secret in database-password database-url admission-config site-admission-config feed-keys webhook-keys; do
    grant "${GC[@]}" secrets add-iam-policy-binding "$secret" --member="serviceAccount:$RUNNER_EMAIL" \
      --role=roles/secretmanager.secretAccessor
  done
  echo "Setup done. Next: $0 deploy"
}

accounts() {  # activation B's secrets, from this machine's files and .env; never printed
  local file
  for file in console-client-id console-client-secret consent-secret; do
    [[ -s "$AUTH0_FILES/$file" ]] || { echo "Missing $AUTH0_FILES/$file: run deployment/auth0/configure.py" >&2; exit 1; }
  done
  [[ -n "$(setting RESEND_API_KEY)" ]] || { echo "Set RESEND_API_KEY in the environment or .env" >&2; exit 1; }
  secret_put console-client-secret < "$AUTH0_FILES/console-client-secret"
  # The Auth0 post-login Action was deployed with this same secret: they must match.
  secret_put consent-secret < "$AUTH0_FILES/consent-secret"
  # The API-key pepper is made once and never replaced: replacing it would invalidate every key.
  "${GC[@]}" secrets describe key-pepper >/dev/null 2>&1 \
    || python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode(), end="")' \
      | secret_put key-pepper
  printf '%s' "$(setting RESEND_API_KEY)" | secret_put resend-api-key
  local names=(console-client-secret consent-secret key-pepper resend-api-key)
  if [[ -n "$(setting RESEND_WEBHOOK_SECRET)" ]]; then
    printf '%s' "$(setting RESEND_WEBHOOK_SECRET)" | secret_put resend-webhook-secret
    names+=(resend-webhook-secret)
  fi
  local secret
  for secret in "${names[@]}"; do
    grant "${GC[@]}" secrets add-iam-policy-binding "$secret" --member="serviceAccount:$RUNNER_EMAIL" \
      --role=roles/secretmanager.secretAccessor
  done
  cat <<NEXT
Activation B secrets stored: ${names[*]}.
Bounces: in Resend, add a webhook to https://$DOMAIN/console/mail-events for email.bounced and
email.complained, put its signing secret in .env as RESEND_WEBHOOK_SECRET and run this again.
Next: ACCOUNTS=1 $0 deploy, then $0 api_edge (once) and $0 schedule (once).
NEXT
}

accounts_env() {  # the extra settings activation B adds to the service and the worker
  local domain
  domain="$(setting AUTH0_DOMAIN | sed 's#^https://##; s#/$##')"
  [[ -n "$domain" ]] || { echo "Set AUTH0_DOMAIN in the environment or .env" >&2; exit 1; }
  printf '##ALVARY_ACCOUNTS=1##ALVARY_CONSOLE_ISSUER=https://%s/##ALVARY_CONSOLE_CLIENT_ID=%s' \
    "$domain" "$(cat "$AUTH0_FILES/console-client-id")"
  printf '##ALVARY_CONSOLE_BASE_URL=https://%s##ALVARY_API_ORIGIN=https://%s' "$DOMAIN" "$API_DOMAIN"
}

accounts_secrets() {
  printf ',ALVARY_KEY_PEPPER=key-pepper:latest,ALVARY_CONSENT_SECRET=consent-secret:latest'
  printf ',ALVARY_CONSOLE_CLIENT_SECRET=console-client-secret:latest,RESEND_API_KEY=resend-api-key:latest'
  if "${GC[@]}" secrets describe resend-webhook-secret >/dev/null 2>&1; then
    printf ',RESEND_WEBHOOK_SECRET=resend-webhook-secret:latest'
  fi
}

app_env() {
  # gcloud's alternate-delimiter form (^##^…##…): values such as "Ruhu, Inc." keep their commas.
  local name env="^##^ALVARY_RUNTIME_MODE=persistent##PUBLIC_DOMAIN=$DOMAIN"
  for name in "${SETTINGS[@]}"; do
    local value
    value="$(setting "$name")"
    [[ -n "$value" ]] || { echo "Set $name in the environment or .env" >&2; exit 1; }
    [[ "$value" != *"##"* ]] || { echo "$name must not contain ##" >&2; exit 1; }
    env+="##$name=$value"
  done
  printf '%s' "$env"
}

deploy() {
  local env context
  local key keys=(feed-keys webhook-keys)
  [[ "$ACCOUNTS" == 1 ]] && keys+=(console-client-secret consent-secret key-pepper resend-api-key)
  for key in "${keys[@]}"; do
    "${GC[@]}" secrets describe "$key" >/dev/null 2>&1 \
      || { echo "The $key secret is missing: run $0 setup first" >&2; exit 1; }
  done
  env="$(app_env)"
  context="$(mktemp -d)"
  trap 'rm -rf "$context"' RETURN
  # Built from the commit itself (git archive): nothing untracked or private can enter the build.
  git archive HEAD | tar -x -C "$context"
  "${GC[@]}" builds submit "$context" --region="$REGION" \
    --service-account="projects/$PROJECT/serviceAccounts/$BUILDER_EMAIL" \
    --config="$context/deployment/gcp/public-service.cloudbuild.yaml" --substitutions="_IMAGE=$IMAGE"
  "${GC[@]}" run jobs deploy "$MIGRATE_JOB" --image="$IMAGE" --region="$REGION" \
    --service-account="$RUNNER_EMAIL" --set-cloudsql-instances="$(connection)" --tasks=1 --max-retries=0 \
    --set-env-vars="$env" --set-secrets="DATABASE_URL=database-url:latest,POSTGRES_PASSWORD=database-password:latest" \
    --command=sh --args=-c,"python -m scripts.check_launch && alembic upgrade head && python -m scripts.seed"
  "${GC[@]}" run jobs execute "$MIGRATE_JOB" --region="$REGION" --wait
  local keys_env="##ALVARY_FEED_KEY_ID=$FEED_KEY_ID##ALVARY_WEBHOOK_KEY_ID=$WEBHOOK_KEY_ID"
  local secrets="DATABASE_URL=database-url:latest,ALVARY_FEED_KEYS=feed-keys:latest,ALVARY_WEBHOOK_KEYS=webhook-keys:latest"
  if [[ "$ACCOUNTS" == 1 ]]; then
    keys_env+="$(accounts_env)"
    secrets+="$(accounts_secrets)"
  fi
  "${GC[@]}" run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
    --add-cloudsql-instances="$(connection)" --ingress=internal-and-cloud-load-balancing --no-default-url \
    --no-invoker-iam-check --min-instances=1 --max-instances="$MAX_INSTANCES" --cpu=1 --memory=1Gi \
    --concurrency=80 \
    --set-env-vars="$env$keys_env##ALVARY_ADMISSION_CONFIG=/secrets/admission/config.json##ALVARY_SITE_ADMISSION_CONFIG=/secrets/site-admission/config.json" \
    --set-secrets="$secrets,/secrets/admission/config.json=admission-config:latest,/secrets/site-admission/config.json=site-admission-config:latest"
  if [[ "$ACCOUNTS" == 1 ]]; then
    # The worker (monitors, digests, webhooks) and the daily sweep and purge run the same image.
    "${GC[@]}" run jobs deploy "$WORKER_JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
      --set-cloudsql-instances="$(connection)" --tasks=1 --max-retries=0 --task-timeout=280s --memory=1Gi \
      --set-env-vars="$env$keys_env" --set-secrets="$secrets" --command=python --args=-m,changes.worker,run
    "${GC[@]}" run jobs deploy "$DAILY_JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUNNER_EMAIL" \
      --set-cloudsql-instances="$(connection)" --tasks=1 --max-retries=0 --task-timeout=600s \
      --set-env-vars="$env$keys_env" --set-secrets="$secrets" \
      --command=sh --args=-c,"python -m changes.admin sweep && python -m changes.admin purge"
  fi
  echo "Deployed $IMAGE. Next: $0 edge (once), then $0 verify"
}

edge() {
  local C=("${GC[@]}" compute)
  "${C[@]}" addresses describe open-alvary-ip --global >/dev/null 2>&1 \
    || "${C[@]}" addresses create open-alvary-ip --global --ip-version=IPV4
  "${C[@]}" network-endpoint-groups describe open-alvary-neg --region="$REGION" >/dev/null 2>&1 \
    || "${C[@]}" network-endpoint-groups create open-alvary-neg --region="$REGION" \
      --network-endpoint-type=serverless --cloud-run-service="$SERVICE"
  "${C[@]}" security-policies describe open-alvary-edge >/dev/null 2>&1 \
    || "${C[@]}" security-policies create open-alvary-edge --description="Open Alvary edge rules (D23)"
  # Website paths only, never api.<domain>: a per-address throttle above any reader's pace (D23 to
  # measure); the origin's own site class does the fine-grained admission. (Cloud Armor's regular
  # expressions allow no groups, so the paths are two prefixes.)
  "${C[@]}" security-policies rules describe 1000 --security-policy=open-alvary-edge >/dev/null 2>&1 \
    || "${C[@]}" security-policies rules create 1000 --security-policy=open-alvary-edge \
      --expression="request.path.startsWith('/site/') || request.path.startsWith('/downloads/')" \
      --action=throttle --rate-limit-threshold-count=600 --rate-limit-threshold-interval-sec=60 \
      --conform-action=allow --exceed-action=deny-429 --enforce-on-key=IP
  "${C[@]}" backend-services describe open-alvary-backend --global >/dev/null 2>&1 \
    || "${C[@]}" backend-services create open-alvary-backend --global --load-balancing-scheme=EXTERNAL_MANAGED
  # Each step checks for itself, so a run interrupted halfway completes on the next.
  "${C[@]}" backend-services describe open-alvary-backend --global --format='value(backends[].group)' \
    | grep -q open-alvary-neg \
    || "${C[@]}" backend-services add-backend open-alvary-backend --global \
      --network-endpoint-group=open-alvary-neg --network-endpoint-group-region="$REGION"
  # Right after a change the backend can answer "not ready" for a few seconds: grant() retries.
  grant "${C[@]}" backend-services update open-alvary-backend --global --security-policy=open-alvary-edge
  "${C[@]}" url-maps describe open-alvary-map >/dev/null 2>&1 \
    || "${C[@]}" url-maps create open-alvary-map --default-service=open-alvary-backend
  local M=("${GC[@]}" certificate-manager)
  "${M[@]}" dns-authorizations describe open-alvary-dns-auth >/dev/null 2>&1 \
    || "${M[@]}" dns-authorizations create open-alvary-dns-auth --domain="$DOMAIN"
  "${M[@]}" certificates describe open-alvary-cert >/dev/null 2>&1 \
    || "${M[@]}" certificates create open-alvary-cert --domains="$DOMAIN" --dns-authorizations=open-alvary-dns-auth
  "${M[@]}" maps describe open-alvary-certs >/dev/null 2>&1 || "${M[@]}" maps create open-alvary-certs
  "${M[@]}" maps entries describe open-alvary-entry --map=open-alvary-certs >/dev/null 2>&1 \
    || "${M[@]}" maps entries create open-alvary-entry --map=open-alvary-certs --certificates=open-alvary-cert \
      --hostname="$DOMAIN"
  "${C[@]}" target-https-proxies describe open-alvary-https --global >/dev/null 2>&1 \
    || "${C[@]}" target-https-proxies create open-alvary-https --global --url-map=open-alvary-map \
      --certificate-map=open-alvary-certs
  "${C[@]}" forwarding-rules describe open-alvary-https-rule --global >/dev/null 2>&1 \
    || "${C[@]}" forwarding-rules create open-alvary-https-rule --global --load-balancing-scheme=EXTERNAL_MANAGED \
      --address=open-alvary-ip --target-https-proxy=open-alvary-https --ports=443
  if ! "${C[@]}" url-maps describe open-alvary-redirect >/dev/null 2>&1; then
    printf 'name: open-alvary-redirect\ndefaultUrlRedirect:\n  httpsRedirect: true\n  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT\n' \
      | "${C[@]}" url-maps import open-alvary-redirect --global  # reads standard input
  fi
  "${C[@]}" target-http-proxies describe open-alvary-http --global >/dev/null 2>&1 \
    || "${C[@]}" target-http-proxies create open-alvary-http --global --url-map=open-alvary-redirect
  "${C[@]}" forwarding-rules describe open-alvary-http-rule --global >/dev/null 2>&1 \
    || "${C[@]}" forwarding-rules create open-alvary-http-rule --global --load-balancing-scheme=EXTERNAL_MANAGED \
      --address=open-alvary-ip --target-http-proxy=open-alvary-http --ports=80
  dns
}

api_edge() {  # api.<domain> on the same load balancer, with its own certificate (MAC-02, D7)
  local M=("${GC[@]}" certificate-manager)
  "${M[@]}" dns-authorizations describe open-alvary-api-dns-auth >/dev/null 2>&1 \
    || "${M[@]}" dns-authorizations create open-alvary-api-dns-auth --domain="$API_DOMAIN"
  "${M[@]}" certificates describe open-alvary-api-cert >/dev/null 2>&1 \
    || "${M[@]}" certificates create open-alvary-api-cert --domains="$API_DOMAIN" \
      --dns-authorizations=open-alvary-api-dns-auth
  "${M[@]}" maps entries describe open-alvary-api-entry --map=open-alvary-certs >/dev/null 2>&1 \
    || "${M[@]}" maps entries create open-alvary-api-entry --map=open-alvary-certs \
      --certificates=open-alvary-api-cert --hostname="$API_DOMAIN"
  local record
  record="$("${M[@]}" dns-authorizations describe open-alvary-api-dns-auth \
    --format='value(dnsResourceRecord.name,dnsResourceRecord.type,dnsResourceRecord.data)')"
  cat <<DNS
DNS records for $API_DOMAIN (alvary.ai's zone):
  1. For the certificate:  $record
  2. Then:                 $API_DOMAIN.  A  $(address)
DNS
}

schedule() {  # once, after the first ACCOUNTS=1 deploy: the worker every 5 minutes, the sweep daily
  "${GC[@]}" services enable cloudscheduler.googleapis.com
  "${GC[@]}" iam service-accounts describe "$SCHEDULER_EMAIL" >/dev/null 2>&1 \
    || "${GC[@]}" iam service-accounts create "$SCHEDULER" --display-name="Runs the Open Alvary jobs"
  local job
  for job in "$WORKER_JOB" "$DAILY_JOB"; do
    grant "${GC[@]}" run jobs add-iam-policy-binding "$job" --region="$REGION" \
      --member="serviceAccount:$SCHEDULER_EMAIL" --role=roles/run.invoker
  done
  local base="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs"
  scheduled() {  # name, cron, job
    local verb=create
    "${GC[@]}" scheduler jobs describe "$1" --location="$REGION" >/dev/null 2>&1 && verb=update
    "${GC[@]}" scheduler jobs "$verb" http "$1" --location="$REGION" --schedule="$2" --time-zone=UTC \
      --uri="$base/$3:run" --http-method=POST --oauth-service-account-email="$SCHEDULER_EMAIL"
  }
  scheduled open-alvary-worker-every-5-minutes "*/5 * * * *" "$WORKER_JOB"
  scheduled open-alvary-daily-sweep "15 3 * * *" "$DAILY_JOB"
  echo "Scheduled: $WORKER_JOB every 5 minutes, $DAILY_JOB daily at 03:15 UTC."
}

address() { "${GC[@]}" compute addresses describe open-alvary-ip --global --format='value(address)'; }

dns() {
  local record
  record="$("${GC[@]}" certificate-manager dns-authorizations describe open-alvary-dns-auth \
    --format='value(dnsResourceRecord.name,dnsResourceRecord.type,dnsResourceRecord.data)')"
  cat <<DNS
DNS records for the owner (alvary.ai's zone):
  1. Now, for the certificate (changes nothing for visitors):
       $record
  2. At cutover, when 'verify' passes: point $DOMAIN at the load balancer instead of Vercel:
       $DOMAIN.  A  $(address)
DNS
}

verify() {
  local ip base status
  ip="$(address)"
  base="https://$DOMAIN"
  # Each check reports its own result; one failure never hides the others.
  curl_lb() { curl -sS --max-time 20 --resolve "$DOMAIN:443:$ip" "$@" 2>/dev/null || printf 'failed'; }
  echo "Through the load balancer at $ip (before or after DNS):"
  for path in / /site/v1/project /site/v1/coverage /site/v1/sources "/site/v1/search?q=act" /site/v1/releases; do
    status="$(curl_lb -o /dev/null -w '%{http_code} %header{cache-control}' "$base$path")"
    echo "  $path -> $status"
  done
  echo "  credential refused -> $(curl_lb -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer x' "$base/site/v1/sources")"
  echo "  page 26 refused -> $(curl_lb -o /dev/null -w '%{http_code}' "$base/site/v1/sources?page=26")"
  echo "  old download link -> $(curl_lb -o /dev/null -w '%{http_code} %header{location}' "$base/api/releases/x/ng/metadata/manifest.json")"
  echo "  no CORS -> '$(curl_lb -o /dev/null -w '%header{access-control-allow-origin}' -H 'Origin: https://elsewhere.example' "$base/site/v1/sources")'"
  # Forged X-Forwarded-For values must all land in the caller's own bucket: 40 requests, each with a
  # different forged address, must drain one allowance (the bucket refills too fast to compare two).
  local lowest
  lowest="$(seq 1 40 | xargs -P 20 -I{} curl -s -o /dev/null --max-time 20 --resolve "$DOMAIN:443:$ip" \
    -H "X-Forwarded-For: 198.51.100.{}" -w '%header{ratelimit}\n' "$base/site/v1/project" \
    | sed -E 's/.*r=([0-9]+).*/\1/' | sort -n | head -1)"
  echo "  40 forged addresses -> lowest remaining $lowest (must fall well below the capacity: one shared bucket)"
  echo "  (separate real visitors keep separate buckets: probe from another network, as on 2026-10-15)"
  local repository
  repository="$(setting PUBLIC_REPOSITORY_URL)"
  echo "  repository link $repository -> $(curl -s -o /dev/null -w '%{http_code}' "$repository") (must be 200)"
  echo "  the service's own address: $("${GC[@]}" run services describe "$SERVICE" --region="$REGION" --format='value(status.url)' || true) (empty: no default address)"
  echo "  change feed -> $(curl_lb -o /dev/null -w '%{http_code}' "$base/api/changes") (200, before and after activation B)"
  if [[ "$ACCOUNTS" == 1 ]]; then
    echo "Activation B:"
    echo "  console -> $(curl_lb -o /dev/null -w '%{http_code}' "$base/console/") (200)"
    echo "  anonymous API closed -> $(curl_lb -o /dev/null -w '%{http_code}' "$base/api/sources") (410)"
    echo "  REST v1 without a key -> $(curl_lb -o /dev/null -w '%{http_code} %header{www-authenticate}' "$base/api/v1/sources") (401 with a Bearer challenge)"
    echo "  MCP metadata -> $(curl_lb -o /dev/null -w '%{http_code}' "$base/.well-known/oauth-protected-resource/mcp") (200)"
    echo "  $API_DOMAIN -> $(curl -sS -o /dev/null --max-time 20 -w '%{http_code}' "https://$API_DOMAIN/api/v1/sources" 2>/dev/null || printf 'not yet') (401 once its DNS and certificate are live)"
  fi
}

status() {
  "${GC[@]}" run services describe "$SERVICE" --region="$REGION" --format='value(status.latestReadyRevisionName)' || true
  "${GC[@]}" sql instances describe "$SQL_INSTANCE" --format='value(state,settings.tier)' || true
  "${GC[@]}" certificate-manager certificates describe open-alvary-cert --format='value(managed.state)' || true
  echo "Load balancer address: $(address 2>/dev/null || echo none)"
}

"${1:?plan, project, setup, deploy, edge, dns, verify, status; activation B: accounts, api_edge, schedule}" "${@:2}"
