DROP FUNCTION {{policy}}.record_usage(jsonb);
DROP FUNCTION {{policy}}.check_credential(jsonb);
DROP FUNCTION {{policy}}.accounts_access(text);
DROP SCHEMA {{accounts}} CASCADE;
DROP FUNCTION {{policy}}.accounts_append_only();
DROP FUNCTION {{policy}}.accounts_entitlement_guard();
DROP FUNCTION {{policy}}.accounts_membership_guard();
