GRANT SELECT,INSERT,UPDATE,DELETE ON {{changes}}.link_state TO {{role_release}};
GRANT SELECT ON {{changes}}.link_state TO {{role_serving}};
