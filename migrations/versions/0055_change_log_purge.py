"""The change log's retention purge, as one function the daily job's restricted login may run (findings item 40).

python -m changes.admin purge deletes change events past 400 days. The jobs login holds no DELETE on
changes.change_event (entries are only added, and a trigger refuses deletes but for the purge), so under
that login the daily job would fail at the purge once the first event was 400 days old, and skip the
steps after it. changes.purge_change_log does the purge and nothing else, as the table's owner, beside
the table (not in the policy schema, whose functions go to the guard role, which owns no table).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE FUNCTION {c}.purge_change_log(kept_days integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    through bigint;
    removed bigint := 0;
BEGIN
    IF kept_days < 400 THEN
        RAISE EXCEPTION 'the change log keeps 400 days at least, not %', kept_days;
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
    op.execute(FUNCTION.format(c=changes_schema()))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP FUNCTION {changes_schema()}.purge_change_log(integer)")
