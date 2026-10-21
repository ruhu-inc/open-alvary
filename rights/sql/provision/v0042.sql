GRANT SELECT,INSERT,UPDATE,DELETE ON {{corpus}}.membership_suspension, {{changes}}.suspension_state TO {{role_release}};
GRANT SELECT ON {{corpus}}.membership_suspension, {{changes}}.suspension_state TO {{role_serving}};
GRANT SELECT ON {{corpus}}.membership_suspension TO {{role_guard}};
