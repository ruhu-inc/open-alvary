GRANT SELECT,INSERT,UPDATE ON {{changes}}.engine_state TO {{role_release}};
GRANT SELECT ON {{changes}}.engine_state TO {{role_serving}};
