"""Independent SQL serializer and native private OA-text-1 assembly/lifecycle."""

import json
from hashlib import sha256
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_canonical import leaf, project, table_candidates, table_plan, texts
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_acquisition import PURPOSE
from test_postgres_acquisition import chain as chain
from test_postgres_assembly import apply_assembly, records, run_command, snapshot_command
from test_postgres_assembly import assembly as assembly
from test_postgres_lifecycle import newer
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from ingestion.canonical import PROFILE_HASH, compile_projection
from rights.acquisition import AcquisitionError, SyntheticAcquisition
from rights.canonical import CanonicalAssembler
from rights.lifecycle import AcquisitionLifecycle
from rights.operators import revoke_operator
from schema.canonical import CanonicalCommand
from schema.operator import LifecycleRequest

pytestmark = pytest.mark.postgres


def canonical_command(f, snapshot, hash, plan, **changes):
    data = dict(
        command_id=uuid4(),
        collection_id=f.collection,
        reason="Original synthetic canonical proposal only",
        action="record_canonical_proposal",
        proposal_id=uuid4(),
        snapshot_id=snapshot.snapshot_id,
        snapshot_revision=snapshot.expected_revision + 1,
        snapshot_hash=hash,
        purpose=PURPOSE,
        plan=plan,
    )
    data.update(changes)
    return CanonicalCommand.model_validate(data)


def call(f, subject, cmd):
    with subject.engine.begin() as conn:
        return CanonicalAssembler(conn, f.ns).apply(cmd)


@pytest.fixture
def projection(assembly):
    f, rights, worker, editor, ctl, pv, acq, artifact, run = assembly
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    cmd = canonical_command(f, snap, saved.output_hash, texts(run.candidates))
    result = call(f, editor, cmd)
    assert result.status == "applied", result
    return f, rights, worker, editor, ctl, pv, acq, artifact, run, snap, cmd, result


