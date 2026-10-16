"""Repair organisation.website's check (v0021): PostgreSQL caps a regular expression's repetition
count at 255, so '{1,2000}' made every website refused with an error. Same rule, length apart.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def table():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return ns.qualified(conn, "accounts", "organisation")


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"ALTER TABLE {table()} DROP CONSTRAINT organisation_website_check, ADD CONSTRAINT organisation_website_check"
        " CHECK (website ~ '^https://[^[:space:]]+$' AND char_length(website) <= 2008)"
    )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"ALTER TABLE {table()} DROP CONSTRAINT organisation_website_check, ADD CONSTRAINT organisation_website_check"
        " CHECK (website ~ '^https://[^[:space:]]{1,2000}$')"
    )
