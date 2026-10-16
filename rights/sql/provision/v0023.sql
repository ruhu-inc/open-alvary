GRANT USAGE ON SCHEMA {{changes}} TO {{role_release}}, {{role_serving}};
GRANT SELECT,INSERT ON {{changes}}.change_event TO {{role_release}};
GRANT SELECT,INSERT,UPDATE ON {{changes}}.published_state, {{changes}}.coverage_state, {{changes}}.feed_state TO {{role_release}};
GRANT SELECT ON {{changes}}.change_event, {{changes}}.feed_state, {{changes}}.coverage_state TO {{role_serving}};
GRANT EXECUTE ON FUNCTION {{policy}}.allocate_publication_revision() TO {{role_release}};
