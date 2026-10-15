GRANT USAGE ON SCHEMA {{accounts}} TO {{role_guard}};
GRANT SELECT ON {{accounts}}.api_key, {{accounts}}.client_grant, {{accounts}}.membership, {{accounts}}.person, {{accounts}}.identity, {{accounts}}.organisation, {{accounts}}.entitlement, {{accounts}}.tier TO {{role_guard}};
GRANT UPDATE (last_used_at) ON {{accounts}}.api_key, {{accounts}}.client_grant TO {{role_guard}};
GRANT SELECT,INSERT,UPDATE ON {{accounts}}.usage_day TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.check_credential(jsonb) TO {{role_serving}};
GRANT EXECUTE ON FUNCTION {{policy}}.record_usage(jsonb) TO {{role_serving}};
