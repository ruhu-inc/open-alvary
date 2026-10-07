CREATE OR REPLACE FUNCTION {{policy}}.hold_stale_canonical(artifact uuid)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE count_value integer; next_rev bigint; audit uuid; item record;
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision,purpose INTO item FROM {{staging}}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT count(*) INTO count_value FROM {{staging}}.canonical_proposal c WHERE c.artifact_id=artifact AND c.state='active' AND NOT {{policy}}.canonical_current(c.id,item.purpose);
            IF count_value=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(e.revision),0)+1 INTO next_rev FROM {{policy}}.assembly_lifecycle_event e WHERE e.artifact_id=artifact;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {{policy}}.current_actor(),item.collection_id,a.evidence_id,'assembly_lifecycle',artifact::text,next_rev,'Snapshot authority no longer current'
                FROM {{policy}}.acquisition_assessment a WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision RETURNING id INTO audit;
            INSERT INTO {{policy}}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id)
                VALUES(artifact,next_rev,{{policy}}.current_actor(),'held','derivation_not_current',count_value,audit);
            UPDATE {{staging}}.canonical_proposal c SET state='held' WHERE c.artifact_id=artifact AND c.state='active' AND NOT {{policy}}.canonical_current(c.id,item.purpose);
            RETURN count_value;
        END $function$
