"""Consolidated findings item 13: the Google Cloud deployment's database backups are watched."""

from datetime import UTC, datetime

import httpx
import pytest

from operations import cloud_backups

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def client(runs: dict[str, list[dict] | int], token_status: int = 200) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(token_status, json={"access_token": "t"})
        assert request.headers["Authorization"] == "Bearer t"
        instance = request.url.path.split("/")[-2]
        found = runs[instance]
        if isinstance(found, int):
            return httpx.Response(found, json={"error": {"message": "denied"}})
        return httpx.Response(200, json={"items": found})

    return httpx.Client(transport=httpx.MockTransport(answer))


def run(status, end):
    return {"status": status, "endTime": end}


def test_a_recent_successful_backup_of_each_database_passes():
    runs = {
        "pg": [run("SUCCESSFUL", "2026-11-16T03:10:00.000Z"), run("FAILED", "2026-11-16T11:00:00Z")],
        "review-pg": [run("SUCCESSFUL", "2026-11-15T13:00:00Z")],
    }
    assert cloud_backups.check([("p", "pg"), ("p", "review-pg")], client(runs), NOW) == []


@pytest.mark.parametrize(
    "runs, says",
    [
        ([run("SUCCESSFUL", "2026-11-15T08:00:00Z")], "28 hours old"),
        ([run("FAILED", "2026-11-16T11:00:00Z")], "no successful backup"),
        ([], "no successful backup"),
        (403, "could not be listed"),
    ],
    ids=["stale", "only-failed", "none", "denied"],
)
def test_a_stale_missing_or_unreadable_backup_list_fails(runs, says):
    [problem] = cloud_backups.check([("p", "pg")], client({"pg": runs}), NOW)
    assert says in problem


def test_no_credentials_or_no_database_named_fails_rather_than_passing():
    assert "credentials" in cloud_backups.check([("p", "pg")], client({}, token_status=404), NOW)[0]
    assert "No database is named" in cloud_backups.check([], client({}), NOW)[0]


def test_instances_are_named_by_project_and_instance(monkeypatch):
    assert cloud_backups.instances_from("p:a, q:b,") == [("p", "a"), ("q", "b")]
    with pytest.raises(SystemExit):
        cloud_backups.instances_from("just-a-name")
    monkeypatch.setenv("ALVARY_BACKUP_INSTANCES", "")
    assert cloud_backups.main(["check"]) == 1  # unset or empty is a failure, never a pass
    monkeypatch.setenv("ALVARY_BACKUP_INSTANCES", "none")
    assert cloud_backups.main(["check"]) == 0  # said on purpose
