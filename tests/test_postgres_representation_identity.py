"""Native additive identity transition; immutable old IDs and receipts survive."""

import json
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_acquisition import PURPOSE
from test_postgres_acquisition import chain as chain
from test_postgres_approval import approve, rows
from test_postgres_approval import ready as ready
from test_postgres_operators import operators as operators
from test_postgres_policy import foundation as foundation

from rights.acquisition import SyntheticAcquisition
from rights.approval import SyntheticApproval
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres


def rollback(engine, revision):
    with engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def fingerprint(f):
    with f.engine.connect() as conn:
        return conn.execute(
            text("SELECT oid,proowner,proacl FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
            {"fn": f.ns.schema("policy") + ".apply_synthetic_approval(jsonb)"},
        ).one()


def test_populated_legacy_id_upgrade_preserves_retries_references_and_erasure(ready):
    f, _, worker, _, maintainer, _, artifact, *rest = ready
    rollback(f.engine, "0012")
    old_command = rest[-2].model_copy(update={"representation_id": "prp_" + str(uuid4())})
    result = approve(f, maintainer, old_command)
    assert result.status == "applied"
    tables = [
        "approved_version",
        "representation_revision",
        "version_node",
        "approved_anchor",
        "representation_head",
        "version_head",
        "document_order",
        "unavailable_mapping",
    ]
    before = {t: rows(f, t) for t in tables}
    old_identity = fingerprint(f)
    migrate(f.engine)
    assert fingerprint(f) == old_identity
    for t, records in before.items():
        assert rows(f, t) == records
    retried = approve(f, maintainer, old_command)
    assert retried.replayed and retried.representation_id == result.representation_id
    with worker.engine.begin() as conn:
        view = SyntheticApproval(conn, f.ns).read(result.version_id, purpose=PURPOSE)
        assert view and view.current_rep_id == result.representation_id
    # A new command against a committed snapshot reuses the actual identity; no remint occurs.
    retry = old_command.model_copy(update={"command_id": uuid4(), "representation_id": "rep_" + str(uuid4())})
    repeated = approve(f, maintainer, retry)
    assert repeated.replayed and repeated.representation_id == result.representation_id
    rollback(f.engine, "0012")
    assert fingerprint(f) == old_identity
    for t, records in before.items():
        assert rows(f, t) == records
    migrate(f.engine)
    with worker.engine.begin() as conn:
        assert SyntheticAcquisition(conn, f.ns).erase(artifact)
    rep = rows(f, "representation_revision")[0]
    assert rep["id"] == result.representation_id and rep["state"] == "erased" and rep["payload"] is None


def test_new_legacy_prefix_is_denied_by_rpc_and_native_insert_trigger(ready):
    f, _, _, _, maintainer, *rest = ready
    cmd = rest[-2].model_copy(update={"representation_id": "prp_" + str(uuid4())})
    assert approve(f, maintainer, cmd).status == "validation_failed"
    assert not rows(f, "approved_version") and not rows(f, "representation_revision")
    # The historic approval alias cannot bypass the new-row identity guard either.
    with maintainer.engine.begin() as conn:
        with pytest.raises(DBAPIError) as failure, conn.begin_nested():
            conn.execute(
                text(
                    f"SELECT {f.ns.qualified(conn, 'policy', 'apply_synthetic_approval_m33')}(CAST(:body AS jsonb))"
                ),
                dict(body=cmd.model_dump_json()),
            )
        assert failure.value.orig.sqlstate in {"23514", "42501"}

    result = approve(f, maintainer, rest[-2])
    assert result.status == "applied" and result.representation_id.startswith("rep_")
    row = rows(f, "representation_revision")[0]
    row.update(id="prp_" + str(uuid4()))
    with f.engine.begin() as conn:
        columns = ",".join(row)
        values = ",".join(":" + k for k in row)
        row["payload"] = json.dumps(row["payload"])
        with pytest.raises(DBAPIError) as failure, conn.begin_nested():
            conn.execute(
                text(
                    f"INSERT INTO {f.ns.qualified(conn, 'corpus', 'representation_revision')}({columns}) VALUES({values})"
                ),
                row,
            )
        assert failure.value.orig.sqlstate == "23514"
    with pytest.raises(RuntimeError, match="forward repair"):
        rollback(f.engine, "0012")
    assert rows(f, "representation_revision")[0]["id"] == result.representation_id


def test_empty_identity_rollback_restores_exact_full_definition_and_trigger_bindings(pg_engine):
    migrate(pg_engine)
    rollback(pg_engine, "0012")
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        fn = ns.schema("policy") + ".apply_synthetic_approval(jsonb)"
        prior = conn.scalar(text("SELECT pg_get_functiondef(to_regprocedure(:fn))"), dict(fn=fn))
        trigger_oids = conn.execute(
            text("SELECT oid,tgfoid FROM pg_trigger WHERE NOT tgisinternal ORDER BY oid")
        ).all()
    migrate(pg_engine)
    rollback(pg_engine, "0012")
    with pg_engine.connect() as conn:
        assert conn.scalar(text("SELECT pg_get_functiondef(to_regprocedure(:fn))"), dict(fn=fn)) == prior
        assert (
            conn.execute(text("SELECT oid,tgfoid FROM pg_trigger WHERE NOT tgisinternal ORDER BY oid")).all()
            == trigger_oids
        )
    migrate(pg_engine)


@pytest.mark.parametrize("corruption", ["constraint", "function"])
def test_identity_upgrade_refuses_unknown_authority_before_ddl(pg_engine, corruption):
    migrate(pg_engine)
    rollback(pg_engine, "0012")
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        conn.rollback()
        tx = conn.begin()
        if corruption == "constraint":
            conn.execute(
                text(
                    f"ALTER TABLE {ns.qualified(conn, 'corpus', 'representation_revision')} DROP CONSTRAINT representation_revision_id_check"
                )
            )
        else:
            conn.execute(
                text(
                    f"ALTER FUNCTION {ns.qualified(conn, 'policy', 'apply_synthetic_approval')}(jsonb) RESET search_path"
                )
            )
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="boundary review"):
            command.upgrade(cfg, "head")
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0012"
        assert (
            conn.scalar(
                text("SELECT to_regprocedure(:fn)"),
                dict(fn=ns.schema("policy") + ".representation_identity_insert()"),
            )
            is None
        )
        tx.rollback()
    migrate(pg_engine)


