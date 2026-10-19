#!/usr/bin/env bash
# The public service from a second region, in Africa (RSV-11, owner decision D30), step by step:
#
#   deployment/gcp/regions.sh plan      # print what would be created and what it costs; creates nothing
#   deployment/gcp/regions.sh setup     # the service in africa-south1 and its endpoint group, in the backend
#   deployment/gcp/regions.sh replica   # optional: a read replica of the database in africa-south1
#   deployment/gcp/regions.sh status
#
# Run by the owner after D30, once deployment/gcp/public_service.sh has deployed the service and its
# edge (deploy, edge). The Johannesburg service runs the same image with the same settings and secrets
# as the one in Belgium, read from the running service itself, and sits beside it in the global
# external HTTPS load balancer's one backend: two serverless endpoint groups, so each request goes to
# the nearest region whose service is healthy. Cloud Armor, the certificates and the address are the
# backend's and stay as they are. Serverless endpoint groups take no health checks, so outlier
# detection takes a region out of the rotation while it answers errors, and puts it back after.
#
# The database: writes go to the primary in Europe only, and so, today, do reads. The application
# has one database connection and writes on every API request (admission, usage, data a day), so the
# Johannesburg service connects to the primary in europe-west1 through the Cloud SQL connector
# (encrypted and IAM-authorised, no authorised networks): the documented cross-region connection.
# Each of a request's database round trips then crosses to Belgium and back (about 150-180 ms), so a
# request served in Johannesburg is slower than one served in Belgium, which the load balancer already
# reaches over Google's network. Until the application reads through a second, read-only connection to
# a replica in Africa (`replica`, not yet used by the service), this region adds availability, not
# speed.
#
# What stays in Europe: the primary database with every write, its backups and point-in-time
# recovery; the secrets (replicated in europe-west1 only); the image registry; the worker and daily
# jobs and their schedules; Cloud Build; the audit seal log bucket. The load balancer is global.
# No data-residency claim follows from serving in Africa (OPS-02): records, accounts and usage are
# stored where they are today, and a request answered in Johannesburg still reads and writes them in
# Belgium. Nothing here changes DNS. Every step can be run again.
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-prod}"
PRIMARY="${GCP_REGION:-europe-west1}"
SECOND="${SECOND_REGION:-africa-south1}"
SERVICE="open-alvary-public"  # the same name in both regions: one service in the metrics and SLOs
RUNNER_EMAIL="public-api@${PROJECT}.iam.gserviceaccount.com"
BACKEND="open-alvary-backend"
PRIMARY_NEG="open-alvary-neg"
SECOND_NEG="open-alvary-neg-${SECOND}"
SQL_INSTANCE="open-alvary-db"
REPLICA="${SQL_REPLICA:-open-alvary-db-${SECOND}}"
REPLICA_TIER="${REPLICA_TIER:-db-g1-small}"
# Each instance holds up to 10 database connections (admission-config: public_pool_size over
# api_replicas). The primary allows 50, less 10 kept for operators and jobs: 4 instances across both
# regions, Europe's 3 and one here. Grow both only with a larger database tier and admission config.
SECOND_MIN_INSTANCES="${SECOND_MIN_INSTANCES:-1}"
SECOND_MAX_INSTANCES="${SECOND_MAX_INSTANCES:-1}"
GC=(gcloud --project "$PROJECT" --quiet)
C=("${GC[@]}" compute)

