CREATE OR REPLACE FUNCTION {{policy}}.erase_staged(artifact_id uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE artifact {{staging}}.staged_artifact; evidence uuid; operator_id uuid;
        BEGIN
            operator_id:={{policy}}.current_actor();
            IF operator_id IS NULL THEN RAISE EXCEPTION 'Acquisition subject required' USING ERRCODE='42501'; END IF;
            PERFORM 1 FROM {{policy}}.actor a WHERE a.id=operator_id FOR SHARE;
            SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                acquired_at,retention_deadline,state,purpose,acquired_by INTO artifact
                FROM {{staging}}.staged_artifact WHERE id=artifact_id FOR UPDATE;
            IF artifact.id IS NULL OR NOT {{policy}}.assigned(artifact.collection_id,{{vocabulary_acquire}}) THEN
                RAISE EXCEPTION 'Acquisition assignment required' USING ERRCODE='42501'; END IF;
            IF artifact.state='erased' THEN RETURN true; END IF;
            SELECT evidence_id INTO evidence FROM {{policy}}.acquisition_assessment
                WHERE id=artifact.assessment_id AND revision=artifact.assessment_revision;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(operator_id,artifact.collection_id,evidence,'staging_erasure',artifact_id::text,1,'Scoped staging erasure');
            UPDATE {{staging}}.staged_artifact SET state='erased',private_bytes=NULL WHERE id=artifact_id;
            RETURN true;
        END $function$
