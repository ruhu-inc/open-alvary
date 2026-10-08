"""Gate public metadata on an instance-bound serving epoch and restore recovery (SYS-07)."""

import json
from hashlib import sha256
from importlib.resources import files

from alembic import op
from sqlalchemy import text

from rights.authority import assert_guard_boundary
from rights.database import PolicyNamespace
from rights.sql_resources import bundle_hash, context, install, normalize_body, render

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None
RESOURCE_HASH = "92855570806d839d146b6d5c41188d166ba260c3002b39ae37c4af83c26c1d1c"
TABLES = ("instance_binding", "serving_epoch", "recovery_record", "recovery_floor")


def prepare():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    if bundle_hash("v0015") != RESOURCE_HASH:
        raise RuntimeError("Applied SQL revision resources changed; use a new revision")
    root = files("rights").joinpath("sql/v0015")
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    return conn, ns, context(conn, ns, manifest["literals"]), root, manifest["new_functions"]


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, tokens, root, entries = prepare()
    policy = tokens["policy"]
    assert_guard_boundary(conn, ns)
    existing = [
        conn.scalar(text("SELECT to_regprocedure(:fn)"), {"fn": policy + "." + e["signature"]})
        for e in entries
    ] + [conn.scalar(text("SELECT to_regclass(:t)"), {"t": policy + "." + t}) for t in TABLES]
    if any(existing):
        raise RuntimeError("Unexpected serving epoch authority requires boundary review")
    conn.exec_driver_sql("SET LOCAL check_function_bodies=on")
    conn.exec_driver_sql(
        render(root.joinpath("recovery.sql").read_text(), tokens), execution_options={"no_parameters": True}
    )
    for entry in entries:
        definition = conn.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure(:fn))"), {"fn": policy + "." + entry["signature"]}
        )
        if (
            sha256(normalize_body(definition, tokens).encode()).hexdigest()
            != entry["current_definition_hash"]
        ):
            raise RuntimeError("Installed serving epoch authority differs from reviewed SQL")
    # The database this migration runs on is the bound instance; restores and clones are not.
    conn.exec_driver_sql(
        f"INSERT INTO {policy}.instance_binding(system_identifier,database_oid)"
        " SELECT (SELECT system_identifier FROM pg_catalog.pg_control_system()),"
        " (SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())"
    )
    guard = tokens["role_guard"]
    provisioned = conn.scalar(text("SELECT to_regrole(:role)"), {"role": ns.role("guard")}) is not None
    if provisioned:
        conn.exec_driver_sql(
            f"GRANT SELECT ON {policy}.instance_binding,{policy}.serving_epoch,{policy}.recovery_floor TO {guard}"
        )
        conn.exec_driver_sql(f"GRANT CREATE ON SCHEMA {policy} TO {guard}")
        for entry in entries:
            conn.exec_driver_sql(f"ALTER FUNCTION {policy}.{entry['signature']} OWNER TO {guard}")
        conn.exec_driver_sql(f"REVOKE CREATE ON SCHEMA {policy} FROM {guard}")
    install(conn, ns, "v0015", RESOURCE_HASH)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, tokens, root, _ = prepare()
    policy = tokens["policy"]
    if conn.scalar(
        text(
            f"SELECT EXISTS(SELECT 1 FROM {policy}.serving_epoch) OR EXISTS(SELECT 1 FROM {policy}.recovery_record)"
        )
    ):
        raise RuntimeError("Serving epoch and recovery history require forward repair")
    install(conn, ns, "v0015", RESOURCE_HASH, downgrade=True)
    conn.exec_driver_sql(
        render(root.joinpath("downgrade.sql").read_text(), tokens), execution_options={"no_parameters": True}
    )