plan() {
  cat <<PLAN
Project $PROJECT: the public service in $PRIMARY (primary) and $SECOND (second).
Would create (if absent):
  - Cloud Run service $SERVICE in $SECOND: the image, settings and secrets the $PRIMARY
    service runs now; $SECOND_MIN_INSTANCES to $SECOND_MAX_INSTANCES instances, 1 vCPU, 1 GiB;
    ingress from the load balancer only; no default address; runs as $RUNNER_EMAIL
  - Its database connection: the primary $SQL_INSTANCE in $PRIMARY, through the Cloud SQL
    connector (cross-region). Writes go to the primary only; so do reads, until the application can
    read from a replica
  - Serverless endpoint group $SECOND_NEG, added to the backend $BACKEND
    beside $PRIMARY_NEG: the load balancer sends each request to the nearest healthy region
  - Outlier detection on $BACKEND: a region answering 5 errors in a row leaves the rotation
    for 30 seconds or more (at most one of the two at a time)
  - Optional, 'replica' only: Cloud SQL read replica $REPLICA ($REPLICA_TIER),
    unused by the service until the application reads through a second, read-only connection

Stays in Europe: the primary database (every write, and every read today), backups, secrets, the
image registry, the worker and daily jobs, Cloud Build, the audit seal bucket. No data-residency
claim follows (OPS-02). A request served in $SECOND makes each database round trip to $PRIMARY
(about 150-180 ms), so it is slower than one served in $PRIMARY: this region adds availability first.

Monthly cost, estimated (list prices, low traffic; check the pricing pages before running):
  Cloud Run in Africa, one instance kept warm       ~ \$18
  Database traffic between Africa and Europe        ~ \$1-5
  Serverless endpoint group, outlier detection      ~ \$0 (the load balancer is already paid for)
  Total without the replica                         ~ \$19-23 a month
  Read replica with storage ('replica' only)        ~ \$35-40 more, plus replication traffic

Owner actions: decide D30, then run 'setup' (billed resources); 'replica' only when wanted. Before
raising SECOND_MAX_INSTANCES, raise the database tier and the admission-config secret's
db_connections, public_pool_size and api_replicas to cover both regions. To take $SECOND out again:
gcloud --project $PROJECT compute backend-services remove-backend $BACKEND --global \\
  --network-endpoint-group=$SECOND_NEG --network-endpoint-group-region=$SECOND
PLAN
}

primary_settings() {  # the running primary as four lines: image, env (gcloud's ^##^ form), secrets, Cloud SQL
  local file
  file="$(mktemp)"
  "${GC[@]}" run services describe "$SERVICE" --region="$PRIMARY" --format=json >"$file"
  python3 - "$file" <<'PY'
import json, sys
template = json.load(open(sys.argv[1]))["spec"]["template"]
container = template["spec"]["containers"][0]
env, secrets = [], []
for item in container.get("env", []):
    ref = item.get("valueFrom", {}).get("secretKeyRef")
    if ref:
        secrets.append(f"{item['name']}={ref['name']}:{ref['key']}")
    elif "##" in item.get("value", ""):
        sys.exit(f"{item['name']} contains ##")
    else:
        env.append(f"{item['name']}={item.get('value', '')}")
volumes = {v["name"]: v.get("secret") for v in template["spec"].get("volumes", [])}
for mount in container.get("volumeMounts", []):
    secret = volumes.get(mount["name"])
    for entry in (secret or {}).get("items", []):
        secrets.append(f"{mount['mountPath']}/{entry['path']}={secret['secretName']}:{entry['key']}")
print(container["image"])
print("^##^" + "##".join(env))
print(",".join(secrets))
print(template["metadata"].get("annotations", {}).get("run.googleapis.com/cloudsql-instances", ""))
PY
  rm -f "$file"
}

outlier_detection() {  # serverless endpoint groups take no health checks; this is how a failing region leaves
  local file
  file="$(mktemp)"
  "${C[@]}" backend-services export "$BACKEND" --global --destination="$file"
  if grep -q '^outlierDetection:' "$file"; then
    echo "Outlier detection already set on $BACKEND."
  else
    cat >>"$file" <<'YAML'
outlierDetection:
  consecutiveErrors: 5
  enforcingConsecutiveErrors: 100
  consecutiveGatewayFailure: 3
  enforcingConsecutiveGatewayFailure: 100
  interval:
    seconds: 10
  baseEjectionTime:
    seconds: 30
  maxEjectionPercent: 50
YAML
    "${C[@]}" backend-services import "$BACKEND" --global --source="$file"
  fi
  rm -f "$file"
}

