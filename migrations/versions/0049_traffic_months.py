"""Website traffic's months made and dropped by a function, so that the daily job needs no ownership.

accounts.traffic_record_day is split by month (migration 0044). Adding a month or dropping one takes the
table's owner, and the daily job signs in with a restricted login (rights/logins.py).
accounts.traffic_months does exactly that and nothing else, as the tables' owner, as traffic_salt and
record_traffic write their counts. It lives beside the tables and not in the policy schema, whose functions
all go to the guard role when capability roles are made (rights.capabilities): that role owns no table, and
a partition can only be added by the owner of its parent.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE FUNCTION {a}.traffic_months(today date, months_ahead integer, days_kept integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    opening date;
    month_table text;
    dropped integer := 0;
BEGIN
    -- Months ahead, so a job failing for weeks loses nothing: counts need their month to exist.
    FOR ahead IN 0..months_ahead LOOP
        opening := (date_trunc('month', today::timestamp) + make_interval(months => ahead))::date;
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS {a}.%I PARTITION OF {a}.traffic_record_day'
            ' FOR VALUES FROM (%L) TO (%L) WITH (fillfactor = 80)',
            'traffic_record_day_' || to_char(opening, 'YYYY_MM'), opening, (opening + interval '1 month')::date
        );
    END LOOP;
    -- A month goes whole when its last day is past what is kept; monthly totals stay.
    FOR month_table IN
        SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = {parent}::regclass
    LOOP
        IF (to_date(right(month_table, 7), 'YYYY_MM') + interval '1 month')::date <= today - days_kept THEN
            EXECUTE format('DROP TABLE {a}.%I', month_table);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END
$function$;
REVOKE ALL ON FUNCTION {a}.traffic_months(date, integer, integer) FROM PUBLIC;
"""


def accounts_schema():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("accounts"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    accounts = accounts_schema()
    parent = op.get_bind().scalar(text("SELECT quote_literal(:t)"), {"t": f"{accounts}.traffic_record_day"})
    op.execute(FUNCTION.format(a=accounts, parent=parent))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP FUNCTION {accounts_schema()}.traffic_months(date, integer, integer)")
