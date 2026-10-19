#!/usr/bin/env bash
# Service level objectives for the public API on Cloud Monitoring (RSV-11, owner decision D30):
#
#   deployment/gcp/slo.sh plan      # print the objectives, the alerts and what they measure; creates nothing
#   deployment/gcp/slo.sh setup     # the monitored service, its objectives and their burn-rate alerts
#   deployment/gcp/slo.sh status    # each objective's goal and the error budget left
#
# Run by the owner after D30, once deployment/gcp/public_service.sh alerts has made the operator's
# email channel, which these alerts notify too. Objectives, not an agreement: no SLA is offered until
# SUS-06's conditions are met.
#
# What is measured. Cloud Run's own request metrics for the service open-alvary-public, in every
# region it runs in (deployment/gcp/regions.sh keeps the same name): availability is the share of
# requests not answered with a 5xx (503 temporarily_unavailable counts against it: refusing to let a
# request through unchecked is still unavailability; 429 does not); latency is the share answered
# within an operation's budget, as Cloud Run measures it, cold starts included (RSV-11 reports those
# separately). What never reaches the service, a load balancer or DNS failure, is the external
# uptime check's to catch (public_service.sh alerts).
#
# Per-route latency. Cloud Run's metrics carry no route: one service answers the website, the
# console, REST, MCP and downloads, so until the routes are told apart every latency objective
# measures all of its requests. Telling them apart needs log-based metrics, and not from Cloud Run's
# request logs: those for /api/v1 and /mcp are dropped before storage (public_service.sh
# request_logs, OA-A2), and their addresses carry queries. The application would write one
# structured line per request with the operation (resolve, retrieve and its mode: lexical, hybrid or
# rerank, check) and its server time, never the query; a log-based distribution metric over it, with
# an `operation` label, then narrows each objective: LATENCY_METRIC=logging.googleapis.com/user/<name>
# deployment/gcp/slo.sh setup. Until then only the loosest budget (2.5 s, which every operation
# must meet) alerts; the others are kept for their error budgets, without paging anyone.
#
# Burn-rate alerts follow the multiwindow rule: fast (2% of a 30-day budget in an hour: 14.4 times
# the allowed rate over the last hour and the last 5 minutes) and slow (5% in six hours: 6 times
# over the last 6 hours and the last 30 minutes). Every step can be run again.
set -euo pipefail

PROJECT="${GCP_PROJECT:-open-alvary-prod}"
SERVICE="open-alvary-public"
MONITORED="open-alvary-public-api"  # the Cloud Monitoring service the objectives belong to
CHANNEL_NAME="Open Alvary operator"  # the email channel public_service.sh alerts made
AVAILABILITY_GOAL="${AVAILABILITY_GOAL:-0.995}"  # proposed for launch (D30); 0.999 with an on-call rota
LATENCY_GOAL="${LATENCY_GOAL:-0.95}"  # p95: 95% of requests within the budget
WINDOW="2592000s"  # 30 days, rolling
LATENCY_METRIC="${LATENCY_METRIC:-}"  # a log-based distribution (ms) with an operation label, once it exists
# id|budget in ms|operation (RSV-11's p95 budgets, server-side and warm)
OPERATIONS=(
  "resolve|300|Resolve, or retrieve on the citation route"
  "retrieve-lexical|800|Retrieve, lexical only"
  "retrieve-hybrid|1500|Retrieve, hybrid"
  "retrieve-rerank|2500|Retrieve, hybrid with rerank and expansion"
  "check|300|Check, 100 items"
)
LOOSEST=2500
API="https://monitoring.googleapis.com/v3/projects/$PROJECT"
GC=(gcloud --project "$PROJECT" --quiet)
REQUESTS="resource.type=\\\"cloud_run_revision\\\" AND resource.label.service_name=\\\"$SERVICE\\\""

