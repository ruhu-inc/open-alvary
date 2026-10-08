"""M4.7 local end-to-end drills: backup → loss → isolated restore → journal recovery, and
withdrawal under admission saturation. Synthetic data only; not a production acceptance."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from test_backup import keypair, tool_view
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

from api.admission import AdmissionStore
from operations.backup import DirectoryDestination, Tools, create, restore_verify
from operations.journal import FileJournal
from rights.database import PolicyRepository, restore_grants
from rights.journal_delivery import dispatch
from rights.recovery import open_epoch, recover

pytestmark = pytest.mark.postgres


def inventory(conn, ns):
    """Pinned signatures, as verify_sql_resources builds them, without re-checking bodies."""
    import json as _json
    from importlib.resources import files

    signatures = {}
    for root in sorted(files("rights").joinpath("sql").iterdir(), key=lambda r: r.name):
        if root.name.startswith("v") and root.joinpath("manifest.json").is_file():
            manifest = _json.loads(root.joinpath("manifest.json").read_text())
            for entry in manifest["functions"] + manifest.get("new_functions", []):
                signatures[entry["signature"]] = entry
    return signatures


def visible(f, engine=None):
    target = f if engine is None else type(f)(**{**f.__dict__, "engine": engine})
    with target.as_role("serving") as conn:
        return PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed


def deliver(engine, ns, journal):
    with engine.begin() as conn:
        return dispatch(conn, ns, journal)


@pytest.fixture
def restore_target(foundation):
    from postgres_support import validated_test_url

    admin_url = validated_test_url()
    name = f"oa_drill_{uuid4().hex[:12]}_restore"
    admin = create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin_url.set(database=name)
    engine = create_engine(
        url, poolclass=NullPool, connect_args={"options": f"-csearch_path={foundation.ns.base}"}
    )
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_backup_loss_restore_and_journal_recovery_preserve_every_recent_denial(
    foundation, restore_target, tmp_path
):
    f = foundation
    (tmp_path / "journal").mkdir()
    journal = FileJournal(tmp_path / "journal")
    f.decision(redistribute_metadata="allow")
    f.serve()  # empty throwaway journal for the clone binding
    assert visible(f)
    # Day 0: everything durable, then the encrypted backup with its journal checkpoint.
    public, secret = keypair(tmp_path)
    offhost = tmp_path / "offhost"
    offhost.mkdir()
    destination = DirectoryDestination(offhost)
    tools = Tools(tuple(os.environ.get("ALVARY_TEST_PG_TOOL_PREFIX", "").split()))
    metadata = create(tool_view(f.engine.url), public, destination, tools, datetime.now(UTC), f.ns.base)
    # After the backup: a withdrawal reaches the journal, then the database is lost.
    f.decision(revision=2, redistribute_metadata="deny")
    assert deliver(f.engine, f.ns, journal).pending == 0
    assert not visible(f)
    url, engine = restore_target
    report = restore_verify(metadata["backup"], destination, secret, tool_view(url), tools, f.ns.base)
    assert report["serving"] == "closed" and report["journal_checkpoint"] == metadata["journal_checkpoint"]
    with engine.begin() as conn:
        exposed = conn.scalar(
            text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                " WHERE n.nspname=:s AND p.prosecdef AND has_function_privilege('public', p.oid, 'EXECUTE')"
            ),
            {"s": f.ns.schema("policy")},
        )
        assert exposed > 0  # the restore itself leaves privileged functions open to PUBLIC
        restore_grants(conn, f.ns)  # ... so grants and PUBLIC revocation are re-applied explicitly
    with engine.connect() as conn:
        from scripts.verify_sql_resources import verify_authority

        verify_authority(conn, f.ns, inventory(conn, f.ns))
    # The restored copy predates the withdrawal and is a new instance: it serves nothing.
    restored = type(f)(**{**f.__dict__, "engine": engine})
    assert not visible(f, engine)
    result = recover(engine, f.ns, journal, metadata["journal_checkpoint"], "Drill: day-old backup restore")
    assert result.decision == "conservative_reset"  # the pre-backup epoch was never cleanly closed
    deliver(engine, f.ns, journal)
    open_epoch(engine, f.ns, journal)
    assert not visible(f, engine)  # the post-backup denial survives the restore
    restored.decision(revision=2, redistribute_metadata="allow")
    assert visible(f, engine)  # only a fresh reviewed revision reopens


def test_operator_withdrawal_succeeds_while_admission_is_saturated(foundation, tmp_path):
    f = foundation
    f.decision(redistribute_metadata="allow")
    f.serve()
    store = AdmissionStore(f.engine, f.ns)
    request = {
        "buckets": [{"key": "ip:flood", "capacity": 1000, "refill_per_minute": 1}],
        "concurrency": [{"key": "global:all", "limit": 3}],
        "lease_seconds": 300,
    }
    leases = [store.admit(request) for _ in range(5)]
    assert sum(r["admitted"] for r in leases) == 3  # public capacity exhausted
    f.decision(revision=2, redistribute_metadata="deny")  # rights reviewer path is not admitted work
    assert not visible(f)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    assert deliver(f.engine, f.ns, FileJournal(journal_dir)).pending == 0


def test_backup_metadata_never_contains_plaintext_or_credentials(foundation, tmp_path):
    f = foundation
    public, _ = keypair(tmp_path)
    offhost = tmp_path / "offhost"
    offhost.mkdir()
    tools = Tools(tuple(os.environ.get("ALVARY_TEST_PG_TOOL_PREFIX", "").split()))
    metadata = create(
        tool_view(f.engine.url), public, DirectoryDestination(offhost), tools, datetime.now(UTC), f.ns.base
    )
    sidecar = (offhost / (metadata["backup"] + ".json")).read_text()
    assert "test-only" not in sidecar and "Synthetic" not in sidecar
    assert json.loads(sidecar)["journal_checkpoint"] is not None
    assert Path(offhost / metadata["backup"]).stat().st_size > 0
