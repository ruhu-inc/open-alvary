GRANT SELECT,INSERT,UPDATE,DELETE ON {{corpus}}.membership, {{changes}}.membership_state TO {{role_release}};
GRANT SELECT ON {{corpus}}.membership, {{changes}}.membership_state TO {{role_serving}};
GRANT SELECT ON {{corpus}}.membership TO {{role_guard}};
