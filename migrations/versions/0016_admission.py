"""ACS-05 admission store: token buckets and concurrency leases in one short transaction."""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install_new, remove_new

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None
RESOURCE_HASH = "3124087da63d9b50590a593d3c4165546a765b6ea73888c0a28100638e782ad6"
GUARD_GRANTS = (
    "GRANT SELECT,INSERT,UPDATE ON {{policy}}.admission_bucket TO {{role_guard}}",
    "GRANT SELECT,INSERT,DELETE ON {{policy}}.admission_lease TO {{role_guard}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.admit(jsonb) TO {{role_serving}}",
    "GRANT EXECUTE ON FUNCTION {{policy}}.release_admission(uuid) TO {{role_serving}}",
)


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    install_new(
        op.get_bind(),
        namespace(),
        "v0016",
        RESOURCE_HASH,
        ("admission_bucket", "admission_lease"),
        GUARD_GRANTS,
    )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # Buckets and leases are short-lived enforcement state, not history; dropping them only
    # resets allowances, which a downgrade to the pre-admission schema implies anyway.
    remove_new(op.get_bind(), namespace(), "v0016", RESOURCE_HASH)
