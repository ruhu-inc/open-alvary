DROP FUNCTION {{policy}}.release_admission(uuid);
DROP FUNCTION {{policy}}.admit(jsonb);
DROP TABLE {{policy}}.admission_lease;
DROP TABLE {{policy}}.admission_bucket;
