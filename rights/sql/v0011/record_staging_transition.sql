CREATE OR REPLACE FUNCTION {{policy}}.record_staging_transition(artifact uuid, following text, cause_value text, cmd uuid, ah bigint, ph bigint, ch bigint)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE item record; evidence uuid; next_revision bigint; audit uuid; operator_id uuid;
        BEGIN
            operator_id:={{policy}}.current_actor();
            SELECT id,collection_id,assessment_id,assessment_revision,state INTO item
                FROM {{staging}}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT a.evidence_id INTO evidence FROM {{policy}}.acquisition_assessment a
                WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision AND a.collection_id=item.collection_id;
            SELECT coalesce(max(e.revision),0)+1 INTO next_revision FROM {{policy}}.staging_lifecycle_event e WHERE e.artifact_id=artifact;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(operator_id,item.collection_id,evidence,'staging_lifecycle',artifact::text,next_revision,cause_value)
                RETURNING id INTO audit;
            INSERT INTO {{policy}}.staging_lifecycle_event(artifact_id,revision,command_id,actor_id,collection_id,
                assessment_id,assessment_revision,from_state,to_state,cause,acquisition_head,privacy_head,controller_head,audit_id)
                VALUES(artifact,next_revision,cmd,operator_id,item.collection_id,item.assessment_id,item.assessment_revision,
                    item.state,following,cause_value,ah,ph,ch,audit);
            IF following='erased' THEN
                PERFORM {{policy}}.erase_staged(artifact);
            ELSE
                UPDATE {{staging}}.staged_artifact SET state=following WHERE id=artifact;
            END IF;
        END $function$
