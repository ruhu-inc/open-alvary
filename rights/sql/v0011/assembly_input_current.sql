CREATE OR REPLACE FUNCTION {{policy}}.assembly_input_current(artifact uuid, requested_purpose text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{staging}}.staged_artifact a WHERE a.id=artifact AND a.purpose=requested_purpose
                AND a.acquired_by IS NOT NULL AND a.state IN ('staged','processing','validated') AND a.retention_deadline>statement_timestamp()
                AND {{policy}}.private_operation_allowed(a.assessment_id,a.assessment_revision,a.collection_id,a.purpose,a.artifact_hash,{{vocabulary_derive}})
                AND {{policy}}.private_operation_allowed(a.assessment_id,a.assessment_revision,a.collection_id,a.purpose,a.artifact_hash,{{vocabulary_retain}}))
        $function$
