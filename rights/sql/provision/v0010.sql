GRANT UPDATE(state,payload) ON {{staging}}.gazette_item_binding TO {{role_guard}};
GRANT INSERT,UPDATE(state,payload) ON {{staging}}.gazette_item_review,{{corpus}}.gazette_correspondence TO {{role_guard}};
GRANT INSERT,UPDATE ON {{corpus}}.gazette_correspondence_head TO {{role_guard}};
GRANT INSERT ON {{policy}}.gazette_receipt TO {{role_guard}};
REVOKE EXECUTE ON FUNCTION {{policy}}.read_approved_synthetic_version_m33(text,text) FROM {{role_acquisition}};
GRANT EXECUTE ON FUNCTION {{policy}}.apply_gazette_command(jsonb) TO {{role_review}},{{role_release}};
GRANT EXECUTE ON FUNCTION {{policy}}.read_gazette_projection(uuid,text) TO {{role_acquisition}};
