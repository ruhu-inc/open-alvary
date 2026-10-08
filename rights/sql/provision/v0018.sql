GRANT INSERT,UPDATE ON {{corpus}}.metadata_projection TO {{role_guard}};
GRANT SELECT,INSERT ON {{policy}}.release_receipt TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.apply_release_command(jsonb) TO {{role_release}};
