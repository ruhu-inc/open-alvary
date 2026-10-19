GRANT SELECT,INSERT,UPDATE ON {{corpus}}.legal_event, {{corpus}}.currentness_profile TO {{role_release}};
GRANT SELECT,INSERT,UPDATE,DELETE ON {{changes}}.event_state TO {{role_release}};
GRANT SELECT ON {{corpus}}.legal_event, {{corpus}}.currentness_profile, {{changes}}.event_state TO {{role_serving}};
GRANT SELECT ON {{corpus}}.legal_event, {{corpus}}.currentness_profile TO {{role_guard}};