@pytest.mark.parametrize(
    "shape",
    [
        "literal",
        "merged",
        "children",
        "empty_document",
        "unavailable",
        "footnotes",
        "notice",
        "repeated_header",
    ],
)
def test_independent_python_and_postgresql_serializers_agree(pg_engine, shape):
    migrate(pg_engine)
    from rights.database import PolicyNamespace

    if shape == "literal":
        candidates = [leaf(1, " Cafe\u0301\r\nلا\u2067RTL\u2069\t😀 "), leaf(2, "No 1,000.00 waiver.")]
        plan = texts(candidates)
    elif shape == "merged":
        candidates = table_candidates()
        plan = table_plan(candidates)
    elif shape == "children":
        candidates = [
            leaf(1, "First", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
            leaf(2, "Second", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
        ]
        plan = dict(
            blocks=[
                dict(
                    kind="table",
                    table_local_id="t",
                    rows=1,
                    columns=1,
                    cells=[dict(candidates=[c.id for c in candidates])],
                )
            ]
        )
    elif shape == "empty_document":
        candidates = [leaf(1, "Page 1")]
        plan = dict(
            blocks=[],
            exclusions=[
                dict(
                    candidate_id=candidates[0].id,
                    kind="furniture",
                    reason="Original synthetic furniture proposal",
                )
            ],
        )
    elif shape == "unavailable":
        candidates = [
            leaf(1, "Cell", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0))
        ]
        plan = dict(
            blocks=[
                dict(
                    kind="table",
                    table_local_id="t",
                    rows=1,
                    columns=2,
                    cells=[dict(candidates=[candidates[0].id])],
                    unavailable=[
                        dict(
                            row=0,
                            column=1,
                            content_state="unsupported",
                            reason="No synthetic cell transcription",
                        )
                    ],
                )
            ]
        )
    elif shape == "repeated_header":
        candidates = [
            leaf(1, "Header", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
            leaf(2, "Header", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
        ]
        plan = dict(
            blocks=[
                dict(
                    kind="table",
                    table_local_id="t",
                    rows=1,
                    columns=1,
                    cells=[dict(candidates=[candidates[0].id])],
                    headers=[candidates[0].id],
                )
            ],
            exclusions=[
                dict(
                    candidate_id=candidates[1].id,
                    kind="repeated_header",
                    target_candidate_id=candidates[0].id,
                    reason="Original synthetic repeat proposal",
                )
            ],
        )
    else:
        candidates = [
            leaf(1, "Notice [b] then [a]."),
            leaf(2, "A", block_type="footnote"),
            leaf(3, "B", block_type="footnote"),
            leaf(4, "Unreferenced", block_type="footnote"),
        ]
        scope = dict(
            blocks=[dict(kind="text", candidate_id=candidates[0].id)],
            footnotes=[
                dict(local_id="a", candidates=[candidates[1].id]),
                dict(local_id="b", candidates=[candidates[2].id]),
                dict(local_id="c", candidates=[candidates[3].id]),
            ],
            markers=[
                dict(candidate_id=candidates[0].id, source_start=16, source_end=19, footnote_id="a"),
                dict(candidate_id=candidates[0].id, source_start=7, source_end=10, footnote_id="b"),
            ],
        )
        plan = (
            scope if shape == "footnotes" else dict(blocks=[dict(kind="notice", local_id="notice1", **scope)])
        )
    expected = project(candidates, plan)
    from schema.canonical import CanonicalPlan

    parsed = CanonicalPlan.model_validate(plan)
    inputs = dict(
        source={str(c.id): c.model_dump(mode="json") for c in candidates},
        order=[str(c.id) for c in candidates],
    )
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        fn = ns.qualified(conn, "policy", "oa_compile")
        result = conn.scalar(
            text(f"SELECT {fn}(CAST(:inputs AS jsonb),CAST(:plan AS jsonb))"),
            {"inputs": json.dumps(inputs), "plan": parsed.model_dump_json()},
        )
    assert result["text"].encode() == expected.canonical_bytes
    assert result["projection"] == expected.details.model_dump(mode="json")


def test_native_canonical_projection_is_private_current_pinned_crosschecked_and_retryable(projection):
    f, _, worker, editor, _, _, _, _, run, snap, cmd, result = projection
    assert call(f, editor, cmd).replayed
    with worker.engine.begin() as conn:
        view = CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose=PURPOSE)
        assert view and view.canonical_text.encode() == run.candidates[0].text.encode() + b"\n"
        assert view.content_hash == sha256(view.canonical_text.encode()).hexdigest() == result.output_hash
        assert view.profile_hash == PROFILE_HASH and view.snapshot_hash == cmd.snapshot_hash
        assert view.payload.projection.publication_eligible is False
        assert CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose="wrong-purpose") is None
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM versions")) == 0
        assert conn.scalar(text("SELECT count(*) FROM identity_observations")) == 0
    with pytest.raises(DBAPIError), worker.engine.begin() as conn:
        conn.execute(text(f"SELECT canonical_bytes FROM {f.name(conn, 'staging', 'canonical_proposal')}"))


def test_native_table_projection_never_invents_text_for_missing_transcription(assembly):
    f, _, worker, editor, _, _, acq, artifact, _ = assembly
    from uuid import UUID

    candidates = tuple(
        c.model_copy(
            update={
                "id": UUID(int=c.id.int + 100),
                "adapter_local_id": "canonical_" + c.adapter_local_id,
                "region_key": "canonical_" + c.region_key,
            }
        )
        for c in table_candidates()
    )
    run = run_command(f, artifact, acq, candidates=candidates).model_copy(
        update={"profile_key": "canonical_table"}
    )
    assert apply_assembly(f, worker, run).status == "applied"
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    cmd = canonical_command(f, snap, saved.output_hash, table_plan(candidates))
    result = call(f, editor, cmd)
    assert result.status == "applied", result
    with worker.engine.begin() as conn:
        view = CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose=PURPOSE)
        assert view.canonical_text.encode() == b"Amount\tunit\t\t\n1,000.00\nnot waived\t\t\n"
        assert view.payload.projection.incomplete_tables == ("t1",)


