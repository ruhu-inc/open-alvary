"""Native-session original synthetic private proposals, not extraction or publication."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
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
from test_postgres_lifecycle import newer
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from rights.acquisition import SyntheticAcquisition
from rights.assembly import SyntheticAssembly, parse_assembly_command
from rights.lifecycle import AcquisitionLifecycle
from rights.operators import revoke_operator
from schema.assembly import CandidateInput, SnapshotCommand, SyntheticRunCommand
from schema.operator import LifecycleRequest

pytestmark = pytest.mark.postgres


def candidate(index=1, *, region=None, **changes):
    fields = dict(
        id=UUID(int=index),
        adapter_local_id=f"local_{index}",
        region_key=region or f"region_{index}",
        artifact_class="native_text",
        block_type="paragraph",
        text=f" Original synthetic candidate {index}.\n",
        unavailable_page_reason="Original synthetic fixture has no physical page",
        unavailable_geometry_reason="Original synthetic fixture has no geometry",
    )
    fields.update(changes)
    return CandidateInput(**fields)


def run_command(f, artifact, assessment, *, candidates=None, **changes):
    return SyntheticRunCommand(
        command_id=uuid4(),
        collection_id=f.collection,
        reason="Original synthetic proposal only",
        action="record_synthetic_run",
        run_id=uuid4(),
        artifact_id=artifact,
        assessment_id=assessment.payload.id,
        assessment_revision=assessment.expected_revision + 1,
        artifact_hash=sha256(RAW).hexdigest(),
        purpose=PURPOSE,
        profile_key="synthetic_fixture_1",
        candidates=candidates or (candidate(),),
        **changes,
    )


def snapshot_command(
    f, run, *, revision=0, parent_hash=None, selected=None, order=None, resolutions=(), snapshot_id=None
):
    selected = selected or [(c.region_key, c.id) for c in run.candidates]
    return SnapshotCommand.model_validate(
        dict(
            command_id=uuid4(),
            collection_id=f.collection,
            reason="Original synthetic editing",
            action="record_snapshot",
            snapshot_id=snapshot_id or uuid4(),
            run_id=run.run_id,
            purpose=PURPOSE,
            expected_revision=revision,
            parent_hash=parent_hash,
            payload=dict(
                selections=[dict(region_key=r, candidate_id=i) for r, i in selected],
                order=order or [i for _, i in selected],
                resolutions=resolutions,
                reason="Original synthetic selection, not approval",
            ),
        )
    )


def apply_assembly(f, subject, cmd):
    with subject.engine.begin() as conn:
        return SyntheticAssembly(conn, f.ns).apply(cmd)


def reconcile(f, worker, **changes):
    from schema.assembly import AssemblyReconcileCommand

    return apply_assembly(
        f,
        worker,
        AssemblyReconcileCommand(
            command_id=uuid4(),
            collection_id=f.collection,
            reason="Original synthetic derivative reconciliation",
            action="reconcile_assembly",
            **changes,
        ),
    )


@pytest.fixture
def assembly(chain, operators):
    f, rights, worker, ctl, pv, acq = chain
    _, create, _ = operators
    acq = newer(f, rights, acq, derive="allow")
    editor = create("content", assignments=((f.collection, "content"), (f.collection, "acquire")))
    declare(f)
    artifact, _ = stage(f, worker, acq)
    run = run_command(f, artifact, acq)
    result = apply_assembly(f, worker, run)
    assert result.status == "applied", result
    return f, rights, worker, editor, ctl, pv, acq, artifact, run


def records(f, table):
    with f.engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(f"SELECT * FROM {f.name(conn, 'staging', table)} ORDER BY 1,2")
            ).mappings()
        ]


def test_candidates_are_literal_private_immutable_and_exactly_pinned(assembly):
    f, _, worker, _, _, _, acq, artifact, run = assembly
    with worker.engine.begin() as conn:
        page = SyntheticAssembly(conn, f.ns).candidates(run.run_id, purpose=PURPOSE)
        assert page.items[0].payload.text == run.candidates[0].text
        item = page.items[0]
        assert (
            item.artifact_id == artifact
            and item.assessment_id == acq.payload.id
            and item.assessment_revision == 2
        )
        assert item.artifact_hash == sha256(RAW).hexdigest()
        assert item.text_hash == sha256(item.payload.text.encode()).hexdigest()
        assert not SyntheticAssembly(conn, f.ns).candidates(run.run_id, purpose="wrong-purpose").items
    assert apply_assembly(f, worker, run).replayed
    with f.engine.connect() as conn:
        for table in ("versions", "identity_observations"):
            assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    with pytest.raises(DBAPIError), worker.engine.begin() as conn:
        conn.execute(text(f"SELECT payload FROM {f.name(conn, 'staging', 'adapter_candidate')}"))
    with pytest.raises(DBAPIError), f.engine.begin() as conn:
        conn.execute(text(f"UPDATE {f.name(conn, 'staging', 'adapter_candidate')} SET payload='{{}}'"))


def test_snapshot_revisions_pin_parent_hash_and_keep_each_previous_proposal(assembly):
    f, _, worker, editor, _, _, _, _, run = assembly
    cmd = snapshot_command(f, run)
    first = apply_assembly(f, editor, cmd)
    assert first.status == "applied" and first.revision == 1
    second_cmd = snapshot_command(
        f, run, revision=1, parent_hash=first.output_hash, snapshot_id=cmd.snapshot_id
    )
    second = apply_assembly(f, editor, second_cmd)
    assert second.status == "applied" and second.revision == 2
    third_cmd = snapshot_command(
        f, run, revision=2, parent_hash=second.output_hash, snapshot_id=cmd.snapshot_id
    )
    assert apply_assembly(f, editor, third_cmd).revision == 3
    assert apply_assembly(f, editor, cmd).replayed
    with worker.engine.begin() as conn:
        service = SyntheticAssembly(conn, f.ns)
        for revision in (1, 2, 3):
            view = service.snapshot(cmd.snapshot_id, revision, purpose=PURPOSE)
            assert view and view.payload.order == (run.candidates[0].id,)
        assert service.snapshot(cmd.snapshot_id, 1, purpose="wrong") is None
    assert len(records(f, "staging_snapshot")) == 3
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM versions")) == 0
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='staging_snapshot'"
                )
            )
            == 3
        )


@pytest.mark.parametrize("change", ["derive_unknown", "hash", "assessment", "purpose", "unqualified"])
def test_run_requires_exact_current_qualified_derive_and_retain_authority(chain, change):
    f, rights, worker, _, _, acq = chain
    declare(f)
    if change != "derive_unknown":
        acq = newer(f, rights, acq, derive="allow")
    if change == "unqualified":
        legacy = f.assessment()
        artifact = f.stage(legacy)["id"]
    else:
        artifact, _ = stage(f, worker, acq)
    cmd = run_command(f, artifact, acq)
    if change == "hash":
        cmd = cmd.model_copy(update={"artifact_hash": "f" * 64})
    elif change == "assessment":
        cmd = cmd.model_copy(update={"assessment_id": uuid4()})
    elif change == "purpose":
        cmd = cmd.model_copy(update={"purpose": "wrong-purpose"})
    result = apply_assembly(f, worker, cmd)
    assert result.status == "forbidden"
    assert records(f, "adapter_candidate") == [] and records(f, "synthetic_run") == []


@pytest.mark.parametrize("kind", ["rights", "content", "release", "outsider", "revoked"])
def test_native_scope_and_role_denials_do_not_leak_or_record_candidates(assembly, operators, kind):
    f, _, worker, _, _, _, _, _, run = assembly
    _, create, _ = operators
    if kind == "outsider":
        subject = create("acquire", assignments=((f.other_collection, "acquire"),))
    elif kind == "revoked":
        subject = worker
        with f.engine.begin() as conn:
            revoke_operator(conn, f.ns, worker.actor, reason="Original synthetic revocation")
    else:
        subject = create(kind)
    result = apply_assembly(f, subject, run.model_copy(update={"command_id": uuid4()}))
    assert result.status == "forbidden"
    with subject.engine.begin() as conn:
        service = SyntheticAssembly(conn, f.ns)
        assert not service.candidates(run.run_id, purpose=PURPOSE).items
    assert len(records(f, "adapter_candidate")) == 1


@pytest.mark.parametrize("kind", ["worker", "content_only"])
def test_snapshot_editor_requires_both_content_and_private_acquisition_assignment(assembly, operators, kind):
    f, _, worker, _, _, _, _, _, run = assembly
    _, create, _ = operators
    subject = worker if kind == "worker" else create("content")
    assert apply_assembly(f, subject, snapshot_command(f, run)).status == "forbidden"
    assert records(f, "staging_snapshot") == []


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_local",
        "missing_parent",
        "hierarchy_cycle",
        "duplicate_id",
        "unknown_field",
        "null_direction",
        "invalid_cell",
    ],
)
def test_database_rejects_malformed_candidate_lineage_and_payload_without_partial_run(assembly, change):
    f, _, worker, _, _, _, acq, artifact, _ = assembly
    first = candidate(10)
    second = candidate(11)
    data = run_command(f, artifact, acq, candidates=(first, second)).model_dump(mode="json")
    data["profile_key"] = "invalid_fixture"
    if change == "duplicate_local":
        data["candidates"][1]["adapter_local_id"] = data["candidates"][0]["adapter_local_id"]
    elif change == "missing_parent":
        data["candidates"][0]["parent_local_id"] = "absent"
    elif change == "hierarchy_cycle":
        data["candidates"][0]["parent_local_id"] = data["candidates"][1]["adapter_local_id"]
        data["candidates"][1]["parent_local_id"] = data["candidates"][0]["adapter_local_id"]
    elif change == "duplicate_id":
        data["candidates"][1]["id"] = data["candidates"][0]["id"]
    elif change == "unknown_field":
        data["candidates"][0]["confidence"] = 0.99
    elif change == "null_direction":
        data["candidates"][0]["direction"] = None
    else:
        data["candidates"][0].update(
            block_type="table_cell",
            cell=dict(table_local_id="table1", row=99, column=0, row_span=2, column_span=1),
        )
    with worker.engine.begin() as conn:
        fn = f.ns.qualified(conn, "policy", "apply_assembly_command")
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.scalar(text(f"SELECT {fn}(CAST(:raw AS jsonb))"), {"raw": json.dumps(data)})
        assert conn.scalar(text("SELECT 1")) == 1
    assert len(records(f, "synthetic_run")) == 1 and len(records(f, "adapter_candidate")) == 1


def test_alternative_runs_and_explicit_overlap_resolution_preserve_all_candidates(assembly):
    f, _, worker, editor, _, _, acq, artifact, oldrun = assembly
    alternatives = (
        candidate(10, region="r1"),
        candidate(11, region="r1", artifact_class="ocr_transcription"),
        candidate(12, region="r2"),
    )
    data = run_command(f, artifact, acq, candidates=alternatives).model_dump(mode="json")
    data["profile_key"] = "alternative_fixture"
    run = parse_assembly_command(json.dumps(data).encode())
    assert apply_assembly(f, worker, run).status == "applied"
    selected = [("r1", alternatives[0].id), ("r2", alternatives[2].id)]
    unresolved = snapshot_command(f, run, selected=selected)
    assert apply_assembly(f, editor, unresolved).status == "validation_failed"
    resolution = dict(
        region_key="r1",
        candidate_id=alternatives[0].id,
        rejected_candidate_ids=[alternatives[1].id],
        reason="Original synthetic comparison selects one alternative",
    )
    cmd = snapshot_command(f, run, selected=selected, resolutions=[resolution])
    assert apply_assembly(f, editor, cmd).status == "applied"
    assert len(records(f, "adapter_candidate")) == 4 and len(records(f, "snapshot_resolution")) == 1
    with worker.engine.begin() as conn:
        assert len(SyntheticAssembly(conn, f.ns).candidates(oldrun.run_id, purpose=PURPOSE).items) == 1


@pytest.mark.parametrize(
    "change",
    [
        "parent_hash",
        "stale_revision",
        "foreign_run",
        "missing_selection",
        "unknown_candidate",
        "duplicate_order",
    ],
)
def test_snapshot_rejects_bad_parents_cross_run_and_incomplete_or_repeated_order(assembly, change):
    f, _, worker, editor, _, _, acq, artifact, run = assembly
    cmd = snapshot_command(f, run)
    first = apply_assembly(f, editor, cmd)
    data = snapshot_command(
        f, run, revision=1, parent_hash=first.output_hash, snapshot_id=cmd.snapshot_id
    ).model_dump(mode="json")
    if change == "parent_hash":
        data["parent_hash"] = "f" * 64
    elif change == "stale_revision":
        data.update(expected_revision=0, parent_hash=None)
    elif change == "foreign_run":
        second = run_command(f, artifact, acq, candidates=(candidate(10),)).model_copy(
            update={"profile_key": "second"}
        )
        assert apply_assembly(f, worker, second).status == "applied"
        data["run_id"] = str(second.run_id)
    elif change == "missing_selection":
        data["payload"]["selections"] = []
    elif change == "unknown_candidate":
        data["payload"]["selections"][0]["candidate_id"] = str(uuid4())
    else:
        data["payload"]["order"] *= 2
    with worker.engine.begin() as conn:
        # Worker has no content authority, so the editor executes the raw invalid envelope below.
        assert conn.scalar(text("SELECT 1")) == 1
    with editor.engine.begin() as conn:
        fn = f.ns.qualified(conn, "policy", "apply_assembly_command")
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.scalar(text(f"SELECT {fn}(CAST(:raw AS jsonb))"), {"raw": json.dumps(data)})
    assert len(records(f, "staging_snapshot")) == 1


@pytest.mark.parametrize("parent", ["controller", "privacy", "acquisition"])
def test_parent_supersession_hides_payloads_before_reconciliation_and_inherits_holds(assembly, parent):
    f, rights, worker, editor, ctl, pv, acq, _, run = assembly
    snap = snapshot_command(f, run)
    assert apply_assembly(f, editor, snap).status == "applied"
    newer(f, rights, {"controller": ctl, "privacy": pv, "acquisition": acq}[parent])
    with worker.engine.begin() as conn:
        service = SyntheticAssembly(conn, f.ns)
        assert (
            not service.candidates(run.run_id, purpose=PURPOSE).items
            and service.snapshot(snap.snapshot_id, 1, purpose=PURPOSE) is None
        )
        req = LifecycleRequest(
            command_id=uuid4(), collection_id=f.collection, reason="Original synthetic raw lifecycle"
        )
        assert AcquisitionLifecycle(conn, f.ns).reconcile(req).held == 1
    for table in ("adapter_candidate", "staging_snapshot"):
        row = records(f, table)[0]
        assert row["state"] == "held" and row["payload"] is not None
    assert apply_assembly(f, worker, run).status == "forbidden"


def test_derivative_reconciliation_keeps_raw_state_and_never_resumes_held_payloads(assembly):
    f, rights, worker, editor, _, _, acq, artifact, run = assembly
    snap = snapshot_command(f, run)
    assert apply_assembly(f, editor, snap).status == "applied"
    # Revoke the review actor: both gates close; the dedicated derivative operator
    # never changes raw state, which stays staged until its own reconciliation.
    with f.engine.begin() as conn:
        revoke_operator(conn, f.ns, rights.actor, reason="Synthetic reviewer revocation")
    result = reconcile(f, worker)
    assert result.held == 2 and result.erased == 0 and result.scanned == 1
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT state FROM {f.name(conn, 'staging', 'staged_artifact')} WHERE id=:id"),
                {"id": artifact},
            )
            == "staged"
        )
    assert reconcile(f, worker).held == 0
    with f.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {f.name(conn, 'policy', 'actor')} SET active=true,registration_reason='Synthetic reactivation' WHERE id=:id"
            ),
            {"id": rights.actor},
        )
    with worker.engine.begin() as conn:
        assert not SyntheticAssembly(conn, f.ns).candidates(run.run_id, purpose=PURPOSE).items
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) == RAW


def test_rejection_and_raw_erasure_delete_all_derived_payloads_and_preserve_hash_lineage(assembly):
    f, rights, worker, editor, _, pv, _, artifact, run = assembly
    snap = snapshot_command(f, run)
    assert apply_assembly(f, editor, snap).status == "applied"
    before = {table: records(f, table)[0] for table in ("adapter_candidate", "staging_snapshot")}
    newer(f, rights, pv, clearance="rejected")
    assert reconcile(f, worker).erasure_required == 2
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    for table in before:
        row = records(f, table)[0]
        assert row.pop("state") == "erased" and row.pop("payload") is None
        before[table].pop("state")
        before[table].pop("payload")
        assert row == before[table]
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT sum(changed_count) FROM {f.name(conn, 'policy', 'assembly_lifecycle_event')} WHERE target='erased'"
                )
            )
            == 2
        )
    assert reconcile(f, worker, erase_due=True).erased == 0


def test_assembly_can_erase_due_payloads_and_later_raw_cleanup_is_idempotent(assembly):
    f, rights, worker, editor, _, pv, _, artifact, run = assembly
    assert apply_assembly(f, editor, snapshot_command(f, run)).status == "applied"
    newer(f, rights, pv, clearance="rejected")
    result = reconcile(f, worker, erase_due=True)
    assert result.erasure_required == result.erased == 2
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    assert all(r["payload"] is None for r in records(f, "adapter_candidate") + records(f, "staging_snapshot"))


def test_outer_rollback_and_safe_failed_command_remove_all_domain_audit_and_receipt(assembly):
    f, _, worker, editor, _, _, _, _, run = assembly
    cmd = snapshot_command(f, run)
    with editor.engine.connect() as conn:
        tx = conn.begin()
        assert SyntheticAssembly(conn, f.ns).apply(cmd).status == "applied"
        tx.rollback()
    assert records(f, "staging_snapshot") == [] and records(f, "snapshot_head") == []
    assert apply_assembly(f, editor, cmd).status == "applied"
    assert (
        apply_assembly(f, editor, cmd.model_copy(update={"reason": "private-do-not-echo changed"})).status
        == "conflict"
    )
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'assembly_receipt')}")) == 2


def test_erasure_releases_raw_and_derived_live_byte_capacity(assembly):
    f, _, worker, _, _, _, acq, artifact, run = assembly
    with f.engine.connect() as conn:
        total = conn.scalar(
            text(f"SELECT {f.ns.qualified(conn, 'policy', 'staging_live_bytes')}(:cid)"),
            {"cid": f.collection},
        )
    assert total > len(RAW)
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT {f.ns.qualified(conn, 'policy', 'staging_live_bytes')}(:cid)"),
                {"cid": f.collection},
            )
            == 0
        )


@pytest.mark.parametrize("identical", [True, False])
def test_concurrent_snapshot_requests_commit_one_revision_and_identical_retry(assembly, identical):
    f, _, _, editor, _, _, _, _, run = assembly
    cmd = snapshot_command(f, run)
    barrier = Barrier(2)

    def call(job):
        with editor.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            barrier.wait(timeout=3)
            return SyntheticAssembly(conn, f.ns).apply(job)

    other = cmd if identical else cmd.model_copy(update={"command_id": uuid4()})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, cmd), pool.submit(call, other)]
        results = [future.result(timeout=8) for future in futures]
    if identical:
        assert all(r.status == "applied" for r in results) and sorted(r.replayed for r in results) == [
            False,
            True,
        ]
    else:
        assert sorted(r.status for r in results) == ["applied", "conflict"]
    assert len(records(f, "staging_snapshot")) == 1


def test_private_read_holds_artifact_lock_until_commit_before_erasure(assembly):
    f, _, worker, _, _, _, _, artifact, run = assembly
    ready = Event()
    backend = []

    def erase():
        with worker.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            backend.append(conn.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            return SyntheticAcquisition(conn, f.ns).erase(artifact)

    with ThreadPoolExecutor(max_workers=1) as pool, worker.engine.connect() as conn:
        tx = conn.begin()
        assert SyntheticAssembly(conn, f.ns).candidates(run.run_id, purpose=PURPOSE).items
        future = pool.submit(erase)
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
        assert future.result(timeout=6)
    assert records(f, "adapter_candidate")[0]["payload"] is None


def test_assembly_cli_commits_safe_result_without_private_text_or_credentials(assembly):
    f, _, worker, editor, _, _, _, _, run = assembly
    cmd = snapshot_command(f, run)
    env = dict(
        os.environ, ALVARY_OPERATOR_DATABASE_URL=editor.engine.url.render_as_string(hide_password=False)
    )
    result = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--assembly", "--schema", f.ns.base],
        input=cmd.model_dump_json(),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "applied" and not result.stderr
    assert run.candidates[0].text not in result.stdout and editor.password not in result.stdout


def test_populated_assembly_refuses_downgrade(assembly):
    f, *_ = assembly
    with f.engine.connect() as conn:
        before = conn.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="requires forward repair"), f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0006")
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == before


def test_populated_m23_upgrade_and_empty_rollback_preserve_raw_records_and_validator(operators):
    f, create, _ = operators
    with f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0006")
        original = conn.scalar(
            text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
            {"fn": f.ns.schema("policy") + ".validate_synthetic_staging()"},
        )
    legacy = f.assessment()
    f.stage(legacy)
    before = records(f, "staged_artifact")
    migrate(f.engine)
    assert records(f, "staged_artifact") == before
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                {"fn": f.ns.schema("policy") + ".validate_synthetic_staging_m23()"},
            )
            == original
        )
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'staging', 'synthetic_run')}")) == 0


def test_expired_retention_denies_reads_and_erases_payloads_through_raw_reconciliation(chain, operators):
    f, rights, worker, _, _, acq = chain
    _, create, _ = operators
    acq = newer(f, rights, acq, derive="allow")
    editor = create("content", assignments=((f.collection, "content"), (f.collection, "acquire")))
    declare(f)
    with f.engine.connect() as conn:
        retention = conn.scalar(text("SELECT statement_timestamp()")) + timedelta(seconds=3)
    artifact, _ = stage(f, worker, acq, retention_deadline=retention)
    run = run_command(f, artifact, acq)
    assert apply_assembly(f, worker, run).status == "applied"
    snap = snapshot_command(f, run)
    assert apply_assembly(f, editor, snap).status == "applied"
    until = monotonic() + 5
    while monotonic() < until:
        with f.engine.connect() as conn:
            due = conn.scalar(text("SELECT statement_timestamp()>=:deadline"), {"deadline": retention})
        if due:
            break
        sleep(0.05)
    assert due
    with worker.engine.begin() as conn:
        service = SyntheticAssembly(conn, f.ns)
        assert not service.candidates(run.run_id, purpose=PURPOSE).items
        assert service.snapshot(snap.snapshot_id, 1, purpose=PURPOSE) is None
        raw = AcquisitionLifecycle(conn, f.ns).reconcile(
            LifecycleRequest(
                command_id=uuid4(),
                collection_id=f.collection,
                reason="Synthetic due retention",
                erase_due=True,
            )
        )
        assert raw.erased == 1
    assert all(r["payload"] is None for r in records(f, "adapter_candidate") + records(f, "staging_snapshot"))


def test_declared_capacity_denies_both_more_raw_bytes_and_more_derivative_payloads(chain):
    f, rights, worker, _, _, acq = chain
    acq = newer(f, rights, acq, derive="allow")
    item = candidate()
    with f.engine.connect() as conn:
        size = conn.scalar(
            text("SELECT octet_length(CAST(:body AS jsonb)::text)"), {"body": item.model_dump_json()}
        )
    declare(f, len(RAW) + size)
    artifact, _ = stage(f, worker, acq)
    run = run_command(f, artifact, acq, candidates=(item,))
    assert apply_assembly(f, worker, run).status == "applied"
    from rights.acquisition import AcquisitionError

    with pytest.raises(AcquisitionError) as denied:
        stage(f, worker, acq)
    assert denied.value.status == "capacity_exceeded"
    second = run_command(f, artifact, acq, candidates=(candidate(10),)).model_copy(
        update={"profile_key": "second_profile"}
    )
    assert apply_assembly(f, worker, second).status == "capacity_exceeded"
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    assert not stage(f, worker, acq)[1]


def test_concurrent_cross_artifact_derivative_capacity_has_one_allocation(chain):
    f, rights, worker, _, _, acq = chain
    acq = newer(f, rights, acq, derive="allow")
    items = [candidate(10), candidate(11)]
    with f.engine.connect() as conn:
        sizes = [
            conn.scalar(
                text("SELECT octet_length(CAST(:body AS jsonb)::text)"), {"body": item.model_dump_json()}
            )
            for item in items
        ]
    assert sizes[0] == sizes[1]
    declare(f, 2 * len(RAW) + sizes[0])
    artifacts = [stage(f, worker, acq)[0] for _ in range(2)]
    jobs = [
        run_command(f, artifact, acq, candidates=(item,))
        for artifact, item in zip(artifacts, items, strict=True)
    ]
    barrier = Barrier(2)

    def call(job):
        with worker.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            barrier.wait(timeout=3)
            return SyntheticAssembly(conn, f.ns).apply(job)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, job) for job in jobs]
        assert sorted(future.result(timeout=8).status for future in futures) == [
            "applied",
            "capacity_exceeded",
        ]
    assert len(records(f, "synthetic_run")) == 1


def test_family_record_and_run_bounds_keep_parent_cleanup_bounded(assembly):
    f, _, worker, editor, _, _, acq, artifact, run = assembly
    for start, count in ((10, 50), (100, 50), (200, 27)):
        cmd = run_command(
            f, artifact, acq, candidates=tuple(candidate(i) for i in range(start, start + count))
        ).model_copy(update={"profile_key": f"profile_{start}"})
        assert apply_assembly(f, worker, cmd).status == "applied"
    assert len(records(f, "adapter_candidate")) == 128
    assert apply_assembly(f, editor, snapshot_command(f, run)).status == "capacity_exceeded"
    fifth = run_command(f, artifact, acq, candidates=(candidate(300),)).model_copy(
        update={"profile_key": "fifth"}
    )
    assert apply_assembly(f, worker, fifth).status == "capacity_exceeded"
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    assert all(r["payload"] is None for r in records(f, "adapter_candidate"))
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT changed_count FROM {f.name(conn, 'policy', 'assembly_lifecycle_event')} WHERE target='erased'"
                )
            )
            == 128
        )


def test_snapshot_revision_bound_and_no_head_rewind(assembly):
    f, _, _, editor, _, _, _, _, run = assembly
    cmd = snapshot_command(f, run)
    parent = None
    for revision in range(32):
        cmd = snapshot_command(f, run, revision=revision, parent_hash=parent, snapshot_id=cmd.snapshot_id)
        result = apply_assembly(f, editor, cmd)
        assert result.status == "applied" and result.revision == revision + 1
        parent = result.output_hash
    invalid = cmd.model_copy(update={"command_id": uuid4(), "expected_revision": 32, "parent_hash": parent})
    assert apply_assembly(f, editor, invalid).status == "validation_failed"
    with pytest.raises(DBAPIError), f.engine.begin() as conn:
        conn.execute(text(f"UPDATE {f.name(conn, 'staging', 'snapshot_head')} SET revision=1"))
    assert len(records(f, "staging_snapshot")) == 32


def test_parent_erasure_fault_rolls_back_raw_and_derived_states_payloads_and_audits(assembly):
    f, _, worker, editor, _, _, _, artifact, run = assembly
    assert apply_assembly(f, editor, snapshot_command(f, run)).status == "applied"
    before = {
        table: records(f, table) for table in ("staged_artifact", "adapter_candidate", "staging_snapshot")
    }
    with f.engine.begin() as conn:
        schema = conn.dialect.identifier_preparer.quote_identifier(f.ns.base)
        conn.execute(
            text(f"""CREATE FUNCTION {schema}.fail_snapshot_erasure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'private-do-not-echo test failure' USING ERRCODE='23514'; END $$;
            CREATE TRIGGER zz_fault BEFORE UPDATE ON {f.name(conn, "staging", "staging_snapshot")}
                FOR EACH ROW EXECUTE FUNCTION {schema}.fail_snapshot_erasure();""")
        )
    from rights.acquisition import AcquisitionError

    with worker.engine.begin() as conn:
        with pytest.raises(AcquisitionError) as failure:
            SyntheticAcquisition(conn, f.ns).erase(artifact)
        assert failure.value.status == "validation_failed" and "private-do-not-echo" not in str(failure.value)
        assert conn.scalar(text("SELECT 1")) == 1
    for table, rows in before.items():
        assert records(f, table) == rows
    with f.engine.begin() as conn:
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'assembly_lifecycle_event')}"))
            == 0
        )
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='staging_erasure'"
                )
            )
            == 0
        )
        conn.execute(text(f"DROP TRIGGER zz_fault ON {f.name(conn, 'staging', 'staging_snapshot')}"))
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)


def test_candidate_write_blocks_parent_supersession_through_caller_commit(chain):
    f, rights, worker, ctl, _, acq = chain
    acq = newer(f, rights, acq, derive="allow")
    declare(f)
    artifact, _ = stage(f, worker, acq)
    run = run_command(f, artifact, acq)
    ready = Event()
    backend = []
    from rights.commands import OperatorCommands

    def supersede():
        with rights.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            backend.append(conn.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            return OperatorCommands(conn, f.ns).apply(
                ctl.model_copy(update={"command_id": uuid4(), "expected_revision": 1})
            )

    with ThreadPoolExecutor(max_workers=1) as pool, worker.engine.connect() as conn:
        tx = conn.begin()
        assert SyntheticAssembly(conn, f.ns).apply(run).status == "applied"
        future = pool.submit(supersede)
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
        assert future.result(timeout=6).status == "applied"
    with worker.engine.begin() as conn:
        assert not SyntheticAssembly(conn, f.ns).candidates(run.run_id, purpose=PURPOSE).items


def test_candidate_pages_and_derivative_reconciliation_use_bounded_uuid_cursors(assembly):
    f, rights, worker, _, ctl, _, acq, artifact, _ = assembly
    cmd = run_command(f, artifact, acq, candidates=tuple(candidate(i) for i in range(10, 15))).model_copy(
        update={"profile_key": "pagination"}
    )
    assert apply_assembly(f, worker, cmd).status == "applied"
    with worker.engine.begin() as conn:
        service = SyntheticAssembly(conn, f.ns)
        first = service.candidates(cmd.run_id, purpose=PURPOSE, limit=2)
        assert len(first.items) == 2 and first.next_after == UUID(int=11)
        second = service.candidates(cmd.run_id, purpose=PURPOSE, limit=2, after=first.next_after)
        third = service.candidates(cmd.run_id, purpose=PURPOSE, limit=2, after=second.next_after)
        assert [item.payload.id for item in first.items + second.items + third.items] == [
            UUID(int=i) for i in range(10, 15)
        ]
        assert third.next_after is None
    newer(f, rights, ctl)
    result = reconcile(f, worker, limit=1)
    assert result.scanned == 1 and result.held == 6 and result.next_after == artifact
    assert reconcile(f, worker, after=artifact, limit=1).scanned == 0


def test_negative_derivative_cleanup_remains_available_while_migration_is_frozen(assembly):
    from api.identity import freeze

    f, rights, worker, _, _, pv, _, _, run = assembly
    newer(f, rights, pv, clearance="rejected")
    with f.engine.begin() as conn:
        freeze(conn, expected_epoch=0)
    assert apply_assembly(f, worker, run.model_copy(update={"command_id": uuid4()})).status in (
        "forbidden",
        "unavailable",
    )
    assert reconcile(f, worker, erase_due=True).erased == 1


@pytest.mark.parametrize("corruption", ["guard_create", "guard_inheritance", "function_owner"])
def test_upgrade_refuses_corrupted_existing_authority_before_assembly_ddl(operators, corruption):
    f, _, _ = operators
    with f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0006")
    with f.engine.connect() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        guard, worker = quote(f.ns.role("guard")), quote(f.ns.role("acquisition"))
        policy = quote(f.ns.schema("policy"))
        owner = quote(conn.scalar(text("SELECT current_user")))
        conn.rollback()
        tx = conn.begin()
        if corruption == "guard_create":
            conn.execute(text(f"GRANT CREATE ON SCHEMA {policy} TO {guard}"))
        elif corruption == "guard_inheritance":
            conn.execute(text(f"GRANT {guard} TO {worker}"))
        else:
            conn.execute(text(f"ALTER FUNCTION {policy}.erase_staged(uuid) OWNER TO {owner}"))
        with pytest.raises(RuntimeError, match="boundary review"):
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
        assert (
            conn.scalar(
                text("SELECT to_regclass(:table)"), {"table": f.ns.schema("staging") + ".synthetic_run"}
            )
            is None
        )
        tx.rollback()
    migrate(f.engine)


def test_complete_snapshot_hashes_change_with_lineage_and_erasure_preserves_parent_chain(assembly):
    f, _, worker, editor, _, _, _, artifact, run = assembly
    cmd = snapshot_command(f, run)
    results = []
    parent = None
    for revision in range(3):
        cmd = snapshot_command(f, run, revision=revision, parent_hash=parent, snapshot_id=cmd.snapshot_id)
        result = apply_assembly(f, editor, cmd)
        assert result.status == "applied"
        results.append(result.output_hash)
        parent = result.output_hash
    assert len(set(results)) == 3
    before = records(f, "staging_snapshot")
    assert len({row["payload_hash"] for row in before}) == 1
    assert [row["snapshot_hash"] for row in before] == results
    with worker.engine.begin() as conn:
        view = SyntheticAssembly(conn, f.ns).snapshot(cmd.snapshot_id, 3, purpose=PURPOSE)
        assert view.snapshot_hash == results[2]
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    after = records(f, "staging_snapshot")
    for old, new in zip(before, after, strict=True):
        assert new.pop("payload") is None and new.pop("state") == "erased"
        old.pop("payload")
        old.pop("state")
        assert old == new


def test_database_integrity_checks_reject_forged_payload_hash_and_snapshot_parent(assembly):
    f, _, _, editor, _, _, _, _, run = assembly
    cmd = snapshot_command(f, run)
    assert apply_assembly(f, editor, cmd).status == "applied"
    candidate_row = records(f, "adapter_candidate")[0]
    snapshot_row = records(f, "staging_snapshot")[0]
    candidate_row.update(id=uuid4(), adapter_local_id="forged_local", payload_hash="f" * 64)
    candidate_row["payload"].update(id=str(candidate_row["id"]), adapter_local_id="forged_local")
    with f.engine.connect() as conn:
        candidate_row["payload_bytes"] = conn.scalar(
            text("SELECT octet_length(CAST(:payload AS jsonb)::text)"),
            {"payload": json.dumps(candidate_row["payload"])},
        )
    snapshot_row.pop("parent_revision")
    snapshot_row.update(revision=2, parent_hash="f" * 64, snapshot_hash="a" * 64)
    for table, row in (("adapter_candidate", candidate_row), ("staging_snapshot", snapshot_row)):
        row["payload"] = json.dumps(row["payload"])
        columns = ",".join(row)
        values = ",".join("CAST(:payload AS jsonb)" if field == "payload" else ":" + field for field in row)
        with pytest.raises(DBAPIError) as failure, f.engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO {f.name(conn, 'staging', table)}({columns}) VALUES({values})"), row
            )
        assert failure.value.orig.sqlstate == ("23514" if table == "adapter_candidate" else "23503")
    assert len(records(f, "adapter_candidate")) == len(records(f, "staging_snapshot")) == 1


def test_family_payload_byte_bound_denies_before_record_or_collection_capacity(assembly):
    f, _, worker, editor, _, _, acq, artifact, _ = assembly
    candidates = tuple(candidate(100 + i, region=f"pair_{i // 2}") for i in range(50))
    run = run_command(f, artifact, acq, candidates=candidates).model_copy(
        update={"profile_key": "bounded_large_proposals"}
    )
    assert apply_assembly(f, worker, run).status == "applied"
    selected = [(f"pair_{i}", UUID(int=100 + 2 * i)) for i in range(25)]
    resolutions = [
        dict(
            region_key=region,
            candidate_id=id,
            rejected_candidate_ids=[UUID(int=id.int + 1)],
            reason="Original synthetic resolution " + ("x" * 2018),
        )
        for region, id in selected
    ]
    parent = None
    snapshot_id = None
    for index in range(64):
        revision = index % 32
        if revision == 0:
            snapshot_id = uuid4()
            parent = None
        cmd = snapshot_command(
            f,
            run,
            revision=revision,
            parent_hash=parent,
            snapshot_id=snapshot_id,
            selected=selected,
            resolutions=resolutions,
        )
        result = apply_assembly(f, editor, cmd)
        if result.status == "capacity_exceeded":
            break
        assert result.status == "applied"
        parent = result.output_hash
    assert result.status == "capacity_exceeded"
    rows = records(f, "adapter_candidate") + records(f, "staging_snapshot")
    assert len(rows) < 128 and sum(row["payload_bytes"] for row in rows) <= 2 * 1024 * 1024
