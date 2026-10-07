GRANT INSERT ON {{policy}}.controller_record,{{policy}}.privacy_review,{{policy}}.acquisition_assessment,{{staging}}.staged_artifact TO {{role_guard}};
GRANT UPDATE(maximum_bytes) ON {{policy}}.synthetic_staging_limit TO {{role_guard}};
GRANT EXECUTE ON FUNCTION {{policy}}.apply_acquisition_command(jsonb) TO {{role_review}};
GRANT EXECUTE ON FUNCTION {{policy}}.private_operation_allowed(uuid,bigint,text,text,text,text) TO {{role_acquisition}};
GRANT EXECUTE ON FUNCTION {{policy}}.is_native_operator() TO {{role_acquisition}};
GRANT EXECUTE ON FUNCTION {{policy}}.stage_synthetic_artifact(uuid,text,uuid,bigint,text,bytea,timestamptz) TO {{role_acquisition}};
GRANT EXECUTE ON FUNCTION {{policy}}.read_synthetic_artifact(uuid,text,text) TO {{role_acquisition}};
