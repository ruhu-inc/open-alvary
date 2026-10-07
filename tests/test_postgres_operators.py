"""Native password-authenticated synthetic sessions, not SET SESSION AUTHORIZATION."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from secrets import token_hex
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.pool import NullPool
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

from api.identity import freeze
from rights.commands import OperatorCommands, parse_command
from rights.database import PolicyNamespace, PolicyRepository, provision_roles
from rights.operators import SyntheticAppointment, register_operator, revoke_operator
from schema.operator import MATERIALS

pytestmark = pytest.mark.postgres


@dataclass
class Subject:
    engine: object
    role: str
    password: str
    actor: object
    appointment: SyntheticAppointment


@pytest.fixture
def operators(foundation):
    f = foundation
    subjects = {}

    def create(kind, *, subject_key=None, assignments=None):
        role, password = "operator_" + uuid4().hex, token_hex(24)
        appointment = SyntheticAppointment(
            subject_issuer="synthetic-local-test",
            subject_key=subject_key or str(uuid4()),
            authenticated_until=f.now + timedelta(hours=1),
            evidence_reference="private://original-synthetic-appointment",
            evidence_hash="a" * 64,
            reason="Original synthetic identity fixture; no person appointed",
        )
        with f.engine.begin() as conn:
            conn.connection.driver_connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            actor = register_operator(
                conn,
                f.ns,
                login_name=role,
                appointment=appointment,
                assignments=assignments or ((f.collection, kind),),
            )
        url = f.engine.url.set(username=role, password=password)
        engine = create_engine(url, poolclass=NullPool)
        result = Subject(engine, role, password, actor, appointment)
        subjects[role] = result
        return result

    try:
        yield f, create, subjects
    finally:
        for subject in subjects.values():
            subject.engine.dispose()
        with f.engine.begin() as conn:
            quote = conn.dialect.identifier_preparer.quote_identifier
            for subject in subjects.values():
                conn.execute(text(f"DROP OWNED BY {quote(subject.role)}"))
                conn.execute(text(f"DROP ROLE {quote(subject.role)}"))


def evidence_command(f, **changes):
    data = {
        "command_id": str(uuid4()),
        "action": "record_evidence",
        "collection_id": f.collection,
        "expected_revision": 0,
        "reason": "Original synthetic evidence registration",
        "payload": {
            "id": str(uuid4()),
            "private_reference": "private://test-evidence-do-not-echo",
            "evidence_hash": "b" * 64,
            "observed_at": (f.now - timedelta(minutes=1)).isoformat(),
        },
    }
    data.update(changes)
    return parse_command(json.dumps(data).encode())


def decision_command(f, **changes):
    data = {
        "command_id": str(uuid4()),
        "action": "record_collection_decision",
        "collection_id": f.collection,
        "expected_revision": 0,
        "reason": "Original synthetic rights decision",
        "payload": {
            "evidence_id": str(f.evidence),
            "valid_from": (f.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (f.now + timedelta(hours=1)).isoformat(),
            "state": "approved",
            "basis_type": "original_authorship",
            "conditions_satisfied": True,
            "metadata_privacy_class": "no_personal_data",
            "privacy_reason": "Original synthetic metadata",
            "redistribute_metadata": "allow",
        },
    }
    data.update(changes)
    return parse_command(json.dumps(data).encode())


def verification_command(f, **changes):
    data = {
        "command_id": str(uuid4()),
        "action": "record_verification_review",
        "collection_id": f.collection,
        "expected_revision": 0,
        "reason": "Original synthetic content review",
        "payload": {
            "evidence_id": str(f.evidence),
            "valid_from": (f.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (f.now + timedelta(hours=1)).isoformat(),
            "state": "approved",
            "sample_numerator": 1,
            "sample_denominator": 1,
            "escalation_rule": "Hold uncertain material",
            "materials": [
                {
                    "material_class": kind,
                    "method": "human_source_comparison",
                    "sample_numerator": 1,
                    "sample_denominator": 1,
                    "competence_evidence_id": str(f.evidence),
                    "escalation_rule": "Hold uncertain material",
                    "exclusion_rule": "Exclude unverified material",
                }
                for kind in sorted(MATERIALS)
            ],
        },
    }
    data.update(changes)
    return parse_command(json.dumps(data).encode())


def apply(f, subject, cmd):
    with subject.engine.begin() as conn:
        return OperatorCommands(conn, f.ns).apply(cmd)


def test_native_authentication_and_atomic_evidence_retry(operators):
    f, create, _ = operators
    reviewer = create("rights")
    bad = create_engine(reviewer.engine.url.set(password="wrong-password"), poolclass=NullPool)
    try:
        with pytest.raises(OperationalError), bad.connect():
            pass
    finally:
        bad.dispose()
    cmd = evidence_command(f)
    first, retry = apply(f, reviewer, cmd), apply(f, reviewer, cmd)
    assert first.status == retry.status == "applied"
    assert not first.replayed and retry.replayed and retry.revision == 1
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 1
        row = conn.execute(
            text(f"SELECT actor_id,evidence_id FROM {f.name(conn, 'policy', 'audit_event')}")
        ).one()
        assert row.actor_id == reviewer.actor and row.evidence_id == cmd.payload.id
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'operator_event')}")) == 2


def test_same_person_cannot_register_a_second_approval_identity(operators):
    f, create, _ = operators
    first = create("rights")
    with pytest.raises(IntegrityError):
        create("content", subject_key=first.appointment.subject_key)
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT count(*) FROM {f.name(conn, 'policy', 'actor')} WHERE database_role_name IS NOT NULL"
                )
            )
            == 1
        )


def test_role_and_collection_authority_are_derived_not_supplied(operators):
    f, create, _ = operators
    rights, content = create("rights"), create("content")
    assert apply(f, content, decision_command(f)).status == "forbidden"
    assert apply(f, rights, verification_command(f)).status == "forbidden"
    assert apply(f, rights, evidence_command(f, collection_id=f.other_collection)).status == "forbidden"
    with f.as_role("review") as conn:
        assert OperatorCommands(conn, f.ns).apply(evidence_command(f)).status == "forbidden"
    cmd = decision_command(f)
    assert apply(f, rights, cmd).status == "applied"
    assert apply(f, content, cmd).status == "forbidden"
    with rights.engine.begin() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        conn.execute(text(f"SET ROLE {quote(f.roles['review'])}"))
        assert conn.scalar(text(f"SELECT {f.name(conn, 'policy', 'current_actor')}()")) == rights.actor
    with pytest.raises(DBAPIError), rights.engine.begin() as conn:
        conn.execute(text(f"SET ROLE {quote(content.role)}"))


def test_independent_collection_reviews_are_atomic_and_do_not_publish(operators):
    f, create, _ = operators
    rights, content = create("rights"), create("content")
    assert apply(f, rights, decision_command(f)).status == "applied"
    review = verification_command(f)
    assert apply(f, content, review).status == "applied"
    assert apply(f, content, review).replayed
    with f.as_role("release") as conn:
        assert PolicyRepository(conn, f.ns).lock_approval_inputs(
            f.collection, rights_revision=1, content_revision=1
        )
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_text").allowed
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 8
        assert (
            conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'verification_material')}")) == 6
        )
        assert conn.scalar(text("SELECT count(*) FROM versions")) == 0


def test_one_operator_holding_both_roles_cannot_supply_independent_review(operators):
    f, create, _ = operators
    both = create("rights", assignments=((f.collection, "rights"), (f.collection, "content")))
    assert apply(f, both, decision_command(f)).status == "applied"
    assert apply(f, both, verification_command(f)).status == "applied"
    with pytest.raises(DBAPIError) as denied, f.as_role("release") as conn:
        PolicyRepository(conn, f.ns).lock_approval_inputs(f.collection, rights_revision=1, content_revision=1)
    assert denied.value.orig.sqlstate == "42501"


def test_verification_failure_rolls_back_policy_materials_audit_and_receipt(operators):
    f, create, _ = operators
    content = create("content")
    cmd = verification_command(f)
    data = cmd.model_dump(mode="json")
    data["payload"]["materials"][-1]["competence_evidence_id"] = str(uuid4())
    bad = parse_command(json.dumps(data).encode())
    with content.engine.begin() as conn:
        assert OperatorCommands(conn, f.ns).apply(bad).status == "validation_failed"
        assert conn.scalar(text("SELECT 1")) == 1  # Savepoint preserves caller transaction.
    with f.engine.connect() as conn:
        for table in (
            "verification_policy",
            "verification_material",
            "audit_event",
            "command_receipt",
            "revision_head",
        ):
            assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', table)}")) == 0
    assert apply(f, content, cmd).status == "applied"


def test_outer_rollback_leaves_no_evidence_or_retry_receipt(operators):
    f, create, _ = operators
    rights = create("rights")
    cmd = evidence_command(f)
    with rights.engine.connect() as conn:
        transaction = conn.begin()
        assert OperatorCommands(conn, f.ns).apply(cmd).status == "applied"
        transaction.rollback()
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 0
    assert not apply(f, rights, cmd).replayed


def test_changed_retry_and_stale_revision_have_no_new_side_effects(operators):
    f, create, _ = operators
    rights = create("rights")
    cmd = decision_command(f)
    assert apply(f, rights, cmd).status == "applied"
    changed = cmd.model_copy(update={"reason": "Changed intent"})
    assert apply(f, rights, changed).status == "conflict"
    assert apply(f, rights, decision_command(f)).status == "conflict"
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 1
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 1


@pytest.mark.parametrize(
    "mode",
    ["revoke", "expire", "rename", "nologin", "credential_expired", "schema_create", "unsafe_membership"],
)
def test_invalidated_native_session_cannot_issue_or_retry_commands(operators, mode):
    f, create, _ = operators
    rights = create("rights")
    cmd = decision_command(f)
    assert apply(f, rights, cmd).status == "applied"
    with rights.engine.connect() as live:
        assert live.scalar(text("SELECT session_user")) == rights.role
        live.commit()
        with f.engine.begin() as conn:
            quote = conn.dialect.identifier_preparer.quote_identifier
            if mode == "revoke":
                revoke_operator(conn, f.ns, rights.actor, reason="Original synthetic revocation")
            elif mode == "expire":
                conn.execute(
                    text(
                        f"UPDATE {f.name(conn, 'policy', 'actor')} SET authenticated_until=statement_timestamp()-interval '1 second' WHERE id=:actor"
                    ),
                    {"actor": rights.actor},
                )
            elif mode == "rename":
                conn.execute(
                    text(f"ALTER ROLE {quote(rights.role)} RENAME TO {quote(rights.role + '_renamed')}")
                )
            elif mode == "nologin":
                conn.execute(text(f"ALTER ROLE {quote(rights.role)} NOLOGIN"))
            elif mode == "credential_expired":
                conn.execute(text(f"ALTER ROLE {quote(rights.role)} VALID UNTIL '2000-01-01T00:00:00Z'"))
            elif mode == "schema_create":
                conn.execute(
                    text(f"GRANT CREATE ON SCHEMA {quote(f.ns.schema('policy'))} TO {quote(rights.role)}")
                )
            else:
                conn.execute(text(f"GRANT {quote(f.roles['guard'])} TO {quote(rights.role)}"))
        with live.begin():
            assert OperatorCommands(live, f.ns).apply(cmd).status == "forbidden"
        if mode == "rename":
            with f.engine.begin() as conn:
                conn.execute(
                    text(f"ALTER ROLE {quote(rights.role + '_renamed')} RENAME TO {quote(rights.role)}")
                )


@pytest.mark.parametrize("retry", [False, True])
def test_concurrent_commands_have_one_effect_and_safe_retry(operators, retry):
    f, create, _ = operators
    rights = create("rights")
    first = decision_command(f)
    second = first if retry else decision_command(f)
    barrier = Barrier(2)

    def run(cmd):
        with rights.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='4s'"))
            barrier.wait(timeout=3)
            return OperatorCommands(conn, f.ns).apply(cmd)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, cmd) for cmd in (first, second)]
        results = [future.result(timeout=8) for future in futures]
    assert sorted(r.status for r in results) == (["applied", "applied"] if retry else ["applied", "conflict"])
    if retry:
        assert sorted(r.replayed for r in results) == [False, True]
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'command_receipt')}")) == 1
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 1


def test_cli_uses_native_subject_and_never_echoes_private_input(operators):
    f, create, _ = operators
    rights = create("rights")
    cmd = evidence_command(f)
    env = dict(
        os.environ, ALVARY_OPERATOR_DATABASE_URL=rights.engine.url.render_as_string(hide_password=False)
    )
    completed = subprocess.run(
        [sys.executable, "-m", "rights.cli", "--schema", f.ns.base],
        input=cmd.model_dump_json(),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "applied"
    assert not completed.stderr
    assert rights.password not in completed.stdout and cmd.payload.private_reference not in completed.stdout


def test_command_denies_frozen_shadow_authority(operators):
    f, create, _ = operators
    rights = create("rights")
    with f.engine.begin() as conn:
        freeze(conn, expected_epoch=0)
    assert apply(f, rights, evidence_command(f)).status == "unavailable"


def test_registered_operator_downgrade_cannot_discard_bindings(operators):
    f, create, _ = operators
    rights = create("rights")
    with pytest.raises(RuntimeError, match="requires forward repair"), f.engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0003")
    with f.engine.connect() as conn:
        assert conn.scalar(
            text(f"SELECT active FROM {f.name(conn, 'policy', 'actor')} WHERE id=:id"), {"id": rights.actor}
        )
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004"


def test_recreated_login_name_cannot_recover_old_actor_authority(operators):
    f, create, _ = operators
    rights = create("rights")
    with f.engine.begin() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        conn.execute(text(f"DROP OWNED BY {quote(rights.role)}"))
        conn.execute(text(f"DROP ROLE {quote(rights.role)}"))
        conn.connection.driver_connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(rights.role), sql.Literal(rights.password)
            )
        )
        conn.execute(text(f"GRANT {quote(f.roles['review'])} TO {quote(rights.role)}"))
    assert apply(f, rights, evidence_command(f)).status == "forbidden"


def test_database_command_rejects_forged_envelope_and_private_receipt_mutation(operators):
    f, create, _ = operators
    rights = create("rights")
    data = evidence_command(f).model_dump(mode="json")
    data["actor_id"] = str(f.actors["review"])
    with pytest.raises(DBAPIError) as invalid, rights.engine.begin() as conn:
        fn = f.name(conn, "policy", "apply_operator_command")
        conn.execute(text(f"SELECT * FROM {fn}(CAST(:data AS jsonb))"), {"data": json.dumps(data)})
    assert invalid.value.orig.sqlstate == "22023"
    for table in ("actor", "assignment", "command_receipt", "operator_event"):
        with pytest.raises(DBAPIError) as forbidden, rights.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {f.name(conn, 'policy', table)}"))
        assert forbidden.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as immutable, f.engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {f.name(conn, 'policy', 'operator_event')}"))
    assert immutable.value.orig.sqlstate == "55000"


def test_registration_and_binding_changes_cannot_lose_identity_history(operators):
    f, create, _ = operators
    rights = create("rights")
    with pytest.raises(IntegrityError), f.engine.begin() as conn:
        conn.execute(
            text(f"UPDATE {f.name(conn, 'policy', 'actor')} SET subject_key='another-person' WHERE id=:id"),
            {"id": rights.actor},
        )
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT subject_key FROM {f.name(conn, 'policy', 'actor')} WHERE id=:id"),
                {"id": rights.actor},
            )
            == rights.appointment.subject_key
        )
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'operator_event')}")) == 2


def test_revocation_waits_for_current_transaction_then_rejects_new_commands(operators):
    f, create, _ = operators
    rights = create("rights")
    started = Event()
    revocation_pid = []

    def revoke():
        with f.engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='4s'"))
            revocation_pid.append(conn.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            revoke_operator(conn, f.ns, rights.actor, reason="Synthetic concurrent revocation")
        return True

    with ThreadPoolExecutor(max_workers=1) as pool, rights.engine.connect() as conn:
        transaction = conn.begin()
        assert OperatorCommands(conn, f.ns).apply(evidence_command(f)).status == "applied"
        future = pool.submit(revoke)
        assert started.wait(timeout=2)
        # Observe the independent administrator actually waiting on our transaction.
        deadline = monotonic() + 2
        waiting = False
        with f.engine.connect() as probe:
            while monotonic() < deadline:
                waiting = probe.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                    {"pid": revocation_pid[0]},
                )
                if waiting:
                    break
                sleep(0.01)
        assert waiting
        transaction.commit()
        assert future.result(timeout=5)
    assert apply(f, rights, evidence_command(f)).status == "forbidden"
    with f.engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'operator_event')}")) == 3


def test_upgrade_with_existing_capability_owners_preserves_pilot_and_subjects(pg_engine):
    migrate(pg_engine, "0003")
    with pg_engine.begin() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        roles = provision_roles(conn, ns)
    try:
        migrate(pg_engine)
        with pg_engine.connect() as conn:
            fn = ns.qualified(conn, "policy", "apply_operator_command")
            assert (
                conn.scalar(
                    text("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid=to_regprocedure(:name)"),
                    {"name": fn + "(jsonb)"},
                )
                == roles["guard"]
            )
            assert conn.scalar(
                text("SELECT has_function_privilege(:role,:fn,'EXECUTE')"),
                {"role": roles["review"], "fn": fn + "(jsonb)"},
            )
            assert not conn.scalar(
                text("SELECT has_schema_privilege(:role,:schema,'CREATE')"),
                {"role": roles["guard"], "schema": ns.schema("policy")},
            )
        with pg_engine.begin() as conn:
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.downgrade(config, "0003")
            table = ns.qualified(conn, "policy", "actor")
            assert not conn.scalar(
                text("SELECT has_column_privilege(:role,:table,'active','UPDATE')"),
                {"role": roles["guard"], "table": table},
            )
        migrate(pg_engine)
    finally:
        with pg_engine.begin() as conn:
            quote = conn.dialect.identifier_preparer.quote_identifier
            for kind in ("policy", "staging", "corpus"):
                conn.execute(text(f"DROP SCHEMA {quote(ns.schema(kind))} CASCADE"))
            for role in roles.values():
                conn.execute(text(f"DROP OWNED BY {quote(role)}"))
                conn.execute(text(f"DROP ROLE {quote(role)}"))


def test_upgrade_refuses_unrelated_guard_role_without_partial_ddl(pg_engine):
    migrate(pg_engine, "0003")
    with pg_engine.begin() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        guard = ns.role("guard")
        quote = conn.dialect.identifier_preparer.quote_identifier
        conn.execute(text(f"CREATE ROLE {quote(guard)} NOLOGIN"))
    try:
        with pytest.raises(RuntimeError, match="boundary review"):
            migrate(pg_engine)
        with pg_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
            assert not conn.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema=:schema AND table_name='actor' AND column_name='database_role_name')"
                ),
                {"schema": ns.schema("policy")},
            )
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text(f"DROP ROLE {quote(guard)}"))


def test_maintenance_login_is_not_an_operator_command_identity(operators):
    f, _, _ = operators
    with f.engine.begin() as conn:
        assert OperatorCommands(conn, f.ns).apply(evidence_command(f)).status == "forbidden"


def test_scope_removal_is_audited_and_denies_command_receipt_replay(operators):
    f, create, _ = operators
    rights = create("rights")
    cmd = evidence_command(f)
    assert apply(f, rights, cmd).status == "applied"
    with f.engine.begin() as conn:
        conn.execute(
            text(
                f"DELETE FROM {f.name(conn, 'policy', 'assignment')} WHERE actor_id=:actor AND collection_id=:scope"
            ),
            {"actor": rights.actor, "scope": f.collection},
        )
    assert apply(f, rights, cmd).status == "forbidden"
    with f.engine.connect() as conn:
        event = conn.execute(
            text(
                f"SELECT administrator,collection_id,capability FROM {f.name(conn, 'policy', 'operator_event')} WHERE event_kind='assignment_removed'"
            )
        ).one()
        assert event.collection_id == f.collection and event.capability == "rights"
        assert event.administrator == conn.scalar(text("SELECT session_user"))


def test_old_command_retry_cannot_restore_a_superseded_permission(operators):
    f, create, _ = operators
    rights = create("rights")
    first = decision_command(f)
    assert apply(f, rights, first).status == "applied"
    data = first.model_dump(mode="json")
    data.update(command_id=str(uuid4()), expected_revision=1, reason="Original synthetic denial")
    data["payload"]["redistribute_metadata"] = "deny"
    second = parse_command(json.dumps(data).encode())
    assert apply(f, rights, second).revision == 2
    assert apply(f, rights, first).replayed
    with f.as_role("serving") as conn:
        assert not PolicyRepository(conn, f.ns).public_decision(f.collection, "redistribute_metadata").allowed
    with f.engine.connect() as conn:
        assert (
            conn.scalar(
                text(
                    f"SELECT revision FROM {f.name(conn, 'policy', 'revision_head')} WHERE kind='collection' AND scope_id=:id"
                ),
                {"id": f.collection},
            )
            == 2
        )
        assert conn.scalar(text(f"SELECT count(*) FROM {f.name(conn, 'policy', 'audit_event')}")) == 2
