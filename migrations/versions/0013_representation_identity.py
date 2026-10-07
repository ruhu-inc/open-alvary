"""Use representation identities for new rows; retain immutable legacy references."""

import json
from hashlib import sha256
from importlib.resources import files

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import bundle_hash, context, install, normalize_body, render

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
RESOURCE_HASH = "0dbb258299753290d53cc56cf5b77e0aab0e9b8a87df19d505c043cdae0c21bf"
SUFFIX = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def assert_constraint(conn, tokens, downgrade):
    prefix = "(rep|prp)" if downgrade else "prp"
    expected = "CHECK ((id ~ '^" + prefix + "_" + SUFFIX + "'::text))"
    constraint = conn.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname='representation_revision_id_check'"
        ),
        {"table": tokens["corpus"] + ".representation_revision"},
    )
    if constraint != expected:
        raise RuntimeError("Representation identity requires boundary review")
    if downgrade and conn.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM "
            + tokens["corpus"]
            + ".representation_revision WHERE id LIKE 'rep_%')"
        )
    ):
        raise RuntimeError("Representation identities require forward repair; history cannot be rewritten")


def assert_insert_authority(conn, ns, tokens, entry, downgrade):
    fn = conn.execute(
        text(
            "SELECT oid,proowner,pg_get_functiondef(oid) definition FROM pg_proc WHERE oid=to_regprocedure(:fn)"
        ),
        {"fn": tokens["policy"] + ".representation_identity_insert()"},
    ).one_or_none()
    trigger = conn.execute(
        text(
            "SELECT tgfoid,tgtype,tgenabled FROM pg_trigger WHERE tgrelid=to_regclass(:table) AND tgname='representation_identity_insert'"
        ),
        {"table": tokens["corpus"] + ".representation_revision"},
    ).one_or_none()
    if downgrade:
        guard = conn.scalar(text("SELECT oid FROM pg_roles WHERE rolname=:role"), {"role": ns.role("guard")})
        if (
            fn is None
            or trigger is None
            or tuple(trigger) != (fn.oid, 7, "O")
            or sha256(normalize_body(fn.definition, tokens).encode()).hexdigest()
            != entry["current_definition_hash"]
            or (guard is not None and fn.proowner != guard)
        ):
            raise RuntimeError("Representation insert authority requires boundary review")
    elif fn is not None or trigger is not None:
        raise RuntimeError("Unexpected representation insert authority requires boundary review")


def finalize_insert_authority(conn, ns, tokens, entry):
    definition = conn.scalar(
        text("SELECT pg_get_functiondef(to_regprocedure(:fn))"),
        {"fn": tokens["policy"] + ".representation_identity_insert()"},
    )
    if sha256(normalize_body(definition, tokens).encode()).hexdigest() != entry["current_definition_hash"]:
        raise RuntimeError("Installed representation insert authority differs from reviewed SQL")
    guard = conn.scalar(text("SELECT rolname FROM pg_roles WHERE rolname=:role"), {"role": ns.role("guard")})
    if guard:
        # PostgreSQL requires the new owner to have schema CREATE during transfer.
        # Keep that privilege inside this migration transaction only.
        conn.exec_driver_sql("GRANT CREATE ON SCHEMA " + tokens["policy"] + " TO " + tokens["role_guard"])
        conn.exec_driver_sql(
            "ALTER FUNCTION "
            + tokens["policy"]
            + ".representation_identity_insert() OWNER TO "
            + tokens["role_guard"]
        )
        conn.exec_driver_sql("REVOKE CREATE ON SCHEMA " + tokens["policy"] + " FROM " + tokens["role_guard"])


def apply(downgrade=False):
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    tokens = context(conn, ns)
    if bundle_hash("v0013") != RESOURCE_HASH:
        raise RuntimeError("Applied SQL revision resources changed; use a new revision")
    root = files("rights").joinpath("sql/v0013")
    entry = json.loads(root.joinpath("manifest.json").read_text())["new_functions"][0]
    assert_constraint(conn, tokens, downgrade)
    assert_insert_authority(conn, ns, tokens, entry, downgrade)
    install(conn, ns, "v0013", RESOURCE_HASH, downgrade=downgrade)
    source = root.joinpath("downgrade.sql" if downgrade else "identity.sql").read_text()
    conn.exec_driver_sql(render(source, tokens), execution_options={"no_parameters": True})
    if not downgrade:
        finalize_insert_authority(conn, ns, tokens, entry)


def upgrade():
    apply()


def downgrade():
    apply(downgrade=True)
