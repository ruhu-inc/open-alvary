"""Bounded native lifecycle, immutable lineage, transaction ordering and recovery."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_acquisition import PURPOSE, RAW, declare, stage
from test_postgres_acquisition import chain as chain
from test_postgres_operators import apply
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from api.identity import freeze
from rights.acquisition import SyntheticAcquisition
from rights.commands import parse_command
from rights.lifecycle import AcquisitionLifecycle
from rights.operators import revoke_operator
from schema.operator import LifecycleRequest

pytestmark = pytest.mark.postgres


def request(f, **changes):
    return LifecycleRequest(
        command_id=uuid4(),
        collection_id=f.collection,
        reason="Original synthetic lifecycle reconciliation",
        **changes,
    )


def reconcile(f, worker, req=None, **changes):
    with worker.engine.begin() as conn:
        return AcquisitionLifecycle(conn, f.ns).reconcile(req or request(f, **changes))


def snapshot(f, artifact):
    with f.engine.connect() as conn:
        return dict(
            conn.execute(
                text(f"SELECT * FROM {f.name(conn, 'staging', 'staged_artifact')} WHERE id=:id"),
                {"id": artifact},
            )
            .mappings()
            .one()
        )


def newer(f, rights, original, **fields):
    data = original.model_dump(mode="json")
    data.update(command_id=str(uuid4()), expected_revision=original.expected_revision + 1)
    data["payload"].update(fields)
    cmd = parse_command(json.dumps(data).encode())
    assert apply(f, rights, cmd).status == "applied"
    return cmd


@pytest.mark.parametrize("parent", ["controller", "privacy", "acquisition"])
def test_supersession_materializes_hold_and_preserves_lineage(chain, parent):
    f, rights, worker, ctl, pv, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    before = snapshot(f, artifact)
    newer(f, rights, {"controller": ctl, "privacy": pv, "acquisition": acq}[parent])
    result = reconcile(f, worker)
    assert result.status == "applied" and result.scanned == result.held == 1
    assert result.erasure_required == result.erased == 0
    after = snapshot(f, artifact)
    assert after.pop("state") == "held_pending_reassessment"
    before.pop("state")
    assert after == before
    assert reconcile(f, worker).held == 0
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) is None
    with f.engine.connect() as conn:
        event = (
            conn.execute(text(f"SELECT * FROM {f.name(conn, 'policy', 'staging_lifecycle_event')}"))
            .mappings()
            .one()
        )
        assert event["actor_id"] == worker.actor and event["assessment_revision"] == 1
        assert event[parent + "_head"] == 2
        assert event["cause"] == "authority_not_current"
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='staging_lifecycle'"
                )
            )
            == 1
        )


def test_current_artifact_keeps_state_and_empty_batch_is_receipted(chain):
    f, _, worker, _, _, acq = chain
    declare(f)
    assert reconcile(f, worker).scanned == 0
    artifact, _ = stage(f, worker, acq)
    before = snapshot(f, artifact)
    result = reconcile(f, worker)
    assert result.scanned == 1 and result.held == result.erasure_required == result.erased == 0
    assert snapshot(f, artifact) == before


def test_privacy_rejection_requires_then_erases_bytes_once_without_restoration(chain):
    f, rights, worker, _, pv, acq = chain
    declare(f, len(RAW))
    artifact, _ = stage(f, worker, acq)
    rejected = newer(f, rights, pv, clearance="rejected")
    first = request(f)
    result = reconcile(f, worker, first)
    assert result.erasure_required == 1 and result.erased == 0
    assert snapshot(f, artifact)["private_bytes"] == RAW
    # Even later clearance cannot resurrect this lineage or erase its rejection duty.
    newer(f, rights, rejected, clearance="cleared")
    assert reconcile(f, worker, first).replayed
    assert snapshot(f, artifact)["state"] == "erasure_required"
    result = reconcile(f, worker, erase_due=True)
    assert result.erased == 1 and result.erasure_required == 0
    after = snapshot(f, artifact)
    assert after["state"] == "erased" and after["private_bytes"] is None
    assert after["assessment_revision"] == 1 and after["size_bytes"] == len(RAW)
    assert reconcile(f, worker, erase_due=True).erased == 0
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='staging_erasure'"
                )
            )
            == 1
        )
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'staging_lifecycle_event')}"))
            == 2
        )


def test_rejected_history_is_not_lost_if_clearance_precedes_materializer(chain):
    f, rights, worker, _, pv, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    rejected = newer(f, rights, pv, clearance="rejected")
    newer(f, rights, rejected, clearance="cleared")
    assert reconcile(f, worker, erase_due=True).erased == 1
    assert snapshot(f, artifact)["private_bytes"] is None


def test_held_artifact_advances_to_erasure_required_on_rejection(chain):
    f, rights, worker, ctl, pv, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    newer(f, rights, ctl)
    assert reconcile(f, worker).held == 1
    newer(f, rights, pv, clearance="rejected")
    result = reconcile(f, worker, erase_due=True)
    assert result.erasure_required == result.erased == 1
    assert snapshot(f, artifact)["state"] == "erased"
    with f.engine.connect() as conn:
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'staging_lifecycle_event')}"))
            == 3
        )


def test_due_retention_erasure_releases_physical_capacity(chain):
    f, _, worker, _, _, acq = chain
    declare(f, len(RAW))
    with f.engine.connect() as conn:
        deadline = conn.scalar(text("SELECT statement_timestamp()")) + timedelta(seconds=2)
    artifact, _ = stage(f, worker, acq, retention_deadline=deadline)
    until = monotonic() + 4
    while monotonic() < until:
        with f.engine.connect() as conn:
            due = conn.scalar(text("SELECT statement_timestamp()>=:deadline"), {"deadline": deadline})
        if due:
            break
        sleep(0.05)
    assert due
    result = reconcile(f, worker, erase_due=True)
    assert result.erasure_required == result.erased == 1
    assert snapshot(f, artifact)["private_bytes"] is None
    assert not stage(f, worker, acq)[1]


def test_keyset_batches_are_bounded_and_retries_do_not_advance_cursor(chain):
    f, rights, worker, ctl, _, acq = chain
    declare(f)
    ids = [stage(f, worker, acq, UUID(int=i))[0] for i in range(1, 6)]
    newer(f, rights, ctl)
    req = request(f, limit=2)
    first = reconcile(f, worker, req)
    assert first.scanned == first.held == 2 and first.next_after == ids[1]
    replay = reconcile(f, worker, req)
    assert replay.replayed and replay.next_after == first.next_after and replay.held == first.held
    assert reconcile(f, worker, req.model_copy(update={"limit": 3})).status == "conflict"
    second = reconcile(f, worker, after=first.next_after, limit=2)
    assert second.scanned == second.held == 2 and second.next_after == ids[3]
    third = reconcile(f, worker, after=second.next_after, limit=2)
    assert third.scanned == third.held == 1 and third.next_after is None
    with f.engine.connect() as conn:
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'staging_lifecycle_event')}"))
            == 5
        )


@pytest.mark.parametrize(
    "kind", ["rights", "content", "release", "outsider", "revoked", "unbound", "no_limit"]
)
def test_lifecycle_requires_current_native_scoped_acquisition_authority(chain, operators, kind):
    f, _, worker, _, _, acq = chain
    _, create, _ = operators
    if kind != "no_limit":
        declare(f)
        stage(f, worker, acq)
    subject = worker
    if kind in {"rights", "content", "release"}:
        subject = create(kind)
    elif kind == "outsider":
        subject = create("acquire", assignments=((f.other_collection, "acquire"),))
    elif kind == "revoked":
        with f.engine.begin() as conn:
            revoke_operator(conn, f.ns, actor_id=worker.actor, reason="Synthetic revocation")
    elif kind == "unbound":
        with f.as_role("acquisition") as conn:
            assert AcquisitionLifecycle(conn, f.ns).reconcile(request(f)).status == "forbidden"
        return
    assert reconcile(f, subject).status == "forbidden"
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'lifecycle_receipt')}")) == 0


def test_receipt_replay_requires_same_actor_and_current_assignment(chain, operators):
    f, _, worker, _, _, _ = chain
    _, create, _ = operators
    declare(f)
    req = request(f)
    assert reconcile(f, worker, req).status == "applied"
    assert reconcile(f, create("acquire"), req).status == "forbidden"
    with f.engine.begin() as conn:
        conn.execute(
            text(f"DELETE FROM {f.name(conn, 'policy', 'assignment')} WHERE actor_id=:actor"),
            {"actor": worker.actor},
        )
    assert reconcile(f, worker, req).status == "forbidden"


def test_outer_rollback_removes_states_bytes_audit_and_receipt(chain):
    f, rights, worker, _, pv, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    newer(f, rights, pv, clearance="rejected")
    before = snapshot(f, artifact)
    req = request(f, erase_due=True)
    with worker.engine.connect() as conn:
        tx = conn.begin()
        assert AcquisitionLifecycle(conn, f.ns).reconcile(req).erased == 1
        tx.rollback()
    assert snapshot(f, artifact) == before
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'lifecycle_receipt')}")) == 0
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'staging_lifecycle_event')}"))
            == 0
        )
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind LIKE 'staging_%'"
                )
            )
            == 0
        )
    assert reconcile(f, worker, req).erased == 1


def test_native_sql_cannot_materialize_retarget_or_forge_events(chain):
    f, rights, worker, ctl, _, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    for state in ("held_pending_reassessment", "erasure_required"):
        with pytest.raises(DBAPIError), worker.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {f.name(conn, 'staging', 'staged_artifact')} SET state=:state WHERE id=:id"),
                {"state": state, "id": artifact},
            )
    newer(f, rights, ctl)
    assert reconcile(f, worker).held == 1
    with worker.engine.begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE {f.name(conn, 'staging', 'staged_artifact')} SET state='staged',assessment_revision=2 WHERE id=:id"
            ),
            {"id": artifact},
        )
        assert result.rowcount == 0  # Held rows are hidden by RLS as well as the state gates.
    assert snapshot(f, artifact)["state"] == "held_pending_reassessment"
    for table in ("lifecycle_receipt", "staging_lifecycle_event"):
        with pytest.raises(DBAPIError), worker.engine.begin() as conn:
            conn.execute(text(f"SELECT * FROM {f.name(conn, 'policy', table)}"))
        with pytest.raises(DBAPIError), f.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {f.name(conn, 'policy', table)}"))


def test_conservative_lifecycle_works_while_migration_is_frozen(chain):
    f, rights, worker, _, pv, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    newer(f, rights, pv, clearance="rejected")
    with f.engine.begin() as conn:
        freeze(conn, expected_epoch=0)
    assert reconcile(f, worker, erase_due=True).erased == 1
    assert snapshot(f, artifact)["private_bytes"] is None


@pytest.mark.parametrize("identical", [True, False])
def test_concurrent_batches_have_one_transition_and_atomic_retry(chain, identical):
    f, rights, worker, ctl, _, acq = chain
    declare(f)
    stage(f, worker, acq)
    newer(f, rights, ctl)
    req = request(f)
    barrier = Barrier(2)

    def run(job):
        with worker.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            barrier.wait(timeout=3)
            return AcquisitionLifecycle(conn, f.ns).reconcile(job)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, req), pool.submit(run, req if identical else request(f))]
        results = [future.result(timeout=8) for future in futures]
    assert all(result.status == "applied" for result in results)
    assert sorted(result.replayed for result in results) == ([False, True] if identical else [False, False])
    with f.engine.connect() as conn:
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'staging_lifecycle_event')}"))
            == 1
        )


def test_authorized_read_delays_due_erasure_until_caller_commit(chain):
    f, _, worker, _, _, acq = chain
    declare(f)
    with f.engine.connect() as conn:
        retention = conn.scalar(text("SELECT statement_timestamp()")) + timedelta(seconds=2)
    artifact, _ = stage(f, worker, acq, retention_deadline=retention)
    ready = Event()
    backend = []

    def materialize():
        with worker.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            backend.append(conn.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            return AcquisitionLifecycle(conn, f.ns).reconcile(request(f, erase_due=True))

    with ThreadPoolExecutor(max_workers=1) as pool, worker.engine.connect() as conn:
        tx = conn.begin()
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) == RAW
        until = monotonic() + 4
        with f.engine.connect() as probe:
            while monotonic() < until:
                due = probe.scalar(text("SELECT statement_timestamp()>=:deadline"), {"deadline": retention})
                if due:
                    break
                sleep(0.05)
        assert due
        future = pool.submit(materialize)
        assert ready.wait(timeout=2)
        until = monotonic() + 2
        with f.engine.connect() as probe:
            while monotonic() < until:
                waiting = probe.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                    {"pid": backend[0]},
                )
                if waiting:
                    break
                sleep(0.01)
        assert waiting
        tx.commit()
        assert future.result(timeout=6).erased == 1
    assert snapshot(f, artifact)["private_bytes"] is None


def test_cli_commits_bounded_counts_without_echoing_private_input(chain):
    f, rights, worker, ctl, _, acq = chain
    declare(f)
    stage(f, worker, acq)
    newer(f, rights, ctl)
    req = request(f).model_copy(update={"reason": "private://do-not-echo"})
    env = dict(
        os.environ, ALVARY_OPERATOR_DATABASE_URL=worker.engine.url.render_as_string(hide_password=False)
    )
    result = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--schema", f.ns.base, "--reconcile-staging"],
        input=req.model_dump_json(),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout)["held"] == 1
    assert (
        "do-not-echo" not in result.stdout
        and worker.password not in result.stdout
        and RAW.decode() not in result.stdout
    )


def test_populated_lifecycle_refuses_downgrade_and_empty_restores_m22_validators(chain):
    f, _, worker, _, _, _ = chain
    declare(f)
    assert reconcile(f, worker).status == "applied"
    with f.engine.connect() as conn:
        before = conn.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="requires forward repair"), f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0005")
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == before


def test_populated_m22_upgrade_preserves_bytes_and_function_rollback_copies(chain):
    f, _, worker, _, _, acq = chain
    # Before lifecycle use, 0006 can be rehearsed down with M2.2 state intact.
    declare(f)
    artifact, _ = stage(f, worker, acq)
    before = snapshot(f, artifact)
    with f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0005")
        defs = {
            fn: conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                {"fn": f.ns.schema("policy") + "." + fn + "()"},
            )
            for fn in ("validate_staging", "validate_synthetic_staging")
        }
    migrate(f.engine)
    assert snapshot(f, artifact) == before
    with f.engine.connect() as conn:
        for fn, definition in defs.items():
            assert (
                conn.scalar(
                    text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                    {"fn": f.ns.schema("policy") + "." + fn + "_m22()"},
                )
                == definition
            )
        guard = f.ns.role("guard")
        assert not conn.scalar(
            text("SELECT has_schema_privilege(:role,:schema,'CREATE')"),
            {"role": guard, "schema": f.ns.schema("policy")},
        )
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) == RAW


def test_failed_batch_savepoint_rolls_back_earlier_transitions_and_preserves_caller(chain):
    f, rights, worker, ctl, _, acq = chain
    declare(f)
    first, _ = stage(f, worker, acq, UUID(int=1))
    second, _ = stage(f, worker, acq, UUID(int=2))
    newer(f, rights, ctl)
    with f.engine.begin() as conn:
        # Explicit isolated-schema fault injection proves the batch boundary.
        schema = conn.dialect.identifier_preparer.quote_identifier(f.ns.base)
        conn.execute(
            text(f"""CREATE FUNCTION {schema}.fail_second_transition() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                IF NEW.id='00000000-0000-0000-0000-000000000002'::uuid THEN
                    RAISE EXCEPTION 'private-do-not-echo failure' USING ERRCODE='23514';
                END IF; RETURN NEW; END $$;
            CREATE TRIGGER zz_fault BEFORE UPDATE ON {f.name(conn, "staging", "staged_artifact")}
                FOR EACH ROW EXECUTE FUNCTION {schema}.fail_second_transition();""")
        )
    req = request(f)
    with worker.engine.begin() as conn:
        result = AcquisitionLifecycle(conn, f.ns).reconcile(req)
        assert result.status == "validation_failed"
        assert "private-do-not-echo" not in result.model_dump_json()
        assert conn.scalar(text("SELECT 1")) == 1
    for artifact in (first, second):
        assert snapshot(f, artifact)["state"] == "staged"
    with f.engine.begin() as conn:
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'staging_lifecycle_event')}"))
            == 0
        )
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'lifecycle_receipt')}")) == 0
        conn.execute(text(f"DROP TRIGGER zz_fault ON {f.name(conn, 'staging', 'staged_artifact')}"))
    assert reconcile(f, worker, req).held == 2


@pytest.mark.parametrize("change", ["privileged", "guard_membership", "guard_create"])
def test_upgrade_refuses_corrupted_role_boundary_before_creating_lifecycle_tables(chain, change):
    f, _, _, _, _, _ = chain
    with f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0005")
    with f.engine.connect() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        guard, acquisition = quote(f.ns.role("guard")), quote(f.ns.role("acquisition"))
        policy = quote(f.ns.schema("policy"))
        conn.rollback()
        tx = conn.begin()
        if change == "privileged":
            conn.execute(text(f"ALTER ROLE {acquisition} BYPASSRLS"))
        elif change == "guard_membership":
            conn.execute(text(f"GRANT {guard} TO {acquisition}"))
        else:
            conn.execute(text(f"GRANT CREATE ON SCHEMA {policy} TO {guard}"))
        with pytest.raises(RuntimeError, match="boundary review"):
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
        assert (
            conn.scalar(
                text("SELECT to_regclass(:table)"), {"table": f.ns.schema("policy") + ".lifecycle_receipt"}
            )
            is None
        )
        tx.rollback()
    migrate(f.engine)


def test_materialization_holds_parent_and_actor_locks_through_caller_commit(chain):
    f, rights, worker, ctl, _, acq = chain
    declare(f)
    stage(f, worker, acq)
    latest = newer(f, rights, ctl)
    # One subsequent reviewer and one maintenance revocation both must wait.
    from rights.commands import OperatorCommands

    ready = [Event(), Event()]
    backends = [None, None]

    def supersede():
        with rights.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            backends[0] = conn.scalar(text("SELECT pg_backend_pid()"))
            ready[0].set()
            cmd = latest.model_copy(update={"command_id": uuid4(), "expected_revision": 2})
            return OperatorCommands(conn, f.ns).apply(cmd)

    def revoke():
        with f.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            backends[1] = conn.scalar(text("SELECT pg_backend_pid()"))
            ready[1].set()
            revoke_operator(conn, f.ns, worker.actor, reason="Synthetic revocation race")

    with ThreadPoolExecutor(max_workers=2) as pool, worker.engine.connect() as conn:
        tx = conn.begin()
        assert AcquisitionLifecycle(conn, f.ns).reconcile(request(f)).held == 1
        futures = [pool.submit(supersede), pool.submit(revoke)]
        assert all(event.wait(timeout=2) for event in ready)
        until = monotonic() + 2
        with f.engine.connect() as probe:
            while monotonic() < until:
                waiting = [
                    probe.scalar(
                        text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                        {"pid": backend},
                    )
                    for backend in backends
                ]
                if all(waiting):
                    break
                sleep(0.01)
        assert all(waiting)
        tx.commit()
        assert futures[0].result(timeout=6).status == "applied"
        futures[1].result(timeout=6)
    assert reconcile(f, worker).status == "forbidden"


@pytest.mark.parametrize(
    "field,value", [("limit", 0), ("limit", 101), ("limit", True), ("erase_due", "true")]
)
def test_database_validates_bounds_even_if_python_validation_is_bypassed(chain, field, value):
    f, _, worker, _, _, _ = chain
    declare(f)
    raw = request(f).model_dump(mode="json")
    raw[field] = value
    with worker.engine.begin() as conn:
        fn = f.ns.qualified(conn, "policy", "reconcile_staging")
        with pytest.raises(DBAPIError) as failure, conn.begin_nested():
            conn.scalar(text(f"SELECT {fn}(CAST(:raw AS jsonb))"), {"raw": json.dumps(raw)})
        assert failure.value.orig.sqlstate[:2] == "22"
        assert conn.scalar(text("SELECT 1")) == 1


def test_held_lineage_never_resumes_when_review_actor_is_reactivated(chain):
    f, rights, worker, _, _, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    with f.engine.begin() as conn:
        revoke_operator(conn, f.ns, rights.actor, reason="Synthetic reviewer revocation")
    assert reconcile(f, worker).held == 1
    with f.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {f.name(conn, 'policy', 'actor')} SET active=true,registration_reason='Synthetic reactivation test' WHERE id=:id"
            ),
            {"id": rights.actor},
        )
    assert reconcile(f, worker).held == 0
    assert snapshot(f, artifact)["state"] == "held_pending_reassessment"
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) is None
    # A retry cannot change the held state even if the same pins become current.
    assert stage(f, worker, acq, artifact)[1]
    assert snapshot(f, artifact)["state"] == "held_pending_reassessment"
    fresh, _ = stage(f, worker, acq)
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(fresh, purpose=PURPOSE) == RAW


def test_unqualified_legacy_artifact_can_be_held_without_manufacturing_qualification(operators):
    f, create, _ = operators
    legacy = f.assessment()
    artifact = f.stage(legacy)["id"]
    declare(f)
    before = snapshot(f, artifact)
    assert reconcile(f, create("acquire")).held == 1
    after = snapshot(f, artifact)
    assert after.pop("state") == "held_pending_reassessment"
    before.pop("state")
    assert after == before and after["purpose"] is None and after["acquired_by"] is None
