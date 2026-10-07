"""Indexed legacy metadata search; legacy publication authority stays unchanged."""

from importlib.resources import files

from alembic import op

from rights.database import PolicyNamespace
from rights.sql_resources import bundle_hash, context, render

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None
RESOURCE_HASH = "3e9f368e7a4d8468074c8fe416a6b44787ee735afb3e3360fa4be515934f11cb"


def apply(down=False):
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    if bundle_hash("v0012") != RESOURCE_HASH:
        raise RuntimeError("Applied SQL revision resources changed; use a new revision")
    ns = PolicyNamespace(conn.exec_driver_sql("SELECT current_schema()").scalar_one())
    tokens = context(conn, ns)
    owns_trigram = False
    if down:
        owns_trigram = conn.exec_driver_sql(
            "SELECT owns_trigram FROM " + tokens["base"] + ".legacy_search_migration_state"
        ).scalar_one()
        schema = conn.exec_driver_sql(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'"
        ).scalar_one()
        if owns_trigram and schema != ns.base:
            raise RuntimeError("Search extension ownership changed; requires forward repair")
    else:
        existed = conn.exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_trgm')"
        ).scalar_one()
        tokens["owns_trigram"] = "false" if existed else "true"
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA " + tokens["base"])
        schema = conn.exec_driver_sql(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'"
        ).scalar_one()
        tokens["trigram_schema"] = conn.dialect.identifier_preparer.quote_identifier(schema)
    filename = "downgrade.sql" if down else "catalogue.sql"
    source = files("rights").joinpath("sql/v0012", filename).read_text()
    conn.exec_driver_sql(render(source, tokens), execution_options={"no_parameters": True})
    if down and owns_trigram:
        conn.exec_driver_sql("DROP EXTENSION pg_trgm")


def upgrade():
    apply()


def downgrade():
    apply(True)
