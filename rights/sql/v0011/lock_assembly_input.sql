CREATE OR REPLACE FUNCTION {{policy}}.lock_assembly_input(artifact uuid, cid text, requested_purpose text, write boolean)
 RETURNS {{staging}}.staged_artifact
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE item {{staging}}.staged_artifact; operator_id uuid;
        BEGIN
            operator_id:={{policy}}.current_actor();
            PERFORM 1 FROM {{policy}}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {{policy}}.is_native_operator() THEN RAISE EXCEPTION 'Native session required' USING ERRCODE='42501'; END IF;
            PERFORM {{base}}.identity_check_legacy_authority(true);
            IF write THEN
                SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                    acquired_at,retention_deadline,state,purpose,acquired_by INTO item FROM {{staging}}.staged_artifact WHERE id=artifact FOR UPDATE;
            ELSE
                SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                    acquired_at,retention_deadline,state,purpose,acquired_by INTO item FROM {{staging}}.staged_artifact WHERE id=artifact FOR SHARE;
            END IF;
            IF item.id IS NULL OR item.collection_id IS DISTINCT FROM cid OR NOT {{policy}}.assigned(cid,{{vocabulary_acquire}}) THEN
                RAISE EXCEPTION 'Scoped artifact required' USING ERRCODE='42501'; END IF;
            PERFORM {{policy}}.lock_acquisition_authority(item.assessment_id,item.assessment_revision,cid);
            IF NOT {{policy}}.assembly_input_current(artifact,requested_purpose) THEN
                RAISE EXCEPTION 'Current exact derive and retain authority required' USING ERRCODE='42501'; END IF;
            RETURN item;
        END $function$