@pytest.mark.parametrize("kind", ["worker", "content_only", "rights", "outsider", "revoked"])
def test_canonical_write_requires_current_native_content_and_acquisition_scope(projection, operators, kind):
    f, _, worker, editor, _, _, _, _, _, _, cmd, _ = projection
    _, create, _ = operators
    if kind == "worker":
        subject = worker
    elif kind == "content_only":
        subject = create("content")
    elif kind == "outsider":
        subject = create(
            "content", assignments=((f.other_collection, "content"), (f.other_collection, "acquire"))
        )
    elif kind == "revoked":
        subject = editor
        with f.engine.begin() as conn:
            revoke_operator(conn, f.ns, editor.actor, reason="Synthetic canonical revocation")
    else:
        subject = create("rights")
    assert (
        call(f, subject, cmd.model_copy(update={"command_id": uuid4(), "proposal_id": uuid4()})).status
        == "forbidden"
    )
    assert len(records(f, "canonical_proposal")) == 1


def test_snapshot_edit_immediately_withholds_old_projection_and_materializer_holds_it(projection):
    f, _, worker, editor, _, _, _, _, run, snap, cmd, _ = projection
    next_snap = snapshot_command(
        f, run, revision=1, parent_hash=cmd.snapshot_hash, snapshot_id=snap.snapshot_id
    )
    updated = apply_assembly(f, editor, next_snap)
    assert updated.status == "applied"
    with worker.engine.begin() as conn:
        assert CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose=PURPOSE) is None
    assert call(f, editor, cmd).status == "forbidden"
    from test_postgres_assembly import reconcile

    assert reconcile(f, worker).held == 1
    assert records(f, "canonical_proposal")[0]["state"] == "held"
    fresh = canonical_command(f, next_snap, updated.output_hash, texts(run.candidates))
    assert call(f, editor, fresh).status == "applied"
    assert records(f, "canonical_proposal")[0]["snapshot_id"] == snap.snapshot_id


@pytest.mark.parametrize("parent", ["controller", "privacy", "acquisition"])
def test_parent_supersession_closes_projection_reads_before_jobs_and_inherits_hold(projection, parent):
    f, rights, worker, _, ctl, pv, acq, _, _, _, cmd, _ = projection
    newer(f, rights, {"controller": ctl, "privacy": pv, "acquisition": acq}[parent])
    with worker.engine.begin() as conn:
        assert CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose=PURPOSE) is None
        result = AcquisitionLifecycle(conn, f.ns).reconcile(
            LifecycleRequest(
                command_id=uuid4(), collection_id=f.collection, reason="Synthetic canonical raw hold"
            )
        )
        assert result.held == 1
    row = records(f, "canonical_proposal")[0]
    assert row["state"] == "held" and row["canonical_bytes"] is not None


def test_raw_erasure_removes_canonical_bytes_and_metadata_payload_without_losing_hashes(projection):
    f, _, worker, _, _, _, _, artifact, _, _, _, _ = projection
    before = records(f, "canonical_proposal")[0]
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    after = records(f, "canonical_proposal")[0]
    assert (
        after.pop("state") == "erased"
        and after.pop("canonical_bytes") is None
        and after.pop("payload") is None
    )
    for field in ("state", "canonical_bytes", "payload"):
        before.pop(field)
    assert before == after


def test_serializer_disagreement_cannot_persist_in_native_database_even_with_direct_rpc(assembly):
    f, _, _, editor, _, _, _, _, run = assembly
    snap = snapshot_command(f, run)
    result = apply_assembly(f, editor, snap)
    cmd = canonical_command(f, snap, result.output_hash, texts(run.candidates))
    computed = compile_projection(cmd.plan, run.candidates, tuple(c.id for c in run.candidates))
    with editor.engine.begin() as conn:
        service = CanonicalAssembler(conn, f.ns)
        fn = f.ns.qualified(conn, "policy", "store_canonical_proposal")
        with pytest.raises(AcquisitionError) as failure:
            service._scalar(
                text(f"SELECT {fn}(CAST(:cmd AS jsonb),:bytes,CAST(:projection AS jsonb))"),
                {
                    "cmd": cmd.model_dump_json(),
                    "bytes": b"private-do-not-echo fabricated words",
                    "projection": computed.details.model_dump_json(),
                },
            )
        assert failure.value.status == "validation_failed" and "private-do-not-echo" not in str(failure.value)
        assert conn.scalar(text("SELECT 1")) == 1
    assert records(f, "canonical_proposal") == []


