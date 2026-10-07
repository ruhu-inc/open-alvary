CREATE OR REPLACE FUNCTION {{policy}}.transition_assembly_m33(artifact uuid, following text, cause_value text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE next_revision bigint; audit uuid; changed integer; item record; eligible text[];
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision INTO item FROM {{staging}}.staged_artifact WHERE id=artifact FOR UPDATE;
            eligible:=CASE following WHEN 'held' THEN ARRAY['active'] WHEN 'erasure_required' THEN ARRAY['active','held']
                WHEN 'erased' THEN ARRAY['active','held','erasure_required'] ELSE ARRAY[]::text[] END;
            SELECT count(*) INTO changed FROM (SELECT 1 FROM {{staging}}.adapter_candidate WHERE artifact_id=artifact AND state=ANY(eligible)
                UNION ALL SELECT 1 FROM {{staging}}.staging_snapshot WHERE artifact_id=artifact AND state=ANY(eligible)
                UNION ALL SELECT 1 FROM {{staging}}.canonical_proposal WHERE artifact_id=artifact AND state=ANY(eligible)
                UNION ALL SELECT 1 FROM {{staging}}.synthetic_expression_binding WHERE artifact_id=artifact AND state=ANY(eligible)
                UNION ALL SELECT 1 FROM {{staging}}.snapshot_review WHERE artifact_id=artifact AND state=ANY(eligible)
                UNION ALL SELECT 1 FROM {{corpus}}.approved_version WHERE artifact_id=artifact AND state=ANY(eligible)
                UNION ALL SELECT 1 FROM {{corpus}}.representation_revision WHERE artifact_id=artifact AND state=ANY(eligible)) q;
            IF changed=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(e.revision),0)+1 INTO next_revision FROM {{policy}}.assembly_lifecycle_event e WHERE e.artifact_id=artifact;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {{policy}}.current_actor(),item.collection_id,a.evidence_id,'assembly_lifecycle',artifact::text,next_revision,cause_value
                    FROM {{policy}}.acquisition_assessment a WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision
                RETURNING id INTO audit;
            INSERT INTO {{policy}}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id)
                VALUES(artifact,next_revision,{{policy}}.current_actor(),following,cause_value,changed,audit);
            UPDATE {{staging}}.adapter_candidate SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END
                WHERE artifact_id=artifact AND state=ANY(eligible);
            UPDATE {{staging}}.staging_snapshot SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END
                WHERE artifact_id=artifact AND state=ANY(eligible);
            UPDATE {{staging}}.canonical_proposal SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END,
                canonical_bytes=CASE WHEN following='erased' THEN NULL ELSE canonical_bytes END WHERE artifact_id=artifact AND state=ANY(eligible);
            UPDATE {{staging}}.synthetic_expression_binding SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END WHERE artifact_id=artifact AND state=ANY(eligible);
            UPDATE {{staging}}.snapshot_review SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END WHERE artifact_id=artifact AND state=ANY(eligible);
            UPDATE {{corpus}}.approved_version SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END,canonical_bytes=CASE WHEN following='erased' THEN NULL ELSE canonical_bytes END WHERE artifact_id=artifact AND state=ANY(eligible);
            UPDATE {{corpus}}.representation_revision SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END WHERE artifact_id=artifact AND state=ANY(eligible);
            RETURN changed;
        END $function$
