GRANT SELECT,INSERT,UPDATE ON {{changes}}.index_generation TO {{role_release}};
GRANT SELECT,INSERT,UPDATE,DELETE ON {{changes}}.index_job TO {{role_release}};
GRANT SELECT ON {{changes}}.index_generation, {{changes}}.index_job TO {{role_serving}};
