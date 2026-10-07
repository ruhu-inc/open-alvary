-- Value-only normalization. Non-ASCII metadata is compared by the full Python profile.
CREATE FUNCTION {{base}}.legacy_metadata_normalize(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog
AS $$ SELECT btrim(regexp_replace(translate(value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'[[:space:]]+',' ','g')) $$;
CREATE INDEX ix_sources_metadata_trigram ON {{base}}.sources USING gin
({{base}}.legacy_metadata_normalize(coalesce(payload->>'title','')||' '||coalesce(payload->>'citation','')) {{trigram_schema}}.gin_trgm_ops);
CREATE TABLE {{base}}.legacy_search_migration_state (
    singleton integer PRIMARY KEY CHECK(singleton=1), owns_trigram boolean NOT NULL
);
INSERT INTO {{base}}.legacy_search_migration_state VALUES(1,{{owns_trigram}});
