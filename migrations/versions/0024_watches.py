"""SUB C2 (changes-contracts.md): watches, their inbox and digest email.

A watch belongs to an organisation and the person who made it; deleting it deletes its filters
and matches at once. Digests are keyed by (person, cadence, window start) so a send is never
repeated. Unsubscribe tokens are stored as hashes. Nothing here is visible to API keys.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE {a}.watch (
    id text PRIMARY KEY CHECK (id ~ '^wat_[0-9A-HJKMNP-TV-Z]{{26}}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    organisation_id text NOT NULL REFERENCES {a}.organisation (id) ON DELETE CASCADE,
    owner_person_id text NOT NULL REFERENCES {a}.person (id) ON DELETE CASCADE,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    filters jsonb NOT NULL CHECK (jsonb_typeof(filters) = 'object'),
    filter_hash text NOT NULL CHECK (char_length(filter_hash) = 32),
    matcher_revision text NOT NULL CHECK (char_length(matcher_revision) BETWEEN 1 AND 40),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'paused')),
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'organisation')),
    start_epoch uuid NOT NULL,
    start_revision bigint NOT NULL CHECK (start_revision >= 0),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    activated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    paused_at timestamptz
);
CREATE INDEX watch_organisation ON {a}.watch (organisation_id, created_at);

-- Who receives a watch's matches by email, and how often (owner; members of a shared watch).
CREATE TABLE {a}.watch_member (
    watch_id text NOT NULL REFERENCES {a}.watch (id) ON DELETE CASCADE,
    person_id text NOT NULL REFERENCES {a}.person (id) ON DELETE CASCADE,
    email text NOT NULL DEFAULT 'off' CHECK (email IN ('off', 'daily', 'weekly')),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (watch_id, person_id)
);
CREATE INDEX watch_member_person ON {a}.watch_member (person_id) WHERE email <> 'off';

CREATE TABLE {a}.inbox_item (
    id text PRIMARY KEY CHECK (id ~ '^inb_[0-9A-HJKMNP-TV-Z]{{26}}$'),
    watch_id text NOT NULL REFERENCES {a}.watch (id) ON DELETE CASCADE,
    event_id uuid NOT NULL,
    event_revision integer NOT NULL CHECK (event_revision >= 1),
    matched_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    match_reasons jsonb NOT NULL CHECK (jsonb_typeof(match_reasons) = 'object'),
    UNIQUE (watch_id, event_id, event_revision)
);
CREATE INDEX inbox_item_watch ON {a}.inbox_item (watch_id, matched_at DESC);

CREATE TABLE {a}.inbox_read (
    inbox_item_id text NOT NULL REFERENCES {a}.inbox_item (id) ON DELETE CASCADE,
    person_id text NOT NULL REFERENCES {a}.person (id) ON DELETE CASCADE,
    read_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (inbox_item_id, person_id)
);

CREATE TABLE {a}.notification_pref (
    person_id text PRIMARY KEY REFERENCES {a}.person (id) ON DELETE CASCADE,
    timezone text NOT NULL DEFAULT 'UTC' CHECK (char_length(timezone) BETWEEN 1 AND 64),
    daily_minute smallint NOT NULL DEFAULT 420 CHECK (daily_minute BETWEEN 0 AND 1439),
    weekly_day smallint NOT NULL DEFAULT 1 CHECK (weekly_day BETWEEN 1 AND 7),
    email_on boolean NOT NULL DEFAULT true,
    bounced_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

-- One row per sent (or empty) digest window; receipts expire after 30 days.
CREATE TABLE {a}.digest (
    id text PRIMARY KEY CHECK (id ~ '^dig_[0-9A-HJKMNP-TV-Z]{{26}}$'),
    person_id text NOT NULL REFERENCES {a}.person (id) ON DELETE CASCADE,
    cadence text NOT NULL CHECK (cadence IN ('daily', 'weekly')),
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL CHECK (window_end > window_start),
    event_count integer NOT NULL CHECK (event_count >= 0),
    outcome text NOT NULL CHECK (outcome IN ('empty', 'sent', 'failed', 'suppressed')),
    provider_message text CHECK (char_length(provider_message) <= 200),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT statement_timestamp() + interval '30 days',
    UNIQUE (person_id, cadence, window_start)
);

CREATE TABLE {a}.unsubscribe_token (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{{64}}$'),
    person_id text NOT NULL REFERENCES {a}.person (id) ON DELETE CASCADE,
    watch_id text REFERENCES {a}.watch (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT statement_timestamp() + interval '90 days'
);

-- Where matching has reached in the change log, per feed epoch.
CREATE TABLE {a}.matcher_checkpoint (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    epoch uuid NOT NULL,
    publication_revision bigint NOT NULL,
    event_id uuid NOT NULL,
    event_revision integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
"""

DROP = (
    "matcher_checkpoint",
    "unsubscribe_token",
    "digest",
    "notification_pref",
    "inbox_read",
    "inbox_item",
    "watch_member",
    "watch",
)


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
    if op.get_bind().scalar(text(f"SELECT EXISTS(SELECT 1 FROM {accounts}.watch)")):
        raise RuntimeError("Watches exist; a downgrade would delete them")
    for table in DROP:
        op.execute(f"DROP TABLE {accounts}.{table}")
