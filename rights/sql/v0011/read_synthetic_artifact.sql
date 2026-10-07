CREATE OR REPLACE FUNCTION {{policy}}.read_synthetic_artifact(artifact_id uuid, requested_purpose text, operation text)
 RETURNS bytea
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE artifact {{staging}}.staged_artifact; actor uuid; bytes bytea;
        BEGIN
            actor:={{policy}}.current_actor();
            IF actor IS NULL THEN RETURN NULL; END IF;
            PERFORM 1 FROM {{policy}}.actor a WHERE a.id=actor FOR SHARE;
            PERFORM {{base}}.identity_check_legacy_authority(true);
            SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                acquired_at,retention_deadline,state,purpose,acquired_by INTO artifact
                FROM {{staging}}.staged_artifact WHERE id=artifact_id FOR SHARE;
            IF artifact.id IS NULL OR artifact.retention_deadline<=statement_timestamp()
                OR artifact.state NOT IN ('staged','processing','validated') OR operation NOT IN ({{vocabulary_retain}},{{vocabulary_derive}}) THEN RETURN NULL; END IF;
            PERFORM {{policy}}.lock_acquisition_authority(artifact.assessment_id,artifact.assessment_revision,artifact.collection_id);
            IF NOT {{policy}}.private_operation_allowed(artifact.assessment_id,artifact.assessment_revision,artifact.collection_id,
                requested_purpose,artifact.artifact_hash,operation) OR artifact.purpose IS DISTINCT FROM requested_purpose THEN RETURN NULL; END IF;
            SELECT private_bytes INTO bytes FROM {{staging}}.staged_artifact WHERE id=artifact_id;
            RETURN bytes;
        END $function$
