#!/bin/sh
# Failure alert: opens a GitHub issue (GitHub emails watchers, including the maintainer).
# Arguments: <event> <stage>. Needs GITHUB_ALERT_TOKEN (fine-grained, Issues: write on one repo)
# and GITHUB_ALERT_REPOSITORY (owner/name). Never includes data, URLs or credentials.
set -eu
event=${1:-unknown}
stage=${2:-unknown}
case "$event:$stage" in *[!A-Za-z0-9_:-]*) event=unknown; stage=unknown ;; esac
: "${GITHUB_ALERT_TOKEN:?}" "${GITHUB_ALERT_REPOSITORY:?}"
body=$(printf '{"title":"Open Alvary alert: %s (%s)","body":"Automated alert from the deployment host. Event: %s; stage: %s. See deployment/runbook.md.","labels":["operations-alert"]}' "$event" "$stage" "$event" "$stage")
wget -q -O /dev/null --header "Authorization: Bearer $GITHUB_ALERT_TOKEN" \
  --header "Accept: application/vnd.github+json" --post-data "$body" \
  "https://api.github.com/repos/$GITHUB_ALERT_REPOSITORY/issues"
