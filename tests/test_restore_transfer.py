"""The restore job's transfer place and role plan (deployment/azure/restore.py), without a database:
the same job reads and writes Azure Blob Storage with the Container Apps identity and Cloud Storage
with the Cloud Run service account, and remakes the source's roles without granting what the target's
platform keeps for itself."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deployment" / "azure" / "restore.py"


def load():
    spec = importlib.util.spec_from_file_location("transfer_restore", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def calls(monkeypatch):
    """Every request the job makes, answered with a token or a file's bytes."""
    made = []

    def urlopen(request, timeout):
        body = request.data.read() if hasattr(request.data, "read") else request.data
        made.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": dict(request.header_items()),
                "body": body,
            }
        )
        if "token" in request.full_url:
            return Answer(json.dumps({"access_token": "t0ken"}).encode())
        return Answer(b"bytes of " + request.full_url.encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return made


def test_a_cloud_storage_place_is_read_and_written_as_the_jobs_service_account(tmp_path, monkeypatch, calls):
    restore = load()
    monkeypatch.setenv("TRANSFER", "gs://open-alvary-staging-transfer/public/")
    bearer = restore.token()
    restore.fetch("database.dump", tmp_path / "database.dump", bearer)
    (tmp_path / "roles.json").write_text("{}")
    restore.upload("roles.json", tmp_path / "roles.json", bearer)

    token, fetched, uploaded = calls
    assert token["url"] == restore.GOOGLE_TOKEN and token["headers"]["Metadata-flavor"] == "Google"
    assert fetched["url"] == (
        "https://storage.googleapis.com/storage/v1/b/open-alvary-staging-transfer/o/public%2Fdatabase.dump?alt=media"
    )
    assert fetched["headers"]["Authorization"] == "Bearer t0ken" and "X-ms-version" not in fetched["headers"]
    assert (tmp_path / "database.dump").read_bytes() == b"bytes of " + fetched["url"].encode()
    assert uploaded["method"] == "POST" and uploaded["url"] == (
        "https://storage.googleapis.com/upload/storage/v1/b/open-alvary-staging-transfer/o"
        "?uploadType=media&name=public%2Froles.json"
    )
    assert uploaded["headers"]["Content-length"] == "2" and uploaded["body"] == b"{}"


def test_an_azure_place_is_read_and_written_as_before(tmp_path, monkeypatch, calls):
    restore = load()
    place = "https://openalvarystgsccorpab12.blob.core.windows.net/transfer/review/"
    monkeypatch.setenv("TRANSFER", place)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "header")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client")
    bearer = restore.token()
    restore.fetch("counts.json", tmp_path / "counts.json", bearer)
    (tmp_path / "database.dump").write_bytes(b"dump")
    restore.upload("database.dump", tmp_path / "database.dump", bearer)

    token, fetched, uploaded = calls
    assert token["url"].startswith(
        "http://localhost:42356/msi/token?resource=https%3A%2F%2Fstorage.azure.com%2F"
    )
    assert "client_id=client" in token["url"] and token["headers"]["X-identity-header"] == "header"
    assert fetched["url"] == place + "counts.json" and fetched["headers"]["X-ms-version"] == "2023-11-03"
    assert uploaded["method"] == "PUT" and uploaded["url"] == place + "database.dump"
    assert uploaded["headers"]["X-ms-blob-type"] == "BlockBlob" and uploaded["body"] == b"dump"


def test_dump_writes_the_jobs_own_database_to_the_place(tmp_path, monkeypatch, calls):
    restore = load()
    monkeypatch.setenv("TRANSFER", "gs://bucket/public/")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@10.0.0.3:5432/open_alvary?sslmode=require")
    dumped = []

    def dump(dsn, folder):
        dumped.append(dsn)
        for name in restore.FILES:
            (folder / name).write_text(name)
        return 0

    monkeypatch.setattr(restore, "dump", dump)
    assert restore.main(["dump"]) == 0
    assert dumped == ["postgresql://u:p@10.0.0.3:5432/open_alvary?sslmode=require"]
    assert [call["url"].rsplit("name=", 1)[-1] for call in calls[1:]] == [
        "public%2Fdatabase.dump",
        "public%2Froles.json",
        "public%2Fcounts.json",
    ]
    assert restore.main(["dump", "extra"]) == 2


def test_the_role_plan_leaves_the_platforms_roles_and_what_is_in_place():
    restore = load()
    roles = {
        "roles": [
            {"name": "cloudsqlsuperuser", "login": False},
            {"name": "open_alvary_access", "login": True},
            {"name": "catalogue_reader", "login": False},
        ],
        "memberships": [
            {"role": "cloudsqlsuperuser", "member": "open_alvary", "member_is_administrator": True},
            {"role": "catalogue_reader", "member": "open_alvary_access", "member_is_administrator": False},
            {"role": "catalogue_reader", "member": "open_alvary", "member_is_administrator": True},
            {"role": "already", "member": "open_alvary_access", "member_is_administrator": False},
        ],
    }
    target = {
        "existing": {"cloudsqlsuperuser", "postgres", "open_alvary", "already"},
        "grantable": {"already"},
        "memberships": {("cloudsqlsuperuser", "open_alvary"), ("already", "open_alvary_access")},
        "administrator": "open_alvary",
    }
    statements, left = restore.role_statements(roles, target, "open_alvary_access", "it's")
    assert statements == [
        "CREATE ROLE \"open_alvary_access\" LOGIN PASSWORD 'it''s' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
        'CREATE ROLE "catalogue_reader" NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION',
        'GRANT "catalogue_reader" TO "open_alvary_access"',
        'GRANT "catalogue_reader" TO "open_alvary"',
    ]
    assert left == 2
    # An Azure target: Cloud SQL's role is made there, so the administrator grants it.
    azure = {
        **target,
        "existing": {"azure_pg_admin", "open_alvary"},
        "memberships": set(),
        "grantable": set(),
    }
    statements, left = restore.role_statements(roles, azure, "open_alvary_access", "pw")
    assert 'GRANT "cloudsqlsuperuser" TO "open_alvary"' in statements and left == 0
    with pytest.raises(SystemExit, match="ACCESS_ROLE_PASSWORD"):
        restore.role_statements(roles, target, "open_alvary_access", None)
