DROP FUNCTION {{policy}}.organisation_seat_limit(text);
DROP TABLE {{accounts}}.stripe_webhook_event;
DROP TABLE {{accounts}}.billing_subscription;
DROP TABLE {{accounts}}.billing_customer;
ALTER TABLE {{accounts}}.entitlement DROP COLUMN seats;
ALTER TABLE {{accounts}}.organisation DROP COLUMN starter_country;
ALTER TABLE {{accounts}}.organisation DROP COLUMN seat_grace_until;
