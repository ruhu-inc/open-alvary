"""Commit every denial with a pending journal delivery record (SYS-07 step 1, LIF-02 allocator)."""

import json
from hashlib import sha256
from importlib.resources import files

from alembic import op
from sqlalchemy import text

from rights.authority import assert_guard_boundary
from rights.database import PolicyNamespace
from rights.sql_resources import bundle_hash, context, normalize_body, render

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None
RESOURCE_HASH = "40fbf6100ac4439056c871be1c7b825a0882785ef1f70aa1dc374c76ef51712e"
TABLES = ("denial_delivery", "publication_revision")


def prepare():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    if bundle_hash("v0014") != RESOURCE_HASH:
        raise RuntimeError("Applied SQL revision resources changed; use a new revision")
    root = files("rights").joinpath("sql/v0014")
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    return conn, ns, context(conn, ns, manifest["literals"]), root, manifest["new_functions"]


def present(conn, tokens, entries):
    functions = [
        conn.scalar(text("SELECT to_regprocedure(:fn)"), {"fn": tokens["policy"] + "." + e["signature"]})
        for e in entries
    ]
    tables = [conn.scalar(text("SELECT to_regclass(:t)"), {"t": tokens["policy"] + "." + t}) for t in TABLES]
    return [x for x in functions + tables if x is not None]


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns, tokens, root, entries = prepare()
    assert_guard_boundary(conn, ns)
    if present(conn, tokens, entries):
        raise RuntimeError("Unexpected denial delivery authority requires boundary review")
    conn.exec_driver_sql("SET LOCAL check_function_bodies=on")
    conn.exec_driver_sql(
        render(root.joinpath("journal.sql").read_text(), tokens), execution_options={"no_parameters": True}
    )
    for entry in entries:
        definition = conn.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure(:fn))"),
            {"fn": tokens["policy"] + "." + entry["signature"]},
        )
        if (
            sha256(normalize_body(definition, tokens).encode()).hexdigest()
            != entry["current_definition_hash"]
        ):
            raise RuntimeError("Installed denial delivery authority differs from reviewed SQL")
    if conn.scalar(text("SELECT to_regrole(:role)"), {"role": ns.role("guard")}) is not None:
        # Provisioned namespaces receive the same grants as fresh provisioning (provision/v0014.sql);
        # PostgreSQL requires schema CREATE for the ownership transfer, revoked before commit.
        policy, guard = tokens["policy"], tokens["role_guard"]
        conn.exec_driver_sql(f"GRANT SELECT,INSERT ON {policy}.denial_delivery TO {guard}")
        conn.exec_driver_sql(f"GRANT SELECT,UPDATE ON {policy}.publication_revision TO {guard}")
        conn.exec_driver_sql(f"GRANT CREATE ON SCHEMA {policy} TO {guard}")
        for entry in entries:
            conn.exec_driver_sql(f"ALTER FUNCTION {policy}.{entry['signature']} OWNER TO {guard}")
        conn.exec_driver_sql(f"REVOKE CREATE ON SCHEMA {policy} FROM {guard}")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, _, tokens, root, _ = prepare()
    policy = tokens["policy"]
    if conn.scalar(
        text(
            f"SELECT EXISTS(SELECT 1 FROM {policy}.denial_delivery)"
            f" OR (SELECT revision FROM {policy}.publication_revision) > 0"
        )
    ):
        raise RuntimeError("Recorded denials require forward repair; delivery history cannot be discarded")
    conn.exec_driver_sql(
        render(root.joinpath("downgrade.sql").read_text(), tokens), execution_options={"no_parameters": True}
    )