def test_current_resource_verifier_tracks_replacement_and_new_trigger(pg_engine, capsys):
    migrate(pg_engine)
    from scripts.verify_sql_resources import verify

    with pg_engine.connect() as conn:
        verify(conn)
    assert "117 installed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "tamper",
    [
        "GRANT EXECUTE ON FUNCTION {policy}.apply_operator_command(jsonb) TO PUBLIC",
        "CREATE FUNCTION {policy}.unreviewed() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    ],
)
def test_resource_verifier_rejects_public_execute_and_unreviewed_functions(pg_engine, tamper):
    migrate(pg_engine)
    from scripts.verify_sql_resources import verify

    with pg_engine.connect() as conn:
        policy = PolicyNamespace(conn.scalar(text("SELECT current_schema()"))).qualified(conn, "policy", "x")
        conn.execute(text(tamper.format(policy=policy.rsplit(".", 1)[0])))
        with pytest.raises(ValueError):
            verify(conn)
        conn.rollback()


def test_resource_verifier_accepts_provisioned_guard_and_rejects_stray_grantee(foundation, capsys):
    from scripts.verify_sql_resources import verify

    f = foundation
    with f.engine.connect() as conn:
        verify(conn)
        assert "owners and EXECUTE grantees match" in capsys.readouterr().out
        stray = "stray_" + uuid4().hex
        conn.execute(text(f'CREATE ROLE "{stray}" NOLOGIN'))
        fn = f.ns.qualified(conn, "policy", "apply_operator_command")
        conn.execute(text(f'GRANT EXECUTE ON FUNCTION {fn}(jsonb) TO "{stray}"'))
        with pytest.raises(ValueError, match="EXECUTE grant"):
            verify(conn)
        conn.rollback()


