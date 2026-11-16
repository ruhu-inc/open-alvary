"""Whether each of this environment's Cloud SQL databases has a recent successful backup (findings item 13).

The backup check in .github/workflows/backup-freshness.yml watches the self-hosted deployment's R2
bucket, which the Google Cloud deployment never writes, so nothing watched these. The daily job runs
this check: it reads each instance's own list of backups from the Cloud SQL Admin API, as the job's
service account (roles/cloudsql.viewer), and fails, so the job-failure alert fires, when the newest
successful backup is older than MAX_AGE, when there is none, or when the list cannot be read.

    python -m operations.cloud_backups check

ALVARY_BACKUP_INSTANCES names the instances, comma-separated, each as project:instance; "none" says
on purpose that this process has no databases to check (a local run). Left unset, the check fails.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

import httpx

MAX_AGE = timedelta(hours=26)  # a daily backup, with two hours' grace, as the R2 check allows
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
API = "https://sqladmin.googleapis.com/v1/projects/{project}/instances/{instance}/backupRuns"


def token(client: httpx.Client) -> str:
    answer = client.get(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    answer.raise_for_status()
    return answer.json()["access_token"]


def newest(client: httpx.Client, bearer: str, project: str, instance: str) -> datetime | None:
    """When the newest successful backup of this instance finished, or None when it has none."""
    answer = client.get(
        API.format(project=project, instance=instance),
        params={"maxResults": 20},
        headers={"Authorization": f"Bearer {bearer}"},
    )
    answer.raise_for_status()
    finished = [
        datetime.fromisoformat(run["endTime"].replace("Z", "+00:00"))
        for run in answer.json().get("items", [])
        if run.get("status") == "SUCCESSFUL" and run.get("endTime")
    ]
    return max(finished, default=None)


def check(instances: list[tuple[str, str]], client: httpx.Client, now: datetime | None = None) -> list[str]:
    """Problems found, one line each; none when every instance has a recent backup."""
    now = now or datetime.now(UTC)
    if not instances:
        return ["No database is named (ALVARY_BACKUP_INSTANCES), so no backup was checked."]
    try:
        bearer = token(client)
    except (httpx.HTTPError, KeyError, ValueError) as error:
        return [f"The job's credentials could not be read: {error}"]
    problems = []
    for project, instance in instances:
        try:
            last = newest(client, bearer, project, instance)
        except (httpx.HTTPError, KeyError, ValueError) as error:
            problems.append(f"{project}:{instance}: its backups could not be listed: {error}")
            continue
        if last is None:
            problems.append(f"{project}:{instance}: no successful backup is listed.")
        elif now - last > MAX_AGE:
            hours = int((now - last).total_seconds() // 3600)
            problems.append(
                f"{project}:{instance}: the newest successful backup is {hours} hours old ({last})."
            )
        else:
            print(f"{project}:{instance}: newest successful backup {last}.")
    return problems


def instances_from(value: str) -> list[tuple[str, str]]:
    found = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        project, _, instance = item.partition(":")
        if not project or not instance:
            raise SystemExit(f"Name each instance as project:instance, not {item!r}")
        found.append((project, instance))
    return found


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["check"]:
        print(__doc__)
        return 2
    named = os.environ.get("ALVARY_BACKUP_INSTANCES", "")
    if named.strip() == "none":
        print("No database backups to check here (ALVARY_BACKUP_INSTANCES=none).")
        return 0
    with httpx.Client(timeout=20) as client:
        problems = check(instances_from(named), client)
    for problem in problems:
        print(f"ERROR backup check: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
