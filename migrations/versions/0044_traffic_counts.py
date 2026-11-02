"""Daily counts of how the public website is used (TRF-01–05): totals, never visits.

accounts.traffic_day holds one number per day, metric and label (searches that found nothing, arrivals
from Nigeria), each metric on its own so no count narrows down to one person; it stays small and is kept.
accounts.traffic_record_day holds a record's views and text reads per day. It is the one table that grows
with the corpus and the traffic, so it is split by month and old months are dropped whole (api.traffic
maintain), which leaves no dead rows to vacuum; traffic_record_month keeps each record's monthly totals
for good and traffic_top the most read records, worked out once a day so a report never scans the rest.
accounts.traffic_visitors holds the day's estimate of different visitors while the day runs, when that is
switched on: a sketch of 4096 small numbers and the day's salt, which is cleared when the next day starts.

Instances write through policy.record_traffic, in batches, adding to what is there; they can neither read
these tables nor set a count. Nothing here is written until ALVARY_TRAFFIC_COUNTS is set.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install_new, remove_new

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

RESOURCE_HASH = "e654482df00586bad6c5f74957e860d86fc5036bb8b443f2b580e5ff4bf32d6e"
# Fresh provisioning gets the same grants from rights/sql/provision/v0044.sql.
GUARD_GRANTS = (
    "GRANT SELECT,INSERT,UPDATE ON {{accounts}}.traffic_day, {{accounts}}.traffic_record_day, "
    "{{accounts}}.traffic_record_month, {{accounts}}.traffic_visitors TO {{role_guard}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.record_traffic(jsonb) TO {{role_serving}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.traffic_salt(bytea) TO {{role_serving}}",
)


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    install_new(op.get_bind(), namespace(), "v0044", RESOURCE_HASH, (), GUARD_GRANTS)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    remove_new(op.get_bind(), namespace(), "v0044", RESOURCE_HASH)
