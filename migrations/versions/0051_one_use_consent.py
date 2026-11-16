"""An app-approval hand-off is used once, and a person has one active grant per app (findings item 7).

The sign-in provider's hand-off to the console's approval page could be replayed for its ten minutes,
and the console's own signed answer passed as a new hand-off, so one approval could insert grants and
audit rows without limit. Each hand-off now carries an ID the console records when it decides
(consent_handoff, kept a day: far past the hand-off's ten minutes); and a person approving the same app
again for the same organisation and resource gets the grant they already hold.

Grants that are already duplicates are kept but revoked, the newest of each set staying: the provider
holds only the newest grant for an app (deployment/auth0/post_login.js), so the others carry no token.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE {a}.consent_handoff (
    id text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9_-]{{16,64}}$'),
    used_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX consent_handoff_used_at ON {a}.consent_handoff (used_at);

ALTER TABLE {a}.client_grant DROP CONSTRAINT client_grant_revoked_reason_check;
ALTER TABLE {a}.client_grant ADD CONSTRAINT client_grant_revoked_reason_check CHECK (revoked_reason IN
    ('person','administrator','member_left','organisation_deleted','account_closed','operator','replaced'));

UPDATE {a}.client_grant SET revoked_at = statement_timestamp(), revoked_reason = 'replaced'
WHERE revoked_at IS NULL AND id NOT IN (
    SELECT DISTINCT ON (person_id, organisation_id, client_id, resource) id FROM {a}.client_grant
    WHERE revoked_at IS NULL ORDER BY person_id, organisation_id, client_id, resource, created_at DESC, id DESC
);
CREATE UNIQUE INDEX client_grant_one_active ON {a}.client_grant (person_id, organisation_id, client_id, resource)
    WHERE revoked_at IS NULL;
"""

DOWNGRADE = """
DROP INDEX {a}.client_grant_one_active;
UPDATE {a}.client_grant SET revoked_reason = 'operator' WHERE revoked_reason = 'replaced';
ALTER TABLE {a}.client_grant DROP CONSTRAINT client_grant_revoked_reason_check;
ALTER TABLE {a}.client_grant ADD CONSTRAINT client_grant_revoked_reason_check CHECK (revoked_reason IN
    ('person','administrator','member_left','organisation_deleted','account_closed','operator'));
DROP TABLE {a}.consent_handoff;
"""


def schema():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("accounts"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(UPGRADE.format(a=schema()))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(DOWNGRADE.format(a=schema()))
