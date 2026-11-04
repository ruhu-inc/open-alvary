"""Every function a restricted login can reach while running as its owner looks only in pg_catalog, then pg_temp.

These are the functions a login of rights/logins.py may run, and the fence that fires when one writes the catalogue.
Each names its own tables in full, so the search path decides only the built-in names: this makes the order explicit.
The bundle changes that one line in each and no body (tests/test_logins_plan.py), and the signatures, owners and grants
stay as they are, so what is running keeps working during and after, on any login. Replacing a function or setting its
path touches its catalog row alone: a request running one finishes on the old definition and the next uses the new.

Any other SECURITY DEFINER function is reached only through the capability roles; a new one must do the same
(tests/test_postgres_logins.py).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None
RESOURCE_HASH = "e77d3c18dbda545404617ac4e4368ef6d60a87fe20f78e8b13b3dacb060ceaac"
# In the catalogue's schema, outside the policy schema's pinned bundles.
BASE_FUNCTIONS = ("identity_check_legacy_authority(boolean)", "identity_legacy_write_fence()")


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def set_paths(conn, ns, path):
    base = conn.dialect.identifier_preparer.quote_identifier(ns.base)
    for signature in BASE_FUNCTIONS:
        conn.exec_driver_sql(f"ALTER FUNCTION {base}.{signature} SET search_path TO {path}")


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    install(conn, ns, "v0050", RESOURCE_HASH)
    set_paths(conn, ns, "pg_catalog, pg_temp")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    set_paths(conn, ns, "pg_catalog")
    install(conn, ns, "v0050", RESOURCE_HASH, downgrade=True)
