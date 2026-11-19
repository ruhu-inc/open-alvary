"""The change log's purge refuses a missing number of days (findings, 2026-11-19).

changes.purge_change_log (migration 0055) keeps 400 days at least: `IF kept_days < 400` refuses fewer.
Given NULL that test is NULL, not true, so nothing was refused; the cut-off then matched no event, every
event read as past retention, and the whole log was deleted. The daily job always passes 400, but the
floor is there to bind the restricted login that may call the function, so it now refuses NULL as it
refuses 30. The function keeps its owner, grants, SECURITY DEFINER and fixed search_path (CREATE OR
REPLACE keeps the owner and grants; the rest is stated again here).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE OR REPLACE FUNCTION {c}.purge_change_log(kept_days integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    through bigint;
    removed bigint := 0;
BEGIN
    IF {guard} THEN
        RAISE EXCEPTION 'the change log keeps 400 days at least, not %', coalesce(kept_days::text, 'none');
    END IF;
    PERFORM set_config('open_alvary.retention_purge', 'on', true);
    SELECT max(publication_revision) INTO through FROM {c}.change_event
    WHERE publication_revision < ALL (
        SELECT publication_revision FROM {c}.change_event
        WHERE committed_at >= statement_timestamp() - make_interval(days => kept_days)
    );
    IF through IS NOT NULL THEN
        DELETE FROM {c}.change_event WHERE publication_revision <= through;
        GET DIAGNOSTICS removed = ROW_COUNT;
        UPDATE {c}.feed_state SET purged_through_revision = greatest(purged_through_revision, through);
    END IF;
    PERFORM set_config('open_alvary.retention_purge', 'off', true);
    RETURN removed;
END
$function$;
REVOKE ALL ON FUNCTION {c}.purge_change_log(integer) FROM PUBLIC;
"""


def changes_schema():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("changes"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(FUNCTION.format(c=changes_schema(), guard="kept_days IS NULL OR kept_days < 400"))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(FUNCTION.format(c=changes_schema(), guard="kept_days < 400"))
