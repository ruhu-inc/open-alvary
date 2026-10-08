"""SYS-07 step 1–3: denials commit with pending delivery and reach the journal in order."""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_acquisition import chain as chain
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from operations.journal import FileJournal, JournalError
from rights.database import PolicyNamespace, PolicyRepository
from rights.journal_delivery import dispatch

pytestmark = pytest.mark.postgres


def deliveries(f):
    with f.engine.connect() as conn:
        return conn.execute(
            text(
                f"SELECT event_id,kind,affected,revision,durability,journal_sequence,erasure "
                f"FROM {f.name(conn, 'policy', 'denial_delivery')} ORDER BY revision"
            )
        ).all()


def run(f, journal, limit=100):
    with f.engine.begin() as conn:
        return dispatch(conn, f.ns, journal, limit)


def test_withdrawing_an_allowed_operation_commits_a_pending_denial_and_denies_at_once(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    assert deliveries(f) == []  # first approval is not a denial
    f.decision(revision=2, redistribute_metadata="deny")
    [row] = deliveries(f)
    assert (row.kind, row.affected, row.revision, row.durability) == (
        "publication_deny",
        [f.collection],
        1,
        "pending",
    )
    with f.as_role("serving") as conn:
        # Pending and durable denials are treated identically by serving.
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed


@pytest.mark.parametrize(
    "second",
    [
        dict(redistribute_metadata="allow", quote="allow"),  # broader
        dict(redistribute_metadata="allow", expires_at=None),  # unchanged vector
    ],
)
def test_non_narrowing_revisions_record_no_denial(foundation, second):
    f = foundation
    f.decision(redistribute_metadata="allow")
    if second.get("expires_at", 0) is None:
        second.pop("expires_at")
    f.decision(revision=2, **second)
    assert deliveries(f) == []


def test_revoking_approval_state_is_a_denial(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    f.decision(revision=2, redistribute_metadata="allow", state="revoked")
    assert [r.kind for r in deliveries(f)] == ["publication_deny"]


def test_rolled_back_denial_leaves_no_delivery_record(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    with pytest.raises(RuntimeError), f.as_role("review") as conn:
        conn.execute(
            text(f"""
            INSERT INTO {f.name(conn, "policy", "collection_decision")}
                (revision,collection_id,actor_id,evidence_id,valid_from,expires_at,state,reason,
                 basis_type,conditions_satisfied,metadata_privacy_class,privacy_reason)
            VALUES (2,:c,:a,:e,:from,:until,'revoked','Synthetic','original_authorship',true,
                'no_personal_data','Synthetic')
            """),
            {
                "c": f.collection,
                "a": f.actors["review"],
                "e": f.evidence,
                "from": f.now - timedelta(hours=1),
                "until": f.now + timedelta(days=1),
            },
        )
        raise RuntimeError("abort after the trigger fired")
    assert deliveries(f) == []


def test_metadata_suspension_records_a_denial(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    f.release()
    f.release("suspend_metadata", expected=1)
    assert [(r.kind, r.affected) for r in deliveries(f)] == [("publication_deny", [f.work])]


def test_dispatch_delivers_in_revision_order_and_records_receipts(foundation, tmp_path):
    f = foundation
    f.decision(redistribute_metadata="allow", quote="allow")
    f.decision(revision=2, redistribute_metadata="allow")
    f.decision(revision=3, redistribute_metadata="allow", quote="allow")
    f.decision(revision=4)
    journal = FileJournal(tmp_path)
    result = run(f, journal)
    assert (result.delivered, result.pending) == (2, 0)
    rows = deliveries(f)
    events = journal.read()
    assert [e.event_id for _, e in events] == [r.event_id for r in rows]
    assert [r.journal_sequence for r in rows] == [1, 2]
    assert [e.revision for _, e in events] == [1, 2]
    assert run(f, journal).delivered == 0


def test_crash_after_append_before_commit_reconciles_without_duplicates(foundation, tmp_path):
    f = foundation
    f.decision(redistribute_metadata="allow")
    f.decision(revision=2)
    journal = FileJournal(tmp_path)
    with f.engine.connect() as conn:
        dispatch(conn, f.ns, journal)
        conn.rollback()  # receipt never committed
    assert deliveries(f)[0].durability == "pending" and journal.high_watermark()[0] == 1
    assert run(f, journal).delivered == 1
    assert journal.high_watermark()[0] == 1 and deliveries(f)[0].journal_sequence == 1


class Unavailable:
    def append(self, event):
        raise JournalError("Journal unavailable")


def test_journal_outage_leaves_denial_pending_and_effective(foundation, tmp_path):
    f = foundation
    f.decision(redistribute_metadata="allow")
    f.decision(revision=2)
    assert (run(f, Unavailable()).delivered, deliveries(f)[0].durability) == (0, "pending")
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed
    assert run(f, FileJournal(tmp_path)).pending == 0


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE {t} SET affected=ARRAY['other']",
        "UPDATE {t} SET durability='pending'",
        "DELETE FROM {t}",
        "TRUNCATE {t}",
    ],
)
def test_delivery_records_are_append_only_even_for_the_owner(foundation, tmp_path, statement):
    f = foundation
    f.decision(redistribute_metadata="allow")
    f.decision(revision=2)
    run(f, FileJournal(tmp_path))
    with pytest.raises(DBAPIError) as denied, f.engine.begin() as conn:
        conn.execute(text(statement.format(t=f.name(conn, "policy", "denial_delivery"))))
    assert denied.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("role", ["serving", "acquisition", "review", "release"])
def test_runtime_roles_cannot_read_or_write_delivery_records(foundation, role):
    f = foundation
    with pytest.raises(DBAPIError) as denied, f.as_role(role) as conn:
        conn.execute(text(f"SELECT * FROM {f.name(conn, 'policy', 'denial_delivery')}"))
    assert denied.value.orig.sqlstate == "42501"


def test_downgrade_refuses_once_denials_exist_and_is_clean_when_empty(pg_engine):
    from alembic import command
    from alembic.config import Config

    migrate(pg_engine)
    cfg = Config("alembic.ini")
    with pg_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0013")
    migrate(pg_engine)
    with pg_engine.begin() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        fn = ns.qualified(conn, "policy", "record_denial")
        conn.execute(text(f"SELECT {fn}('erasure', ARRAY['synthetic'], true)"))
    with pytest.raises(RuntimeError, match="forward repair"), pg_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0013")


def test_event_ids_are_uuids(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    f.decision(revision=2)
    assert isinstance(deliveries(f)[0].event_id, UUID)


def test_staged_erasure_records_one_erasure_event(chain, tmp_path):
    from test_postgres_acquisition import declare, stage

    from rights.acquisition import SyntheticAcquisition

    f, _, worker, _, _, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    with worker.engine.begin() as conn:
        service = SyntheticAcquisition(conn, f.ns)
        assert service.erase(artifact) and service.erase(artifact)
    rows = deliveries(f)
    assert [(r.kind, r.affected, r.erasure) for r in rows] == [("erasure", [str(artifact)], True)]
    assert run(f, FileJournal(tmp_path)).pending == 0
