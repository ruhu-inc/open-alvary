"""Original synthetic artifacts and native acquisition/privacy sessions; no fetching."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_operators import apply
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from rights.acquisition import AcquisitionError, SyntheticAcquisition, declare_synthetic_staging_limit
from rights.commands import OperatorCommands, parse_command
from rights.database import PolicyNamespace
from schema.identity import mint

pytestmark = pytest.mark.postgres
RAW = b"Original synthetic private acquisition fixture. Not legislation or personal data."
PURPOSE = "synthetic_local_review"


def review(f, action, payload, **changes):
    common = {
        "id": str(uuid4()),
        "evidence_id": str(f.evidence),
        "valid_from": (f.now - timedelta(minutes=1)).isoformat(),
        "expires_at": (f.now + timedelta(hours=1)).isoformat(),
        "state": "approved",
    }
    common.update(payload)
    data = {
        "command_id": str(uuid4()),
        "action": action,
        "collection_id": f.collection,
        "expected_revision": 0,
        "reason": "Original synthetic review; no legal approval or real controller",
        "payload": common,
    }
    data.update(changes)
    return parse_command(json.dumps(data).encode())


def controller(f, **changes):
    return review(
        f,
        "record_controller",
        {
            "legal_name": "Original synthetic controller (fictional)",
            "postal_address": "Fictional test address",
            "jurisdiction_id": "ng",
            "privacy_contact": "synthetic@example.test",
            "accountable_role": "Synthetic test role",
        },
        **changes,
    )


def privacy(f, ctl, **payload):
    fields = {
        "controller_id": str(ctl.payload.id),
        "controller_revision": 1,
        "purpose": PURPOSE,
        "lawful_basis": "Original synthetic fixture only",
        "assessment_reference": "private://synthetic-assessment",
        "clearance": "cleared",
    }
    fields.update(payload)
    return review(f, "record_privacy_review", fields)


def assessment(f, pv=None, **payload):
    fields = {
        "discover": "allow",
        "acquire": "allow",
        "retain": "allow",
        "derive": "unknown",
        "privacy_class": "personal_data" if pv else "no_personal_data",
        "classification_reason": "Original synthetic classification; no real source",
        "purpose": PURPOSE,
        "retention_deadline": (f.now + timedelta(minutes=30)).isoformat(),
        "artifact_hash": sha256(RAW).hexdigest(),
    }
    if pv:
        fields.update(privacy_review_id=str(pv.payload.id), privacy_review_revision=1)
    fields.update(payload)
    return review(f, "record_acquisition_assessment", fields)


def declare(f, maximum=100 * 1024 * 1024):
    with f.engine.begin() as conn:
        declare_synthetic_staging_limit(
            conn,
            f.ns,
            collection_id=f.collection,
            maximum_bytes=maximum,
            evidence_id=f.evidence,
            reason="Original synthetic capacity declaration; not production approval",
        )


def stage(f, worker, decision, artifact=None, **changes):
    args = {
        "artifact_id": artifact or uuid4(),
        "collection_id": f.collection,
        "assessment_id": decision.payload.id,
        "assessment_revision": decision.expected_revision + 1,
        "purpose": PURPOSE,
        "private_bytes": RAW,
        "retention_deadline": f.now + timedelta(minutes=20),
    }
    args.update(changes)
    with worker.engine.begin() as conn:
        result = SyntheticAcquisition(conn, f.ns).stage(**args)
    return args["artifact_id"], result


@pytest.fixture
def chain(operators):
    f, create, _ = operators
    rights, worker = create("rights"), create("acquire")
    ctl = controller(f)
    assert apply(f, rights, ctl).status == "applied"
    pv = privacy(f, ctl)
    assert apply(f, rights, pv).status == "applied"
    acq = assessment(f, pv)
    assert apply(f, rights, acq).status == "applied"
    return f, rights, worker, ctl, pv, acq


def test_native_review_chain_stages_reads_retries_and_audits_without_public_text(chain):
    f, rights, worker, ctl, pv, acq = chain
    for cmd in (ctl, pv, acq):
        assert apply(f, rights, cmd).replayed
    declare(f)
    artifact, replayed = stage(f, worker, acq)
    assert not replayed
    assert stage(f, worker, acq, artifact)[1]
    with worker.engine.begin() as conn:
        service = SyntheticAcquisition(conn, f.ns)
        assert service.read(artifact, purpose=PURPOSE) == RAW
        assert service.read(artifact, purpose="another-purpose") is None
        assert service.read(artifact, purpose=PURPOSE, operation="derive") is None
        assert service.read(artifact, purpose=PURPOSE, operation="external_process") is None
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 3
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 4
        row = conn.execute(
            text(
                f"SELECT purpose,acquired_by,artifact_hash FROM {f.name(conn, 'staging', 'staged_artifact')}"
            )
        ).one()
        assert (
            row.purpose == PURPOSE
            and row.acquired_by == worker.actor
            and row.artifact_hash == sha256(RAW).hexdigest()
        )
        assert conn.scalar(text("SELECT count(*) FROM versions")) == 0
        assert conn.scalar(text("SELECT count(*) FROM identity_observations")) == 0


def test_staging_failure_is_safe_and_outer_rollback_releases_capacity(chain):
    f, _, worker, _, _, acq = chain
    declare(f, len(RAW))
    with worker.engine.connect() as conn:
        transaction = conn.begin()
        service = SyntheticAcquisition(conn, f.ns)
        with pytest.raises(AcquisitionError) as denied:
            service.stage(
                artifact_id=uuid4(),
                collection_id=f.collection,
                assessment_id=acq.payload.id,
                assessment_revision=1,
                purpose=PURPOSE,
                private_bytes=b"private-do-not-echo-invalid-bytes",
                retention_deadline=f.now + timedelta(minutes=20),
            )
        assert "private-do-not-echo" not in str(denied.value)
        assert conn.scalar(text("SELECT 1")) == 1
        assert not service.stage(
            artifact_id=uuid4(),
            collection_id=f.collection,
            assessment_id=acq.payload.id,
            assessment_revision=1,
            purpose=PURPOSE,
            private_bytes=RAW,
            retention_deadline=f.now + timedelta(minutes=20),
        )
        transaction.rollback()
    assert not stage(f, worker, acq)[1]
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='synthetic_staging'"
                )
            )
            == 1
        )


def test_explicit_derive_permission_and_redacted_hash_apply_to_private_reads(operators):
    f, create, _ = operators
    rights, worker = create("rights"), create("acquire")
    ctl = controller(f)
    assert apply(f, rights, ctl).status == "applied"
    pv = privacy(f, ctl, clearance="redacted", cleared_hash=sha256(RAW).hexdigest())
    assert apply(f, rights, pv).status == "applied"
    acq = assessment(f, pv, derive="allow")
    assert apply(f, rights, acq).status == "applied"
    declare(f)
    artifact, _ = stage(f, worker, acq)
    with worker.engine.begin() as conn:
        service = SyntheticAcquisition(conn, f.ns)
        assert service.read(artifact, purpose=PURPOSE, operation="derive") == RAW
        assert service.read(artifact, purpose=PURPOSE, operation="external_process") is None


def test_reassessment_creates_fresh_lineage_without_retargeting_old_bytes(chain):
    f, rights, worker, ctl, pv, acq = chain
    declare(f)
    old_artifact, _ = stage(f, worker, acq)
    assert (
        apply(f, rights, ctl.model_copy(update={"command_id": uuid4(), "expected_revision": 1})).status
        == "applied"
    )
    data = pv.model_dump(mode="json")
    data.update(command_id=str(uuid4()), expected_revision=1)
    data["payload"]["controller_revision"] = 2
    fresh_privacy = parse_command(json.dumps(data).encode())
    assert apply(f, rights, fresh_privacy).status == "applied"
    data = acq.model_dump(mode="json")
    data.update(command_id=str(uuid4()), expected_revision=1)
    data["payload"]["privacy_review_revision"] = 2
    fresh_assessment = parse_command(json.dumps(data).encode())
    assert apply(f, rights, fresh_assessment).status == "applied"
    new_artifact, _ = stage(f, worker, fresh_assessment)
    with worker.engine.begin() as conn:
        service = SyntheticAcquisition(conn, f.ns)
        assert service.read(old_artifact, purpose=PURPOSE) is None
        assert service.read(new_artifact, purpose=PURPOSE) == RAW
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT assessment_revision FROM {f.name(conn, 'staging', 'staged_artifact')} WHERE id=:id"
                ),
                {"id": old_artifact},
            )
            == 1
        )


def test_parent_supersession_waits_for_authorized_read_transaction(chain):
    f, rights, worker, ctl, _, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    started = Event()
    backend_pid = []
    newer = ctl.model_copy(update={"command_id": uuid4(), "expected_revision": 1})

    def supersede():
        with rights.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            backend_pid.append(conn.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return OperatorCommands(conn, f.ns).apply(newer)

    with ThreadPoolExecutor(max_workers=1) as pool, worker.engine.connect() as conn:
        transaction = conn.begin()
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) == RAW
        future = pool.submit(supersede)
        assert started.wait(timeout=2)
        deadline = monotonic() + 2
        waiting = False
        with f.engine.connect() as probe:
            while monotonic() < deadline:
                waiting = probe.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                    {"pid": backend_pid[0]},
                )
                if waiting:
                    break
                sleep(0.01)
        assert waiting
        transaction.commit()
        assert future.result(timeout=6).status == "applied"
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) is None


def test_concurrent_acquisition_commands_have_one_receipt_for_identical_retry(operators):
    f, create, _ = operators
    rights = create("rights")
    cmd = assessment(f)
    barrier = Barrier(2)

    def run():
        with rights.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='5s'"))
            barrier.wait(timeout=3)
            return OperatorCommands(conn, f.ns).apply(cmd)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [future.result(timeout=8) for future in futures]
    assert all(result.status == "applied" for result in results)
    assert sorted(result.replayed for result in results) == [False, True]
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 1


def test_approved_assessment_failures_do_not_commit_receipts_or_revision_heads(operators):
    f, create, _ = operators
    rights = create("rights")
    cmd = assessment(f)
    data = cmd.model_dump(mode="json")
    data["payload"]["evidence_id"] = str(uuid4())
    invalid = parse_command(json.dumps(data).encode())
    with rights.engine.begin() as conn:
        assert OperatorCommands(conn, f.ns).apply(invalid).status == "validation_failed"
        assert conn.scalar(text("SELECT 1")) == 1
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'revision_head')}")) == 0
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 0
    assert apply(f, rights, cmd).status == "applied"


@pytest.mark.parametrize("action", ["controller", "privacy", "acquisition"])
def test_worker_cannot_approve_assessments_or_controllers(operators, action):
    f, create, _ = operators
    worker = create("acquire")
    ctl = controller(f)
    cmd = ctl if action == "controller" else privacy(f, ctl) if action == "privacy" else assessment(f)
    assert apply(f, worker, cmd).status == "forbidden"


def test_no_personal_data_assessment_needs_no_invented_controller_but_exact_bytes(operators):
    f, create, _ = operators
    rights, worker = create("rights"), create("acquire")
    acq = assessment(f)
    assert apply(f, rights, acq).status == "applied"
    declare(f)
    artifact, _ = stage(f, worker, acq)
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) == RAW
    with f.engine.connect() as conn:
        for table in ("controller_record", "privacy_review"):
            assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', table)}")) == 0


@pytest.mark.parametrize("change", ["purpose", "hash", "missing_hash", "missing_budget", "retention"])
def test_staging_denies_unqualified_input_without_retaining_bytes(chain, change):
    f, rights, worker, _, pv, acq = chain
    if change != "missing_budget":
        declare(f)
    kwargs = {}
    if change == "purpose":
        kwargs["purpose"] = "wrong-purpose"
    if change == "hash":
        kwargs["private_bytes"] = b"Changed original synthetic bytes"
    if change == "retention":
        kwargs["retention_deadline"] = f.now + timedelta(hours=2)
    if change == "missing_hash":
        acq = assessment(f, pv, artifact_hash=None)
        assert apply(f, rights, acq).status == "applied"
    with pytest.raises(AcquisitionError):
        stage(f, worker, acq, **kwargs)
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'staging', 'staged_artifact')}")) == 0


@pytest.mark.parametrize(
    "change", ["controller_superseded", "privacy_superseded", "purpose_mismatch", "redacted_wrong_hash"]
)
def test_positive_acquisition_requires_current_exact_privacy_authority(chain, change):
    f, rights, _, ctl, pv, _ = chain
    if change == "controller_superseded":
        newer = ctl.model_copy(update={"command_id": uuid4(), "expected_revision": 1})
        assert apply(f, rights, newer).status == "applied"
    elif change == "privacy_superseded":
        newer = pv.model_copy(update={"command_id": uuid4(), "expected_revision": 1})
        assert apply(f, rights, newer).status == "applied"
    elif change == "purpose_mismatch":
        pv = privacy(f, ctl, purpose="different-purpose")
        assert apply(f, rights, pv).status == "applied"
    else:
        pv = privacy(f, ctl, clearance="redacted", cleared_hash="f" * 64)
        assert apply(f, rights, pv).status == "applied"
    assert apply(f, rights, assessment(f, pv)).status == "forbidden"


def test_privacy_approval_rejects_stale_controller_but_rejection_remains_recordable(chain):
    f, rights, _, ctl, _, _ = chain
    assert (
        apply(f, rights, ctl.model_copy(update={"command_id": uuid4(), "expected_revision": 1})).status
        == "applied"
    )
    assert apply(f, rights, privacy(f, ctl)).status == "forbidden"
    assert apply(f, rights, privacy(f, ctl, clearance="rejected")).status == "applied"


@pytest.mark.parametrize("which", ["controller", "privacy", "acquisition"])
def test_supersession_hides_staging_before_any_reconciliation_job(chain, which):
    f, rights, worker, ctl, pv, acq = chain
    declare(f)
    artifact, _ = stage(f, worker, acq)
    original = {"controller": ctl, "privacy": pv, "acquisition": acq}[which]
    changed = original.model_copy(update={"command_id": uuid4(), "expected_revision": 1})
    assert apply(f, rights, changed).status == "applied"
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) is None
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'staging', 'staged_artifact')}")) == 0
    # Old immutable lineage cannot silently retarget to the new revision.
    with pytest.raises(AcquisitionError):
        stage(f, worker, acq, artifact)


def test_rejected_privacy_hides_bytes_and_erasure_is_scoped_audited_idempotent(chain, operators):
    f, rights, worker, _, pv, acq = chain
    _, create, _ = operators
    outsider = create("acquire", assignments=((f.other_collection, "acquire"),))
    declare(f)
    artifact, _ = stage(f, worker, acq)
    data = pv.model_dump(mode="json")
    data.update(command_id=str(uuid4()), expected_revision=1)
    data["payload"]["clearance"] = "rejected"
    assert apply(f, rights, parse_command(json.dumps(data).encode())).status == "applied"
    with worker.engine.begin() as conn:
        service = SyntheticAcquisition(conn, f.ns)
        assert service.read(artifact, purpose=PURPOSE) is None
        assert service.erase(artifact)
        assert service.erase(artifact)
    with pytest.raises(AcquisitionError), outsider.engine.begin() as conn:
        SyntheticAcquisition(conn, f.ns).erase(artifact)
    with f.engine.connect() as conn:
        assert conn.scalar(
            text(f"SELECT private_bytes IS NULL FROM {f.name(conn, 'staging', 'staged_artifact')}")
        )
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')} WHERE kind='staging_erasure'"
                )
            )
            == 1
        )
    with pytest.raises(AcquisitionError):
        stage(f, worker, acq, artifact)


def test_native_direct_sql_cannot_omit_purpose_or_skip_erasure_audit(chain):
    f, _, worker, _, _, acq = chain
    declare(f)
    with pytest.raises(DBAPIError), worker.engine.begin() as conn:
        conn.execute(
            text(f"""INSERT INTO {f.name(conn, "staging", "staged_artifact")}
            (id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,private_bytes,retention_deadline)
            VALUES(:id,:cid,:assessment,1,:hash,:size,:bytes,:deadline)"""),
            {
                "id": uuid4(),
                "cid": f.collection,
                "assessment": acq.payload.id,
                "hash": sha256(RAW).hexdigest(),
                "size": len(RAW),
                "bytes": RAW,
                "deadline": f.now + timedelta(minutes=20),
            },
        )
    artifact, _ = stage(f, worker, acq)
    with pytest.raises(DBAPIError), worker.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {f.name(conn, 'staging', 'staged_artifact')} SET state='erased',private_bytes=NULL WHERE id=:id"
            ),
            {"id": artifact},
        )


def test_expired_retention_keeps_capacity_until_explicit_erasure(chain):
    f, _, worker, _, _, acq = chain
    declare(f, len(RAW))
    with f.engine.connect() as conn:
        now = conn.scalar(text("SELECT statement_timestamp()"))
    artifact, _ = stage(f, worker, acq, retention_deadline=now + timedelta(seconds=3))
    # Use database time; no maintenance mutation of immutable deadlines.
    deadline = monotonic() + 4
    while monotonic() < deadline:
        with f.engine.connect() as conn:
            expired = conn.scalar(
                text(
                    f"SELECT retention_deadline<=statement_timestamp() FROM {f.name(conn, 'staging', 'staged_artifact')} WHERE id=:id"
                ),
                {"id": artifact},
            )
        if expired:
            break
        sleep(0.05)
    assert expired
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(artifact, purpose=PURPOSE) is None
    with pytest.raises(AcquisitionError):
        stage(f, worker, acq)
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    assert not stage(f, worker, acq)[1]


def test_collection_capacity_is_serialized_across_workers_and_outer_rollback(chain):
    f, _, worker, _, _, acq = chain
    declare(f, len(RAW))
    barrier = Barrier(2)

    def run():
        try:
            with worker.engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout='5s'"))
                barrier.wait(timeout=3)
                SyntheticAcquisition(conn, f.ns).stage(
                    artifact_id=uuid4(),
                    collection_id=f.collection,
                    assessment_id=acq.payload.id,
                    assessment_revision=1,
                    purpose=PURPOSE,
                    private_bytes=RAW,
                    retention_deadline=f.now + timedelta(minutes=20),
                )
            return "applied"
        except AcquisitionError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        assert sorted(future.result(timeout=8) for future in futures) == ["applied", "capacity_exceeded"]
    with f.engine.connect() as conn:
        assert conn.scalar(
            text(f"SELECT sum(size_bytes) FROM {f.name(conn, 'staging', 'staged_artifact')}")
        ) == len(RAW)


def test_new_assessment_retry_does_not_restore_superseded_current_authority(chain):
    f, rights, worker, _, _, acq = chain
    data = acq.model_dump(mode="json")
    data.update(command_id=str(uuid4()), expected_revision=1)
    data["payload"]["retain"] = "deny"
    assert apply(f, rights, parse_command(json.dumps(data).encode())).revision == 2
    assert apply(f, rights, acq).replayed
    with worker.engine.begin() as conn:
        assert not SyntheticAcquisition(conn, f.ns).allowed(
            collection_id=f.collection,
            assessment_id=acq.payload.id,
            assessment_revision=1,
            purpose=PURPOSE,
            artifact_hash=sha256(RAW).hexdigest(),
            operation="retain",
        )


def test_original_schema_upgrade_preserves_unqualified_records_without_fabrication(pg_engine):
    migrate(pg_engine, "0004")
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        for table in ("synthetic_staging_limit", "controller_record", "acquisition_assessment"):
            assert conn.scalar(text(f"SELECT count(*) FROM {ns.qualified(conn, 'policy', table)}")) == 0


def test_populated_0004_upgrade_preserves_receipts_bindings_and_unqualified_bytes(operators):
    f, create, _ = operators
    rights = create("rights")
    with f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0004")
    from test_postgres_operators import evidence_command

    assert apply(f, rights, evidence_command(f)).status == "applied"
    legacy_assessment = f.assessment()
    legacy_artifact = f.stage(legacy_assessment)
    with f.engine.connect() as conn:
        before = {
            table: list(
                conn.execute(text(f"SELECT * FROM {f.name(conn, 'policy', table)} ORDER BY 1")).mappings()
            )
            for table in ("actor", "command_receipt", "acquisition_assessment")
        }
        original_bytes = conn.scalar(
            text(f"SELECT private_bytes FROM {f.name(conn, 'staging', 'staged_artifact')}")
        )
    migrate(f.engine)
    with f.engine.connect() as conn:
        for table in ("actor", "command_receipt"):
            assert (
                list(
                    conn.execute(text(f"SELECT * FROM {f.name(conn, 'policy', table)} ORDER BY 1")).mappings()
                )
                == before[table]
            )
        record = dict(
            conn.execute(text(f"SELECT * FROM {f.name(conn, 'policy', 'acquisition_assessment')}"))
            .mappings()
            .one()
        )
        assert record.pop("artifact_hash") is None and record == before["acquisition_assessment"][0]
        row = conn.execute(
            text(
                f"SELECT private_bytes,purpose,acquired_by FROM {f.name(conn, 'staging', 'staged_artifact')}"
            )
        ).one()
        assert row.private_bytes == original_bytes and row.purpose is None and row.acquired_by is None
    worker = create("acquire")
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).read(legacy_artifact["id"], purpose="local_test") is None
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'staging', 'staged_artifact')}")) == 0


def test_judgment_class_requires_privacy_even_when_labelled_no_personal_data(operators):
    f, create, _ = operators
    collection = mint("scp")
    with f.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO identity_collections(id,jurisdiction_id,document_class,provider_key) VALUES(:id,'ng','judgment','original-synthetic-court')"
            ),
            {"id": collection},
        )
    rights = create("rights", assignments=((collection, "rights"),))
    cmd = assessment(f).model_copy(update={"collection_id": collection})
    assert apply(f, rights, cmd).status == "forbidden"


def test_acquisition_cli_commits_without_echoing_private_controller_contacts(operators):
    f, create, _ = operators
    rights = create("rights")
    cmd = controller(f)
    env = dict(
        os.environ, ALVARY_OPERATOR_DATABASE_URL=rights.engine.url.render_as_string(hide_password=False)
    )
    result = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--schema", f.ns.base],
        input=cmd.model_dump_json(),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "applied"
    assert not result.stderr
    assert cmd.payload.privacy_contact not in result.stdout and rights.password not in result.stdout


def test_nonempty_acquisition_workflow_refuses_destructive_downgrade(chain):
    f, _, _, _, _, _ = chain
    with pytest.raises(RuntimeError, match="requires forward repair"), f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0004")
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 3
