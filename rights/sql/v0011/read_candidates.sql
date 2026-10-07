CREATE OR REPLACE FUNCTION {{policy}}.read_candidates(run_uuid uuid, requested_purpose text, cursor_id uuid, batch_limit integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE run {{staging}}.synthetic_run; item record; result jsonb:='[]'; last_id uuid; count_value integer:=0;
        BEGIN
            IF batch_limit IS NULL OR batch_limit NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Bounded candidate read required' USING ERRCODE='22023'; END IF;
            SELECT r.* INTO run FROM {{staging}}.synthetic_run r WHERE r.id=run_uuid;
            IF run.id IS NULL THEN RETURN jsonb_build_object('items','[]'::jsonb,'next_after',NULL); END IF;
            PERFORM {{policy}}.lock_assembly_input(run.artifact_id,run.collection_id,requested_purpose,false);
            FOR item IN SELECT id,payload,payload_hash,text_hash FROM {{staging}}.adapter_candidate WHERE run_id=run_uuid AND state='active'
                AND (cursor_id IS NULL OR id>cursor_id) ORDER BY id LIMIT batch_limit FOR SHARE
            LOOP
                count_value:=count_value+1; last_id:=item.id;
                result:=result||jsonb_build_array(jsonb_build_object('run_id',run.id,'artifact_id',run.artifact_id,'collection_id',run.collection_id,
                    'assessment_id',run.assessment_id,'assessment_revision',run.assessment_revision,'artifact_hash',run.artifact_hash,'purpose',run.purpose,
                    'retention_deadline',run.retention_deadline,'payload_hash',item.payload_hash,'text_hash',item.text_hash,'payload',item.payload));
            END LOOP;
            RETURN jsonb_build_object('items',result,'next_after',CASE WHEN count_value=batch_limit THEN last_id ELSE NULL END);
        END $function$