plan() {
  local operation id budget words latency_alerts="every operation"
  [[ -n "$LATENCY_METRIC" ]] || latency_alerts="the $LOOSEST ms budget only, until per-operation latency is recorded"
  cat <<PLAN
Project $PROJECT, Cloud Run service $SERVICE in every region. Would create (if absent):
  - Cloud Monitoring service $MONITORED, with these objectives over 30 days, rolling:
      availability               $AVAILABILITY_GOAL of requests not answered with a 5xx
PLAN
  for operation in "${OPERATIONS[@]}"; do
    IFS='|' read -r id budget words <<<"$operation"
    printf '      %-26s %s of requests within %s ms: %s\n' "latency-$id" "$LATENCY_GOAL" "$budget" "$words"
  done
  cat <<PLAN
    Availability from run.googleapis.com/request_count; latency from
    ${LATENCY_METRIC:-run.googleapis.com/request_latencies}${LATENCY_METRIC:+, per operation}.
  - Alert policies notifying the "$CHANNEL_NAME" email channel: availability burning fast (14.4x
    over 1 hour and 5 minutes) and slow (6x over 6 hours and 30 minutes); latency burning slowly,
    for $latency_alerts

Until per-operation latency is recorded (LATENCY_METRIC), every latency objective measures all of
the service's requests: the website, the console, REST, MCP and downloads together. Cloud Run's
request logs cannot supply it: those for /api/v1 and /mcp are not stored (OA-A2) and carry queries.

Monthly cost: ~ \$0 (objectives, alert policies and Cloud Run's metrics are free; a log-based metric
later costs only beyond the free logging allowance).

Owner actions: decide D30 (the availability goal, the error budget, who answers); run
'public_service.sh alerts' first if the "$CHANNEL_NAME" channel does not exist; then 'setup'.
PLAN
}

headers() {  # curl's arguments for Cloud Monitoring's API, as the signed-in owner
  printf '%s\n' -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "x-goog-user-project: $PROJECT" \
    -H "Content-Type: application/json"
}

api() {  # METHOD PATH [BODY_FILE]: Cloud Monitoring's API (gcloud has no commands for services or SLOs)
  local method="$1" path="$2" auth=() line
  while IFS= read -r line; do auth+=("$line"); done < <(headers)
  if [[ -n "${3:-}" ]]; then
    curl -sS --fail-with-body -X "$method" "${auth[@]}" --data-binary "@$3" "$API/$path"
  else
    curl -sS --fail-with-body -X "$method" "${auth[@]}" "$API/$path"
  fi
}

exists() {  # PATH: whether that resource is there (200), never creating it
  local auth=() line
  while IFS= read -r line; do auth+=("$line"); done < <(headers)
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' "${auth[@]}" "$API/$1")" == 200 ]]
}

encode() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

objective() {  # ID FILE: create the objective, or bring an existing one in line; prints its full name
  local id="$1" file="$2" path="services/$MONITORED/serviceLevelObjectives" answer
  if exists "$path/$id"; then
    answer="$(api PATCH "$path/$id?updateMask=displayName,goal,rollingPeriod,serviceLevelIndicator" "$file")" \
      || { echo "$answer" >&2; return 1; }
  else
    answer="$(api POST "$path?serviceLevelObjectiveId=$id" "$file")" || { echo "$answer" >&2; return 1; }
  fi
  python3 -c 'import json, sys; print(json.loads(sys.argv[1])["name"])' "$answer"
}

burn_alert() {  # TITLE SLO_NAME RATE LONG SHORT CHANNEL WHAT
  local title="$1" slo="$2" rate="$3" long="$4" short="$5" channel="$6" what="$7" file
  if "${GC[@]}" alpha monitoring policies list --filter="displayName=\"$title\"" --format='value(name)' \
    | grep -q .; then
    echo "Alert policy already there: $title"
    return
  fi
  file="$(mktemp)"
  cat >"$file" <<POLICY
{
  "displayName": "$title",
  "combiner": "AND",
  "conditions": [
    {
      "displayName": "Error budget burning at over ${rate}x for $long",
      "conditionThreshold": {
        "filter": "select_slo_burn_rate(\"$slo\", \"$long\")",
        "comparison": "COMPARISON_GT",
        "thresholdValue": $rate,
        "duration": "0s",
        "trigger": {"count": 1}
      }
    },
    {
      "displayName": "...and still over ${rate}x for the last $short",
      "conditionThreshold": {
        "filter": "select_slo_burn_rate(\"$slo\", \"$short\")",
        "comparison": "COMPARISON_GT",
        "thresholdValue": $rate,
        "duration": "0s",
        "trigger": {"count": 1}
      }
    }
  ],
  "notificationChannels": ["$channel"],
  "alertStrategy": {"autoClose": "86400s"},
  "documentation": {
    "mimeType": "text/markdown",
    "content": "$what is using its 30-day error budget ${rate} times faster than allowed. Check the service's revisions and logs in each region (deployment/gcp/public_service.sh status, deployment/gcp/regions.sh status), then the database. Objectives in deployment/gcp/slo.sh."
  }
}
POLICY
  "${GC[@]}" alpha monitoring policies create --policy-from-file="$file" --format='value(name)'
  rm -f "$file"
}

