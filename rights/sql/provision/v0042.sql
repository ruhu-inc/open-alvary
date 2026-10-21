GRANT SELECT,INSERT,UPDATE ON {{corpus}}.citation, {{corpus}}.judicial_treatment TO {{role_release}};
GRANT SELECT,INSERT,UPDATE,DELETE ON {{changes}}.treatment_state TO {{role_release}};
GRANT SELECT ON {{corpus}}.citation, {{corpus}}.judicial_treatment, {{changes}}.treatment_state TO {{role_serving}};
GRANT SELECT ON {{corpus}}.citation, {{corpus}}.judicial_treatment TO {{role_guard}};
