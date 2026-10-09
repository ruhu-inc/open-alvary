#!/bin/sh
# Point-in-time recovery jobs (pgBackRest inside the db container). Run from the repository root.
#   deployment/pitr.sh init          once: create the stanza and verify archiving
#   deployment/pitr.sh full|diff     scheduled: weekly full (Sunday), daily differential
#   deployment/pitr.sh check|info    verify archiving / list backups and the recovery window
set -eu
umask 077
action=${1:?Usage: deployment/pitr.sh init|full|diff|check|info}
env_file=deployment/.env
compose="docker compose --env-file $env_file -f deployment/compose.yaml -f deployment/pitr.yaml"
run() { $compose exec -T -u postgres db pgbackrest --stanza=main "$@"; }
alert() {
  token=$(sed -n 's/^GITHUB_ALERT_TOKEN=//p' "$env_file" | tail -n 1)
  repo=$(sed -n 's/^GITHUB_ALERT_REPOSITORY=//p' "$env_file" | tail -n 1)
  if [ -n "$token" ] && [ -n "$repo" ]; then
    GITHUB_ALERT_TOKEN=$token GITHUB_ALERT_REPOSITORY=$repo deployment/alert_github_issue.sh "pitr-failed" "$action" || true
  else
    echo "WARNING: no alert channel configured; PITR failure not delivered" >&2
  fi
}
case "$action" in
  init) run stanza-create && run check ;;
  full) run backup --type=full ;;
  diff) run backup --type=diff ;;
  check) run check ;;
  info) run info ;;
  *) echo "Unknown action: $action" >&2; exit 2 ;;
esac || { alert; exit 1; }
