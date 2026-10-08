GRANT SELECT,INSERT,UPDATE ON {{policy}}.admission_bucket TO {{role_guard}};
GRANT SELECT,INSERT,DELETE ON {{policy}}.admission_lease TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.admit(jsonb) TO {{role_serving}};
GRANT EXECUTE ON FUNCTION {{policy}}.release_admission(uuid) TO {{role_serving}};