def test_canonical_outer_rollback_and_changed_retry_have_no_partial_domain_rows(assembly):
    f, _, _, editor, _, _, _, _, run = assembly
    snap = snapshot_command(f, run)
    result = apply_assembly(f, editor, snap)
    cmd = canonical_command(f, snap, result.output_hash, texts(run.candidates))
    with editor.engine.connect() as conn:
        tx = conn.begin()
        assert CanonicalAssembler(conn, f.ns).apply(cmd).status == "applied"
        tx.rollback()
    assert records(f, "canonical_proposal") == []
    assert call(f, editor, cmd).status == "applied"
    assert (
        call(f, editor, cmd.model_copy(update={"reason": "Changed private canonical intent"})).status
        == "conflict"
    )


def test_populated_0007_empty_upgrade_rollback_preserves_existing_private_bytes_and_exact_helpers(assembly):
    f, _, _, _, _, _, _, _, _ = assembly
    with f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0007")
        funcs = (
            "staging_live_bytes(text)",
            "reserve_assembly_capacity(uuid,text,bigint,integer)",
            "transition_assembly(uuid,text,text)",
            "apply_assembly_command(jsonb)",
        )
        defs = {
            fn: conn.scalar(
                text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                {"fn": f.ns.schema("policy") + "." + fn},
            )
            for fn in funcs
        }
    before = {table: records(f, table) for table in ("staged_artifact", "synthetic_run", "adapter_candidate")}
    migrate(f.engine)
    for table in before:
        assert records(f, table) == before[table]
    with f.engine.connect() as conn:
        for fn in funcs:
            alias = fn.replace("(", "_m31(", 1)
            assert (
                conn.scalar(
                    text("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
                    {"fn": f.ns.schema("policy") + "." + alias},
                )
                == defs[fn]
            )


def test_populated_canonical_state_refuses_destructive_downgrade(projection):
    f, *_ = projection
    with f.engine.connect() as conn:
        before = conn.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="requires forward repair"), f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0007")
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == before


def test_rejected_privacy_and_due_derivative_erasure_include_canonical_payload(projection):
    f, rights, worker, _, _, pv, _, artifact, _, _, cmd, _ = projection
    newer(f, rights, pv, clearance="rejected")
    from test_postgres_assembly import reconcile

    result = reconcile(f, worker, erase_due=True)
    assert result.erasure_required == result.erased == 3
    row = records(f, "canonical_proposal")[0]
    assert row["canonical_bytes"] is None and row["payload"] is None and row["state"] == "erased"
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    assert reconcile(f, worker, erase_due=True).erased == 0


def test_canonical_capacity_charges_bytes_and_layout_and_erasure_releases_them(projection):
    f, _, worker, _, _, _, _, artifact, _, _, _, _ = projection
    row = records(f, "canonical_proposal")[0]
    with f.engine.connect() as conn:
        fn = f.ns.qualified(conn, "policy", "staging_live_bytes")
        total = conn.scalar(text(f"SELECT {fn}(:cid)"), {"cid": f.collection})
        parent_bytes = conn.scalar(
            text(f"SELECT sum(octet_length(private_bytes)) FROM {f.name(conn, 'staging', 'staged_artifact')}")
        )
        body_bytes = sum(
            r["payload_bytes"] for r in records(f, "adapter_candidate") + records(f, "staging_snapshot")
        )
        assert total == parent_bytes + body_bytes + row["size_bytes"] + row["payload_bytes"]
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT {fn}(:cid)"), {"cid": f.collection}) == 0


