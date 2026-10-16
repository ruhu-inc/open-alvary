DROP SCHEMA {{changes}} CASCADE;
DROP FUNCTION {{policy}}.allocate_publication_revision();
DROP FUNCTION {{policy}}.changes_append_only();
