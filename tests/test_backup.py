"""Encrypted backup, isolated restore verification and explicit retention."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from operations.backup import (
    BackupError,
    Database,
    DirectoryDestination,
    Tools,
    purge,
    restore_verify,
    retained,
)


def stamp(moment):
    return f"open-alvary-{moment:%Y%m%dT%H%M%SZ}.dump.gpg"


def test_retention_keeps_newest_per_day_and_week_and_always_the_latest():
    start = datetime(2026, 10, 7, 3, tzinfo=UTC)
    names = [stamp(start - timedelta(hours=12 * i)) for i in range(60)]
    keep = retained(names, keep_daily=7, keep_weekly=4)
    assert names[0] in keep
    days = {n[12:20] for n in keep}
    assert len(keep) == len(days) and len(days) <= 7 + 4
    assert all(n == max(m for m in names if m[12:20] == n[12:20]) for n in keep)
    assert retained(names, keep_daily=1, keep_weekly=0) == {names[0]}


def test_database_url_password_never_enters_dsn_or_repr():
    database = Database.from_url("postgresql+psycopg://operator:s3cr%40t@db.example:5432/open_alvary")
    assert database.dsn == "postgresql://operator@db.example:5432/open_alvary"
    assert database.password == "s3cr@t"
    assert "s3cr" not in repr(database) and database.name == "open_alvary"
    with pytest.raises(BackupError):
        Database.from_url("sqlite:///open_alvary.db")


def test_destination_is_atomic_and_never_overwrites(tmp_path):
    root = tmp_path / "offhost"
    root.mkdir()
    source = tmp_path / "payload"
    source.write_bytes(b"encrypted")
    destination = DirectoryDestination(root)
    destination.put("a", source)
    with pytest.raises(BackupError):
        destination.put("a", source)
    assert destination.names() == ["a"] and not list(root.glob(".*"))


def test_purge_removes_complete_pairs_and_ignores_unrelated_files(tmp_path):
    destination = DirectoryDestination(tmp_path)
    start = datetime(2026, 10, 7, tzinfo=UTC)
    names = [stamp(start - timedelta(days=i)) for i in range(5)]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
        (tmp_path / (name + ".json")).write_text("{}")
    (tmp_path / "unrelated.txt").write_text("keep")
    (tmp_path / stamp(start - timedelta(days=30))).write_bytes(b"incomplete, no marker")
    result = purge(destination, keep_daily=2, keep_weekly=0)
    assert result["kept"] == sorted(names[:2])
    assert sorted(destination.names()) == sorted(
        [*names[:2], *(n + ".json" for n in names[:2]), "unrelated.txt", stamp(start - timedelta(days=30))]
    )
    with pytest.raises(BackupError):
        purge(destination, keep_daily=0, keep_weekly=0)


@pytest.mark.parametrize("target", ["open_alvary", "open_alvary_live"])
def test_restore_refuses_any_database_not_named_as_isolated(tmp_path, target):
    database = Database.from_url(f"postgresql://operator@localhost/{target}")
    with pytest.raises(BackupError, match="configuration"):
        restore_verify(
            stamp(datetime.now(UTC)), DirectoryDestination(tmp_path), tmp_path / "k", database, Tools()
        )


def test_cli_failure_reports_stage_only_and_invokes_alert_hook(tmp_path):
    record = tmp_path / "alert"
    env = dict(os.environ)
    env["ALVARY_BACKUP_DATABASE_URL"] = "postgresql://operator:do-not-echo@127.0.0.1:1/open_alvary"
    hook = tmp_path / "hook.py"
    hook.write_text(f"import sys\nopen({str(record)!r}, 'w').write(' '.join(sys.argv[1:]))\n")
    env["ALVARY_BACKUP_ALERT_COMMAND"] = f"{sys.executable} {hook}"
    key = tmp_path / "public.asc"
    key.write_text("not a key")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "operations.backup",
            "create",
            "--recipient-file",
            str(key),
            "--destination",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {"status": "failed", "stage": "encrypt"}
    assert "do-not-echo" not in completed.stdout + completed.stderr
    assert record.read_text() == "backup-failed encrypt"


# --- PostgreSQL drill -------------------------------------------------------------------


def keypair(tmp_path):
    from operations.backup import gpg_home

    public, secret = tmp_path / "recipient.asc", tmp_path / "identity.asc"
    with gpg_home() as home:
        base = ["gpg", "--homedir", str(home), "--batch", "--no-tty", "--pinentry-mode", "loopback"]
        base += ["--passphrase", ""]
        uid = "Synthetic backup <backup@example.invalid>"
        subprocess.run(
            [*base, "--quick-gen-key", uid, "default", "default", "never"], check=True, capture_output=True
        )
        public.write_bytes(
            subprocess.run([*base, "--armor", "--export"], check=True, capture_output=True).stdout
        )
        exported = subprocess.run([*base, "--armor", "--export-secret-keys"], check=True, capture_output=True)
        secret.write_bytes(exported.stdout)
    return public, secret


def tool_view(url):
    """Address as seen by the PostgreSQL tools (possibly inside a version-matched container)."""
    hostport = os.environ.get("ALVARY_TEST_PG_TOOL_HOSTPORT")
    database = Database.from_url(url.render_as_string(hide_password=False))
    if hostport:
        user = url.username + "@" if url.username else ""
        database = Database(f"postgresql://{user}{hostport}/{url.database}", database.password)
    return database


@pytest.fixture
def drill(tmp_path):
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config
    from postgres_support import validated_test_url
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    admin_url = validated_test_url()
    admin = create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    suffix = uuid4().hex[:12]
    names = {"source": f"oa_backup_{suffix}_test", "target": f"oa_backup_{suffix}_restore"}
    with admin.connect() as conn:
        for name in names.values():
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    engines = {k: create_engine(admin_url.set(database=v), poolclass=NullPool) for k, v in names.items()}
    try:
        with engines["source"].begin() as conn:
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        from api.store import Store
        from scripts.seed import seed

        seed(Store(engines["source"]))
        yield {
            "engines": engines,
            "source": tool_view(admin_url.set(database=names["source"])),
            "target": tool_view(admin_url.set(database=names["target"])),
            "tools": Tools(tuple(os.environ.get("ALVARY_TEST_PG_TOOL_PREFIX", "").split())),
        }
    finally:
        for engine in engines.values():
            engine.dispose()
        with admin.connect() as conn:
            for name in names.values():
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def content(engine, tables):
    from sqlalchemy import text

    with engine.connect() as conn:
        return {
            t: conn.scalar(
                text(f"SELECT md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) FROM {t} r")
            )
            for t in tables
        }


@pytest.mark.postgres
def test_encrypted_backup_restores_into_isolated_database_with_identical_content(drill, tmp_path):
    from operations.backup import create

    public, secret = keypair(tmp_path)
    offhost = tmp_path / "offhost"
    offhost.mkdir()
    destination = DirectoryDestination(offhost)
    metadata = create(drill["source"], public, destination, drill["tools"], datetime.now(UTC))
    stored = offhost / metadata["backup"]
    assert b"identity_collections" not in stored.read_bytes()  # ciphertext, not a plain dump
    assert metadata["journal_checkpoint"] is None and metadata["encryption"]["recipients"]
    report = restore_verify(metadata["backup"], destination, secret, drill["target"], drill["tools"])
    assert report["serving"] == "closed" and report["tables"] == len(metadata["tables"])
    assert content(drill["engines"]["source"], metadata["tables"]) == content(
        drill["engines"]["target"], metadata["tables"]
    )
    with pytest.raises(BackupError, match="restore"):
        restore_verify(metadata["backup"], destination, secret, drill["target"], drill["tools"])


@pytest.mark.postgres
def test_tampered_backup_and_secret_key_on_backup_host_are_rejected(drill, tmp_path):
    from operations.backup import create

    public, secret = keypair(tmp_path)
    offhost = tmp_path / "offhost"
    offhost.mkdir()
    destination = DirectoryDestination(offhost)
    with pytest.raises(BackupError, match="encrypt"):
        create(drill["source"], secret, destination, drill["tools"], datetime.now(UTC))
    assert destination.names() == []
    metadata = create(drill["source"], public, destination, drill["tools"], datetime.now(UTC))
    stored = Path(offhost / metadata["backup"])
    stored.write_bytes(stored.read_bytes()[:-1] + b"\0")
    with pytest.raises(BackupError, match="integrity"):
        restore_verify(metadata["backup"], destination, secret, drill["target"], drill["tools"])
