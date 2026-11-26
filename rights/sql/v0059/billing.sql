-- Seat limits and Stripe billing (docs: teams-and-seats §5, stripe-billing §4.1).
-- An organisation over its plan's seats gets a grace period, then its members' personal keys and connected
-- apps are refused (policy.check_credential); nobody is removed. Paid access stays in entitlement (source
-- 'payment', reference the Stripe subscription), written only from verified webhooks; these tables mirror
-- Stripe so that webhooks are idempotent and arrive in any order.
ALTER TABLE {{accounts}}.organisation ADD COLUMN seat_grace_until timestamptz;
-- Starter covers one country, which an owner chooses (accounts.store change_countries). Starter holds no
-- grant to carry a scope, so the choice lives here; policy.check_credential gives it as Starter's scope.
ALTER TABLE {{accounts}}.organisation ADD COLUMN starter_country text CHECK (starter_country ~ '^[a-z]{2}$');
-- An operator's seat count for a contract (Enterprise); never in scope, whose shape the plan rules fix.
ALTER TABLE {{accounts}}.entitlement ADD COLUMN seats integer CHECK (seats BETWEEN 1 AND 100000);

CREATE TABLE {{accounts}}.billing_customer (
    organisation_id text PRIMARY KEY REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    stripe_customer_id text NOT NULL UNIQUE CHECK (stripe_customer_id ~ '^cus_[A-Za-z0-9]{1,250}$'),
    -- Cleared when the organisation is deleted; Stripe keeps its own copy for its records.
    billing_email text CHECK (char_length(billing_email) BETWEEN 3 AND 320),
    open_checkout_session_id text CHECK (open_checkout_session_id ~ '^cs_[A-Za-z0-9_]{1,250}$'),
    -- The Idempotency-Key that session was created with: a repeat of the same request gets the same session.
    open_checkout_key text CHECK (open_checkout_key ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK ((open_checkout_session_id IS NULL) = (open_checkout_key IS NULL))
);

CREATE TABLE {{accounts}}.billing_subscription (
    stripe_subscription_id text PRIMARY KEY CHECK (stripe_subscription_id ~ '^sub_[A-Za-z0-9]{1,250}$'),
    organisation_id text NOT NULL REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    tier_id text NOT NULL REFERENCES {{accounts}}.tier (id),
    billing_interval text NOT NULL CHECK (billing_interval IN ('month','year')),
    collection_method text NOT NULL CHECK (collection_method IN ('charge_automatically','send_invoice')),
    status text NOT NULL CHECK (status IN ('incomplete','incomplete_expired','trialing','active','past_due',
        'canceled','unpaid','paused')),
    invoice_paid boolean NOT NULL DEFAULT false,
    latest_invoice_id text CHECK (latest_invoice_id ~ '^in_[A-Za-z0-9]{1,250}$'),
    latest_invoice_url text CHECK (latest_invoice_url ~ '^https://[^[:space:]]+$' AND char_length(latest_invoice_url) <= 2000),
    scope jsonb NOT NULL CHECK (scope = '{"all": true}'::jsonb OR (
        jsonb_typeof(scope->'jurisdictions') = 'array'
        AND jsonb_array_length(scope->'jurisdictions') BETWEEN 1 AND 100
        AND scope - 'jurisdictions' = '{}'::jsonb)),
    current_period_end timestamptz,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    past_due_since timestamptz,
    -- When the newest customer.subscription.* event applied was created: an older one changes nothing.
    last_event_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX billing_subscription_organisation ON {{accounts}}.billing_subscription (organisation_id);

-- Each Stripe event once: a row is written in the same transaction as what the event changed.
CREATE TABLE {{accounts}}.stripe_webhook_event (
    event_id text PRIMARY KEY CHECK (event_id ~ '^evt_[A-Za-z0-9_]{1,250}$'),
    event_type text NOT NULL CHECK (event_type ~ '^[a-z_.]{1,100}$'),
    received_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    processed_at timestamptz,
    outcome text NOT NULL CHECK (outcome ~ '^[a-z_]{1,40}$')
);
CREATE INDEX stripe_webhook_event_received ON {{accounts}}.stripe_webhook_event (received_at);

-- How many members the organisation's access allows now: the operator's seats for a contract, else the
-- plan's allowance.members, else (a plan seeded without one) the organisation's own member_limit. With no
-- access granted it is Starter's, as policy.accounts_access gives Starter. It runs as whoever calls it:
-- the service and jobs logins, and policy.check_credential as its owner.
CREATE FUNCTION {{policy}}.organisation_seat_limit(organisation_id text) RETURNS integer
LANGUAGE sql STABLE SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
    WITH current_access AS (
        SELECT e.seats, e.tier_id FROM {{accounts}}.entitlement e
        WHERE e.organisation_id = organisation_seat_limit.organisation_id
            AND e.state IN ('scheduled','active') AND e.starts_at <= statement_timestamp()
            AND (e.ends_at IS NULL OR e.ends_at > statement_timestamp())
        ORDER BY e.starts_at DESC LIMIT 1
    )
    SELECT coalesce(
        (SELECT c.seats FROM current_access c),
        (SELECT CASE WHEN jsonb_typeof(t.allowance->'members') = 'number'
                AND t.allowance->>'members' ~ '^[1-9][0-9]{0,4}$|^100000$'
            THEN (t.allowance->>'members')::integer END
            FROM {{accounts}}.tier t
            WHERE t.id = coalesce((SELECT c.tier_id FROM current_access c), 'starter')),
        (SELECT o.member_limit FROM {{accounts}}.organisation o
            WHERE o.id = organisation_seat_limit.organisation_id))
$function$;

REVOKE ALL ON FUNCTION {{policy}}.organisation_seat_limit(text) FROM PUBLIC;