def test_expired_raw_retention_denies_then_erases_all_canonical_output(chain, operators):
    from datetime import timedelta
    from time import monotonic, sleep

    from test_postgres_acquisition import declare, stage

    from rights.lifecycle import AcquisitionLifecycle
    from schema.operator import LifecycleRequest

    f, rights, worker, _, _, acq = chain
    _, create, _ = operators
    editor = create("content", assignments=((f.collection, "content"), (f.collection, "acquire")))
    acq = newer(f, rights, acq, derive="allow")
    declare(f)
    with f.engine.connect() as conn:
        deadline = conn.scalar(text("SELECT statement_timestamp()")) + timedelta(seconds=3)
    artifact, _ = stage(f, worker, acq, retention_deadline=deadline)
    run = run_command(f, artifact, acq)
    assert apply_assembly(f, worker, run).status == "applied"
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    cmd = canonical_command(f, snap, saved.output_hash, texts(run.candidates))
    assert call(f, editor, cmd).status == "applied"
    until = monotonic() + 5
    while monotonic() < until:
        with f.engine.connect() as conn:
            due = conn.scalar(text("SELECT statement_timestamp()>=:deadline"), {"deadline": deadline})
        if due:
            break
        sleep(0.05)
    assert due
    with worker.engine.begin() as conn:
        assert CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose=PURPOSE) is None
        assert (
            AcquisitionLifecycle(conn, f.ns)
            .reconcile(
                LifecycleRequest(
                    command_id=uuid4(),
                    collection_id=f.collection,
                    reason="Synthetic deadline erasure",
                    erase_due=True,
                )
            )
            .erased
            == 1
        )
    assert records(f, "canonical_proposal")[0]["canonical_bytes"] is None


def test_canonical_read_blocks_snapshot_edit_through_caller_commit(projection):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    f, _, worker, editor, _, _, _, _, run, snap, cmd, _ = projection
    ready = Event()
    backend = []
    next_snap = snapshot_command(
        f, run, revision=1, parent_hash=cmd.snapshot_hash, snapshot_id=snap.snapshot_id
    )

    def edit():
        with editor.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            backend.append(conn.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            return SyntheticAssembly(conn, f.ns).apply(next_snap)

    from rights.assembly import SyntheticAssembly

    with ThreadPoolExecutor(max_workers=1) as pool, worker.engine.connect() as conn:
        tx = conn.begin()
        assert CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose=PURPOSE)
        future = pool.submit(edit)
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
        assert CanonicalAssembler(conn, f.ns).read(cmd.proposal_id, purpose=PURPOSE) is None


@pytest.mark.parametrize("identical", [True, False])
def test_concurrent_canonical_builds_share_receipt_or_conflict_without_deadlock(assembly, identical):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    f, _, _, editor, _, _, _, _, run = assembly
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    cmd = canonical_command(f, snap, saved.output_hash, texts(run.candidates))
    other = cmd if identical else cmd.model_copy(update={"command_id": uuid4(), "proposal_id": uuid4()})
    barrier = Barrier(2)

    def build(job):
        with editor.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            barrier.wait(timeout=3)
            return CanonicalAssembler(conn, f.ns).apply(job)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(build, cmd), pool.submit(build, other)]
        results = [future.result(timeout=8) for future in futures]
    if identical:
        assert all(r.status == "applied" for r in results) and sorted(r.replayed for r in results) == [
            False,
            True,
        ]
    else:
        assert sorted(r.status for r in results) == ["applied", "conflict"]
    assert len(records(f, "canonical_proposal")) == 1


