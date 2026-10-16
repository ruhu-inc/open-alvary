"""SUB C3 (changes-contracts.md): webhook endpoints, their signing keys and deliveries.

Signing secrets are stored encrypted (AES-256-GCM, ALVARY_WEBHOOK_KEYS): HMAC needs them back.
A delivery's identity is stable across automatic retries; a replay is a new delivery of the same
event. Delivery records are kept 7 days.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE {a}.webhook_endpoint (
    id text PRIMARY KEY CHECK (id ~ '^whe_[0-9A-HJKMNP-TV-Z]{{26}}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    organisation_id text NOT NULL REFERENCES {a}.organisation (id) ON DELETE CASCADE,
    created_by text REFERENCES {a}.person (id) ON DELETE SET NULL,
    url text NOT NULL CHECK (char_length(url) BETWEEN 9 AND 2000 AND url LIKE 'https://%'),
    description text CHECK (char_length(description) <= 200),
    state text NOT NULL DEFAULT 'pending_verification'
        CHECK (state IN ('pending_verification', 'active', 'paused', 'failing', 'disabled')),
    consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    first_failure_at timestamptz,
    last_error text CHECK (char_length(last_error) <= 300),
    verification_window_start timestamptz,
    verification_attempts integer NOT NULL DEFAULT 0,
    replay_window_start timestamptz,
    replays integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    verified_at timestamptz
);
CREATE INDEX webhook_endpoint_organisation ON {a}.webhook_endpoint (organisation_id);

-- The endpoint's signing secrets, encrypted; during a rotation two are current.
CREATE TABLE {a}.webhook_key (
    endpoint_id text NOT NULL REFERENCES {a}.webhook_endpoint (id) ON DELETE CASCADE,
    kid text NOT NULL CHECK (kid ~ '^[0-9]{{1,6}}$'),
    secret_sealed text NOT NULL CHECK (char_length(secret_sealed) BETWEEN 20 AND 400),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    retires_at timestamptz,
    PRIMARY KEY (endpoint_id, kid)
);

CREATE TABLE {a}.webhook_endpoint_watch (
    endpoint_id text NOT NULL REFERENCES {a}.webhook_endpoint (id) ON DELETE CASCADE,
    watch_id text NOT NULL REFERENCES {a}.watch (id) ON DELETE CASCADE,
    PRIMARY KEY (endpoint_id, watch_id)
);

CREATE TABLE {a}.webhook_delivery (
    id text PRIMARY KEY CHECK (id ~ '^whd_[0-9A-HJKMNP-TV-Z]{{26}}$'),
    endpoint_id text NOT NULL REFERENCES {a}.webhook_endpoint (id) ON DELETE CASCADE,
    event_id uuid NOT NULL,
    event_revision integer NOT NULL CHECK (event_revision >= 1),
    watch_ids text[] NOT NULL DEFAULT '{{}}',
    replay_of text REFERENCES {a}.webhook_delivery (id) ON DELETE SET NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'rejected', 'abandoned', 'withheld')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    last_status integer,
    last_error text CHECK (char_length(last_error) <= 300),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    finished_at timestamptz
);
CREATE UNIQUE INDEX webhook_delivery_once ON {a}.webhook_delivery (endpoint_id, event_id, event_revision)
    WHERE replay_of IS NULL;
CREATE INDEX webhook_delivery_due ON {a}.webhook_delivery (next_attempt_at) WHERE state = 'pending';
CREATE INDEX webhook_delivery_endpoint ON {a}.webhook_delivery (endpoint_id, created_at DESC);
"""

DROP = ("webhook_delivery", "webhook_endpoint_watch", "webhook_key", "webhook_endpoint")


def schema():
    conn = op.get_bind()
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    return conn.dialect.identifier_preparer.quote_identifier(ns.schema("accounts"))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(TABLES.format(a=schema()))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    accounts = schema()
    if op.get_bind().scalar(text(f"SELECT EXISTS(SELECT 1 FROM {accounts}.webhook_endpoint)")):
        raise RuntimeError("Webhook endpoints exist; a downgrade would delete them and their secrets")
    for table in DROP:
        op.execute(f"DROP TABLE {accounts}.{table}")