setup() {
  local channel
  channel="$("${GC[@]}" beta monitoring channels list --filter="displayName=\"$CHANNEL_NAME\"" \
    --format='value(name)' | head -1)"
  [[ -n "$channel" ]] \
    || { echo "No \"$CHANNEL_NAME\" channel: run deployment/gcp/public_service.sh alerts first." >&2; exit 1; }
  "${GC[@]}" services enable monitoring.googleapis.com
  local file
  file="$(mktemp)"
  if ! exists "services/$MONITORED"; then
    printf '{"displayName": "Open Alvary public API", "custom": {}}' >"$file"
    api POST "services?serviceId=$MONITORED" "$file" >/dev/null
  fi
  cat >"$file" <<SLO
{
  "displayName": "Availability: $AVAILABILITY_GOAL of requests without a server error, 30 days",
  "goal": $AVAILABILITY_GOAL,
  "rollingPeriod": "$WINDOW",
  "serviceLevelIndicator": {"requestBased": {"goodTotalRatio": {
    "totalServiceFilter": "metric.type=\"run.googleapis.com/request_count\" AND $REQUESTS",
    "badServiceFilter": "metric.type=\"run.googleapis.com/request_count\" AND $REQUESTS AND metric.label.response_code_class=\"5xx\""
  }}}
}
SLO
  local availability
  availability="$(objective availability "$file")"
  burn_alert "Open Alvary SLO: availability burning fast" "$availability" 14.4 60m 5m "$channel" "Availability"
  burn_alert "Open Alvary SLO: availability burning slowly" "$availability" 6 6h 30m "$channel" "Availability"
  local operation id budget words filter slo
  for operation in "${OPERATIONS[@]}"; do
    IFS='|' read -r id budget words <<<"$operation"
    if [[ -n "$LATENCY_METRIC" ]]; then
      filter="metric.type=\\\"$LATENCY_METRIC\\\" AND $REQUESTS AND metric.label.operation=\\\"$id\\\""
    else
      filter="metric.type=\\\"run.googleapis.com/request_latencies\\\" AND $REQUESTS"
    fi
    cat >"$file" <<SLO
{
  "displayName": "Latency, $words: $LATENCY_GOAL within $budget ms, 30 days${LATENCY_METRIC:+ (per operation)}",
  "goal": $LATENCY_GOAL,
  "rollingPeriod": "$WINDOW",
  "serviceLevelIndicator": {"requestBased": {"distributionCut": {
    "distributionFilter": "$filter",
    "range": {"min": 0, "max": $budget}
  }}}
}
SLO
    slo="$(objective "latency-$id" "$file")"
    if [[ -n "$LATENCY_METRIC" || "$budget" == "$LOOSEST" ]]; then
      burn_alert "Open Alvary SLO: latency of $id burning slowly" "$slo" 6 6h 30m "$channel" "Latency ($words)"
    fi
  done
  rm -f "$file"
  echo "Objectives and alerts in place. Cloud console: Monitoring, Services, Open Alvary public API."
}

status() {  # each objective, its goal and the share of its error budget left
  local listing names name filter series end start
  listing="$(api GET "services/$MONITORED/serviceLevelObjectives" 2>/dev/null)" \
    || { echo "No objectives yet: run $0 setup."; return; }
  end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  start="$(date -u -v-10M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"
  names="$(python3 -c 'import json, sys
for s in json.loads(sys.argv[1]).get("serviceLevelObjectives", []): print(s["name"])' "$listing")"
  for name in $names; do
    filter="$(encode "select_slo_budget_fraction(\"$name\")")"
    series="$(api GET "timeSeries?filter=$filter&interval.startTime=$start&interval.endTime=$end" 2>/dev/null \
      || echo '{}')"
    python3 - "$listing" "$name" "$series" <<'PY'
import json, sys
listing, name, series = json.loads(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
slo = next(s for s in listing["serviceLevelObjectives"] if s["name"] == name)
points = (series.get("timeSeries") or [{}])[0].get("points") or []
left = f"{points[0]['value']['doubleValue']:.1%}" if points else "n/a"
print(f"{name.rsplit('/', 1)[-1]:<26} goal {slo['goal']:<6} budget left {left:<7} {slo.get('displayName', '')}")
PY
  done
  "${GC[@]}" alpha monitoring policies list --filter='displayName:"Open Alvary SLO"' \
    --format='value(displayName,enabled)' || true
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
  status) status ;;
  *) echo "Usage: $0 plan|setup|status" >&2; exit 2 ;;
esac
