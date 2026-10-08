GRANT SELECT,INSERT,UPDATE ON {{policy}}.byte_bucket,{{policy}}.byte_reservation TO {{role_guard}};
GRANT SELECT,INSERT ON {{policy}}.byte_reservation_scope TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.reserve_bytes(jsonb),{{policy}}.begin_emission(uuid),{{policy}}.release_bytes(uuid),{{policy}}.settle_bytes(uuid, bigint, text) TO {{role_serving}};