def test_erasure_fault_in_canonical_row_rolls_back_entire_raw_derived_cascade(projection):
    f, _, worker, _, _, _, _, artifact, _, _, _, _ = projection
    before = {
        table: records(f, table)
        for table in ("staged_artifact", "adapter_candidate", "staging_snapshot", "canonical_proposal")
    }
    with f.engine.begin() as conn:
        schema = conn.dialect.identifier_preparer.quote_identifier(f.ns.base)
        conn.execute(
            text(f"""CREATE FUNCTION {schema}.fail_canonical_erasure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'private-do-not-echo failure' USING ERRCODE='23514'; END $$;
            CREATE TRIGGER zz_fault BEFORE UPDATE ON {f.name(conn, "staging", "canonical_proposal")}
                FOR EACH ROW EXECUTE FUNCTION {schema}.fail_canonical_erasure();""")
        )
    with worker.engine.begin() as conn:
        with pytest.raises(AcquisitionError) as failure:
            SyntheticAcquisition(conn, f.ns).erase(artifact)
        assert failure.value.status == "validation_failed" and "private-do-not-echo" not in str(failure.value)
        assert conn.scalar(text("SELECT 1")) == 1
    for table, values in before.items():
        assert records(f, table) == values
    with f.engine.begin() as conn:
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'assembly_lifecycle_event')}"))
            == 0
        )
        conn.execute(text(f"DROP TRIGGER zz_fault ON {f.name(conn, 'staging', 'canonical_proposal')}"))
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)


def test_cli_commits_canonical_metadata_without_text_or_credentials(assembly):
    import os
    import subprocess
    import sys

    f, _, _, editor, _, _, _, _, run = assembly
    snap = snapshot_command(f, run)
    saved = apply_assembly(f, editor, snap)
    cmd = canonical_command(f, snap, saved.output_hash, texts(run.candidates))
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
    assert result.returncode == 0 and not result.stderr and json.loads(result.stdout)["status"] == "applied"
    assert editor.password not in result.stdout and run.candidates[0].text not in result.stdout


def test_frozen_migration_blocks_positive_canonical_work_but_keeps_erasure(projection):
    from api.identity import freeze

    f, _, worker, editor, _, _, _, artifact, _, _, cmd, _ = projection
    with f.engine.begin() as conn:
        freeze(conn, expected_epoch=0)
    assert call(f, editor, cmd).status in ("forbidden", "unavailable")
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    assert records(f, "canonical_proposal")[0]["payload"] is None


@pytest.mark.parametrize("corruption", ["guard_create", "guard_inheritance", "function_owner"])
def test_canonical_upgrade_rejects_existing_authority_corruption_before_ddl(assembly, corruption):
    f, *_ = assembly
    with f.engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0007")
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
            conn.execute(
                text(f"ALTER FUNCTION {policy}.transition_assembly(uuid,text,text) OWNER TO {owner}")
            )
        with pytest.raises(RuntimeError, match="boundary review"):
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
        assert (
            conn.scalar(
                text("SELECT to_regclass(:table)"), {"table": f.ns.schema("staging") + ".canonical_proposal"}
            )
            is None
        )
        tx.rollback()
    migrate(f.engine)


@pytest.mark.parametrize("change", ["body_bytes", "body_metadata"])
def test_database_canonical_integrity_rejects_root_forgery(projection, change):
    f, *_ = projection
    row = records(f, "canonical_proposal")[0]
    row.update(id=uuid4(), profile_hash=PROFILE_HASH)
    if change == "body_bytes":
        row["canonical_bytes"] = b"forged"
        row["size_bytes"] = len(row["canonical_bytes"])
    else:
        row["payload"]["projection"]["publication_eligible"] = True
    row["payload"] = json.dumps(row["payload"])
    columns = ",".join(row)
    values = ",".join("CAST(:payload AS jsonb)" if field == "payload" else ":" + field for field in row)
    # Remove the prior row's uniqueness as a confounder: explicitly isolated transaction.
    with f.engine.connect() as conn:
        tx = conn.begin()
        if change == "body_metadata":
            row["payload_hash"], row["payload_bytes"] = conn.execute(
                text(
                    "SELECT encode(sha256(convert_to(CAST(:body AS jsonb)::text,'UTF8')),'hex'),"
                    "octet_length(CAST(:body AS jsonb)::text)"
                ),
                {"body": row["payload"]},
            ).one()
        relation = f.name(conn, "staging", "canonical_proposal")
        constraint = conn.scalar(
            text("SELECT conname FROM pg_constraint WHERE conrelid=to_regclass(:relation) AND contype='u'"),
            {"relation": relation},
        )
        conn.execute(
            text(
                f"ALTER TABLE {relation} DROP CONSTRAINT {conn.dialect.identifier_preparer.quote(constraint)}"
            )
        )
        with pytest.raises(DBAPIError) as failure, conn.begin_nested():
            conn.execute(
                text(
                    f"INSERT INTO {f.name(conn, 'staging', 'canonical_proposal')}({columns}) VALUES({values})"
                ),
                row,
            )
        assert failure.value.orig.sqlstate == "23514"
        tx.rollback()


