#!/bin/sh
# Failure alert: opens a GitHub issue (GitHub emails watchers, including the maintainer).
# Arguments: <event> <stage>. Needs GITHUB_ALERT_TOKEN (fine-grained, Issues: write on one repo)
# and GITHUB_ALERT_REPOSITORY (owner/name). The token is read from the environment inside Python,
# never placed on a command line; the issue carries no data, URLs or credentials.
set -eu
exec python3 - "${1:-unknown}" "${2:-unknown}" <<'PY'
import json, os, re, sys, urllib.request
event, stage = (v if re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", v) else "unknown" for v in sys.argv[1:3])
token, repository = os.environ["GITHUB_ALERT_TOKEN"], os.environ["GITHUB_ALERT_REPOSITORY"]
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
    sys.exit("Invalid GITHUB_ALERT_REPOSITORY")
body = {
    "title": f"Open Alvary alert: {event} ({stage})",
    "body": f"Automated alert from the deployment host. Event: {event}; stage: {stage}. See deployment/runbook.md.",
    "labels": ["operations-alert"],
}
request = urllib.request.Request(
    f"https://api.github.com/repos/{repository}/issues",
    data=json.dumps(body).encode(),
    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    method="POST",
)
urllib.request.urlopen(request, timeout=20).close()
PY
