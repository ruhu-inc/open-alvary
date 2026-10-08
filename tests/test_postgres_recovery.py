"""SYS-07 serving epochs, restore recovery and conservative reset drills."""

from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

from operations.journal import FileJournal
from rights.database import PolicyRepository
from rights.journal_delivery import dispatch
from rights.recovery import EpochRefused, RecoveryRequired, close_epoch, open_epoch, recover

pytestmark = pytest.mark.postgres


def metadata_visible(f):
    with f.as_role("serving") as conn:
        return PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed


def deliver(f, journal):
    with f.engine.begin() as conn:
        return dispatch(conn, f.ns, journal)


def serve(f, journal):
    """A cloned test database is a new instance: recover (empty journal), then open an epoch."""
    result = recover(f.engine, f.ns, journal, None, "Synthetic test instance binding")
    deliver(f, journal)
    open_epoch(f.engine, f.ns, journal)
    return result


@contextmanager
def snapshot(f):
    """A consistent copy with a new database OID: what a logical restore looks like."""
    name = "oa_restore_" + uuid4().hex[:16] + "_test"
    admin = create_engine(f.engine.url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    source = f.engine.url.database
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{source}"'))
    options = {"options": f"-csearch_path={f.ns.base}"}
    engine = create_engine(f.engine.url.set(database=name), poolclass=NullPool, connect_args=options)
    try:
        yield replace(f, engine=engine)
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def epochs(f):
    with f.engine.connect() as conn:
        return (
            conn.execute(
                text(f"SELECT state FROM {f.name(conn, 'policy', 'serving_epoch')} ORDER BY opened_at")
            )
            .scalars()
            .all()
        )


def test_serving_requires_recovered_instance_and_open_epoch(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    f.decision(redistribute_metadata="allow")
    assert not metadata_visible(f)
    with pytest.raises(RecoveryRequired):
        open_epoch(f.engine, f.ns, journal)
    assert serve(f, journal).decision == "continuity_verified"
    assert metadata_visible(f)
    close_epoch(f.engine, f.ns, journal)
    assert not metadata_visible(f) and epochs(f) == ["closed"]
    kinds = [e.kind for _, e in journal.read()]
    assert kinds == ["epoch_open", "epoch_close"]


def test_pending_denials_block_opening_and_closing(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    f.decision(redistribute_metadata="allow")
    serve(f, journal)
    f.decision(revision=2)
    with pytest.raises(EpochRefused, match="pending"):
        close_epoch(f.engine, f.ns, journal)
    deliver(f, journal)
    close_epoch(f.engine, f.ns, journal)
    with pytest.raises(EpochRefused, match="already open"):
        open_epoch(f.engine, f.ns, journal)
        open_epoch(f.engine, f.ns, journal)


def test_a_restored_copy_never_inherits_an_open_epoch(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    f.decision(redistribute_metadata="allow")
    serve(f, journal)
    assert metadata_visible(f)
    with f.engine.begin() as conn:
        # Simulate a restore: the dump carries the epoch row but the instance differs.
        conn.execute(text(f"UPDATE {f.name(conn, 'policy', 'instance_binding')} SET database_oid=0"))
    assert not metadata_visible(f)


def test_clean_close_and_journaled_denial_survive_restore_with_continuity(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    f.decision(redistribute_metadata="allow")
    serve(f, journal)
    close_epoch(f.engine, f.ns, journal)
    sequence, chain = journal.high_watermark()
    with snapshot(f) as restored:  # the backup
        # After the backup: a new epoch, a denial delivered to the journal, a clean close.
        open_epoch(f.engine, f.ns, journal)
        f.decision(revision=2, redistribute_metadata="deny")
        deliver(f, journal)
        close_epoch(f.engine, f.ns, journal)
        # The live database is lost; the restored copy still allows the collection.
        result = recover(
            restored.engine, f.ns, journal, {"journal_sequence": sequence, "journal_chain": chain}, "Drill"
        )
        assert result.decision == "continuity_verified"
        deliver(restored, journal)
        open_epoch(restored.engine, f.ns, journal)
        assert not metadata_visible(restored)  # replayed journal denial holds it closed
        restored.decision(revision=2, redistribute_metadata="allow")  # fresh reviewed revision
        assert metadata_visible(restored)


def test_unclean_epoch_forces_conservative_reset_of_every_collection(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    f.decision(redistribute_metadata="allow")
    serve(f, journal)  # never closed: DB lost during an open epoch
    result = recover(f.engine, f.ns, journal, None, "Synthetic unclean loss")
    assert result.decision == "conservative_reset"
    assert result.predicate["unclean_epochs"] and epochs(f) == ["abandoned"]
    deliver(f, journal)
    open_epoch(f.engine, f.ns, journal)
    assert not metadata_visible(f)
    f.decision(revision=2, redistribute_metadata="allow")
    assert metadata_visible(f)


def test_pending_denial_missing_from_journal_forces_conservative_reset(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    f.decision(redistribute_metadata="allow")
    f.decision(revision=2, redistribute_metadata="allow", quote="deny")
    f.decision(revision=3, redistribute_metadata="allow")
    # The second revision narrowed nothing allowed; the third denies nothing; force one pending:
    f.decision(revision=4, redistribute_metadata="deny")
    result = recover(f.engine, f.ns, journal, None, "Synthetic unjournaled pending deny")
    assert result.decision == "conservative_reset" and result.predicate["unmatched_pending"]


def test_journal_lacking_the_backup_checkpoint_forces_conservative_reset(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    result = recover(
        f.engine, f.ns, journal, {"journal_sequence": 5, "journal_chain": "a" * 64}, "Synthetic lost journal"
    )
    assert result.decision == "conservative_reset"
    assert not result.predicate["checks"]["checkpoint_in_journal"]


def test_replay_reapplies_metadata_suspension_and_raises_allocator_floor(foundation, tmp_path):
    f = foundation
    journal = FileJournal(tmp_path)
    f.decision(redistribute_metadata="allow")
    f.release()
    with snapshot(f) as restored:
        f.release("suspend_metadata", expected=1)
        deliver(f, journal)
        recover(restored.engine, f.ns, journal, None, "Synthetic replay")
        with restored.engine.connect() as conn:
            table = f.name(conn, "corpus", "metadata_projection")
            assert conn.scalar(text(f"SELECT suspended FROM {table} WHERE work_id=:w"), {"w": f.work})
            assert (
                conn.scalar(text(f"SELECT revision FROM {f.name(conn, 'policy', 'publication_revision')}"))
                >= 1
            )


def test_epoch_identity_cannot_be_forged_by_inserting_a_row(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    with f.engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {f.name(conn, 'policy', 'serving_epoch')}"
                "(epoch_id,system_identifier,database_oid,state,open_sequence) VALUES (:id,1,1,'open',1)"
            ),
            {"id": uuid4()},
        )
    assert not metadata_visible(f)