def test_collection_budget_denies_canonical_bytes_without_partial_receipt(chain, operators):
    from test_postgres_acquisition import RAW, declare, stage
    from test_postgres_assembly import candidate

    f, rights, worker, _, _, acq = chain
    _, create, _ = operators
    acq = newer(f, rights, acq, derive="allow")
    editor = create("content", assignments=((f.collection, "content"), (f.collection, "acquire")))
    artifact = uuid4()
    item = candidate()
    run = run_command(f, artifact, acq, candidates=(item,))
    snap = snapshot_command(f, run)
    with f.engine.connect() as conn:
        size = sum(
            conn.scalar(text("SELECT octet_length(CAST(:body AS jsonb)::text)"), {"body": body})
            for body in (item.model_dump_json(), snap.payload.model_dump_json())
        )
    declare(f, len(RAW) + size)
    stage(f, worker, acq, artifact)
    assert apply_assembly(f, worker, run).status == "applied"
    saved = apply_assembly(f, editor, snap)
    assert saved.status == "applied"
    cmd = canonical_command(f, snap, saved.output_hash, texts(run.candidates))
    with f.engine.connect() as conn:
        receipts = f.ns.qualified(conn, "policy", "assembly_receipt")
        audit = f.ns.qualified(conn, "policy", "audit_event")
        counts = text(f"SELECT (SELECT count(*) FROM {receipts}),(SELECT count(*) FROM {audit})")
        before = conn.execute(counts).one()
    assert call(f, editor, cmd).status == "capacity_exceeded"
    assert not records(f, "canonical_proposal")
    with f.engine.connect() as conn:
        assert conn.execute(counts).one() == before


@pytest.mark.parametrize(
    "shape", ["invented_words", "omitted_leaf", "reordered", "table_gap", "repeat_mismatch"]
)
def test_database_serializer_independently_rejects_invalid_structure(pg_engine, shape):
    from rights.database import PolicyNamespace

    migrate(pg_engine)
    candidates = [leaf(1, "Header"), leaf(2, "Different")]
    plan = json.loads(json.dumps(texts(candidates), default=str))
    if shape == "invented_words":
        plan["blocks"][0]["text"] = "Invented words"
    elif shape == "omitted_leaf":
        plan["blocks"].pop()
    elif shape == "reordered":
        plan["blocks"].reverse()
    else:
        candidates = [
            leaf(1, "Header", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
            leaf(2, "Different", block_type="table_cell", cell=dict(table_local_id="t", row=0, column=0)),
        ]
        plan = dict(
            blocks=[
                dict(
                    kind="table",
                    table_local_id="t",
                    rows=1,
                    columns=2 if shape == "table_gap" else 1,
                    cells=[dict(candidates=[str(candidates[0].id)])],
                    headers=[str(candidates[0].id)],
                )
            ],
            exclusions=[
                dict(
                    candidate_id=str(candidates[1].id),
                    kind="repeated_header",
                    target_candidate_id=str(candidates[0].id),
                    reason="Synthetic repeat claim",
                )
            ],
        )
    inputs = dict(
        source={str(c.id): c.model_dump(mode="json") for c in candidates},
        order=[str(c.id) for c in candidates],
    )
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        fn = ns.qualified(conn, "policy", "oa_compile")
        with pytest.raises(DBAPIError) as failure, conn.begin_nested():
            conn.execute(
                text(f"SELECT {fn}(CAST(:inputs AS jsonb),CAST(:plan AS jsonb))"),
                {"inputs": json.dumps(inputs), "plan": json.dumps(plan)},
            )
        assert failure.value.orig.sqlstate in {"22023", "23514"}