setup() {
  "${C[@]}" backend-services describe "$BACKEND" --global >/dev/null 2>&1 \
    || { echo "No $BACKEND: run deployment/gcp/public_service.sh deploy and edge first." >&2; exit 1; }
  local image env secrets sql
  {
    read -r image
    read -r env
    read -r secrets
    read -r sql
  } < <(primary_settings)
  [[ -n "$image" && -n "$sql" ]] || { echo "Could not read the $PRIMARY service's settings." >&2; exit 1; }
  # The same database connection as the primary (its DATABASE_URL names this instance): writes, and
  # today reads, go to Europe. Never a replica here: the service writes on every request.
  "${GC[@]}" run deploy "$SERVICE" --image="$image" --region="$SECOND" --service-account="$RUNNER_EMAIL" \
    --add-cloudsql-instances="$sql" --ingress=internal-and-cloud-load-balancing --no-default-url \
    --no-invoker-iam-check --min-instances="$SECOND_MIN_INSTANCES" --max-instances="$SECOND_MAX_INSTANCES" \
    --cpu=1 --memory=1Gi --concurrency=80 --set-env-vars="$env" --set-secrets="$secrets"
  "${C[@]}" network-endpoint-groups describe "$SECOND_NEG" --region="$SECOND" >/dev/null 2>&1 \
    || "${C[@]}" network-endpoint-groups create "$SECOND_NEG" --region="$SECOND" \
      --network-endpoint-type=serverless --cloud-run-service="$SERVICE"
  "${C[@]}" backend-services describe "$BACKEND" --global --format='value(backends[].group)' \
    | grep -q "$SECOND_NEG" \
    || "${C[@]}" backend-services add-backend "$BACKEND" --global \
      --network-endpoint-group="$SECOND_NEG" --network-endpoint-group-region="$SECOND"
  outlier_detection
  echo "Serving from $PRIMARY and $SECOND, nearest healthy first. A new image reaches $SECOND only when"
  echo "this runs again after deployment/gcp/public_service.sh deploy."
}

replica() {  # optional and billed: a read replica in Africa, for when the application can read from it
  if "${GC[@]}" sql instances describe "$REPLICA" >/dev/null 2>&1; then
    echo "Replica $REPLICA already there."
  else
    "${GC[@]}" sql instances create "$REPLICA" --master-instance-name="$SQL_INSTANCE" --region="$SECOND" \
      --tier="$REPLICA_TIER" --storage-auto-increase
  fi
  echo "The service does not read from $REPLICA yet: that needs a second, read-only database connection"
  echo "in the application. Promoting it would move the primary to $SECOND, which D7 decides."
}

status() {
  local region
  for region in "$PRIMARY" "$SECOND"; do
    echo "$region: $("${GC[@]}" run services describe "$SERVICE" --region="$region" \
      --format='value(status.latestReadyRevisionName,spec.template.spec.containers[0].image)' 2>/dev/null \
      || echo 'no service')"
  done
  echo "Backends of $BACKEND: $("${C[@]}" backend-services describe "$BACKEND" --global \
    --format='value(backends[].group.basename())' 2>/dev/null || echo none)"
  echo "Outlier detection: $("${C[@]}" backend-services describe "$BACKEND" --global \
    --format='value(outlierDetection)' 2>/dev/null || echo none)"
  echo "Replica: $("${GC[@]}" sql instances describe "$REPLICA" --format='value(state,region,settings.tier)' \
    2>/dev/null || echo none)"
}

# A gcloud whose sign-in has lapsed fails every call, which the checks above would read as a missing
# resource: say so first. ("plan" only prints.)
signed_in() {
  gcloud auth print-access-token >/dev/null 2>&1 \
    || { echo "gcloud isn't signed in, or its sign-in expired: run gcloud auth login, then try again." >&2; exit 1; }
}

[[ "${1:-plan}" == plan ]] || signed_in
case "${1:-plan}" in
  plan) plan ;;
  setup) setup ;;
  replica) replica ;;
  status) status ;;
  *) echo "Usage: $0 plan|setup|replica|status" >&2; exit 2 ;;
esac
