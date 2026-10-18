GRANT SELECT,INSERT,UPDATE,DELETE ON {{changes}}.text_unit TO {{role_release}};
GRANT SELECT,UPDATE ON {{changes}}.text_index_state TO {{role_release}};
GRANT SELECT ON {{changes}}.text_unit, {{changes}}.text_index_state TO {{role_serving}};