def test_new_insert_guard_has_no_runtime_execute_or_schema_create_authority(ready):
    f, _, worker, editor, maintainer, *_ = ready
    with f.engine.connect() as conn:
        fn = f.ns.schema("policy") + ".representation_identity_insert()"
        row = conn.execute(
            text("SELECT proowner,prosecdef,proconfig FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
            dict(fn=fn),
        ).one()
        assert row.prosecdef and row.proconfig == ["search_path=pg_catalog"]
        assert row.proowner == conn.scalar(
            text("SELECT oid FROM pg_roles WHERE rolname=:role"), dict(role=f.ns.role("guard"))
        )
        for role in [f.ns.role(k) for k in ("serving", "acquisition", "review", "release")] + [
            worker.role,
            editor.role,
            maintainer.role,
        ]:
            assert not conn.scalar(
                text("SELECT has_function_privilege(:role,:fn,'EXECUTE')"), dict(role=role, fn=fn)
            )
        assert not conn.scalar(
            text("SELECT has_schema_privilege(:role,:schema,'CREATE')"),
            dict(role=f.ns.role("guard"), schema=f.ns.schema("policy")),
        )


def test_existing_guard_upgrade_works_with_nonsuperuser_native_migration_owner(ready):
    from secrets import token_hex

    from psycopg import sql
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    f, *_ = ready
    rollback(f.engine, "0012")
    role = "migration_" + uuid4().hex
    password = token_hex(24)
    with f.engine.begin() as conn:
        owner = conn.scalar(text("SELECT session_user"))
        native = conn.connection.driver_connection
        native.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
        # Inherit ordinary schema/table/function ownership, without SET ROLE escalation.
        native.execute(
            sql.SQL("GRANT {} TO {} WITH SET FALSE").format(sql.Identifier(owner), sql.Identifier(role))
        )
        native.execute(
            sql.SQL("GRANT {} TO {}").format(sql.Identifier(f.ns.role("guard")), sql.Identifier(role))
        )
    engine = create_engine(
        f.engine.url.set(username=role, password=password),
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={f.ns.base}"},
    )
    try:
        with engine.connect() as conn:
            assert not conn.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=session_user"))
        migrate(engine)
        with f.engine.connect() as conn:
            assert not conn.scalar(
                text("SELECT has_schema_privilege(:role,:schema,'CREATE')"),
                dict(role=f.ns.role("guard"), schema=f.ns.schema("policy")),
            )
            assert conn.scalar(
                text(
                    "SELECT proowner=(SELECT oid FROM pg_roles WHERE rolname=:role) FROM pg_proc WHERE oid=to_regprocedure(:fn)"
                ),
                dict(role=f.ns.role("guard"), fn=f.ns.schema("policy") + ".representation_identity_insert()"),
            )
    finally:
        engine.dispose()
        with f.engine.begin() as conn:
            quote = conn.dialect.identifier_preparer.quote_identifier
            conn.execute(text("DROP OWNED BY " + quote(role)))
            conn.execute(text("DROP ROLE " + quote(role)))


def test_sql_bundle_tool_pins_an_edited_function_without_touching_the_database(
    pg_engine, tmp_path, monkeypatch
):
    import shutil

    from scripts import sql_bundle

    migrate(pg_engine)
    (tmp_path / "v0013").mkdir()
    shutil.copy(sql_bundle.ROOT / "v0013" / "manifest.json", tmp_path / "v0013")
    monkeypatch.setattr(sql_bundle, "ROOT", tmp_path)
    monkeypatch.setattr(sql_bundle, "bundle_hash", lambda revision: "pinned")
    signature = "apply_operator_command(jsonb)"
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        before = fingerprint_of(conn, ns, signature)
        sql_bundle.init(conn, ns, "v0014", [signature])
        conn.rollback()
        source = tmp_path / "v0014" / "apply_operator_command.sql"
        edited = source.read_text().replace("BEGIN", "BEGIN\n    -- reviewed edit", 1)
        source.write_text(edited.replace("CREATE OR REPLACE FUNCTION", "create or replace function", 1))
        with pytest.raises(SystemExit, match="normalized"):
            sql_bundle.pin(conn, ns, "v0014")
        assert source.read_text() == edited
        sql_bundle.pin(conn, ns, "v0014")
        assert fingerprint_of(conn, ns, signature) == before
    entry = json.loads((tmp_path / "v0014" / "manifest.json").read_text())["functions"][0]
    assert entry["prior_definition_hash"] != entry["current_definition_hash"]
    assert not (tmp_path / "v0014" / "draft.json").exists()


def fingerprint_of(conn, ns, signature):
    return conn.execute(
        text(
            "SELECT oid,proowner,proacl,md5(pg_get_functiondef(oid)) FROM pg_proc WHERE oid=to_regprocedure(:fn)"
        ),
        {"fn": ns.qualified(conn, "policy", "x").rsplit(".", 1)[0] + "." + signature},
    ).one()
