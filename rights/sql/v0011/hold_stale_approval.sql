CREATE OR REPLACE FUNCTION {{policy}}.hold_stale_approval(artifact uuid)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE changed integer;next_rev bigint;audit uuid;meta record;targets jsonb;
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision,purpose INTO meta FROM {{staging}}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT jsonb_build_object('binding',coalesce((SELECT jsonb_agg(id) FROM {{staging}}.synthetic_expression_binding b WHERE artifact_id=artifact AND state='active' AND NOT {{policy}}.binding_current(b.id)),'[]'),
                'review',coalesce((SELECT jsonb_agg(id) FROM {{staging}}.snapshot_review r WHERE artifact_id=artifact AND state='active' AND
                    (NOT {{policy}}.binding_current(r.binding_id) OR NOT EXISTS(SELECT 1 FROM {{policy}}.revision_head WHERE kind='verification' AND scope_id=r.collection_id AND revision=r.verification_revision)
                    OR (NOT EXISTS(SELECT 1 FROM {{policy}}.approval_commit WHERE review_id=r.id) AND NOT {{policy}}.canonical_current(r.proposal_id,meta.purpose)))),'[]'),
                'version',coalesce((SELECT jsonb_agg(id) FROM {{corpus}}.approved_version v WHERE artifact_id=artifact AND state='active' AND NOT {{policy}}.approved_version_current_m33(v.id,meta.purpose)),'[]'),
                'representation',coalesce((SELECT jsonb_agg(id) FROM {{corpus}}.representation_revision r WHERE artifact_id=artifact AND state='active' AND NOT {{policy}}.approved_version_current_m33(r.version_id,meta.purpose)),'[]')) INTO targets;
            changed:=jsonb_array_length(targets->'binding')+jsonb_array_length(targets->'review')+jsonb_array_length(targets->'version')+jsonb_array_length(targets->'representation');
            IF changed=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(revision),0)+1 INTO next_rev FROM {{policy}}.assembly_lifecycle_event WHERE artifact_id=artifact;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {{policy}}.current_actor(),meta.collection_id,evidence_id,'assembly_lifecycle',artifact::text,next_rev,'Approval authority no longer current'
                FROM {{policy}}.acquisition_assessment WHERE id=meta.assessment_id AND revision=meta.assessment_revision RETURNING id INTO audit;
            INSERT INTO {{policy}}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id)
                VALUES(artifact,next_rev,{{policy}}.current_actor(),'held','derivation_not_current',changed,audit);
            UPDATE {{staging}}.synthetic_expression_binding SET state='held' WHERE targets->'binding' ? id::text;
            UPDATE {{staging}}.snapshot_review SET state='held' WHERE targets->'review' ? id::text;
            UPDATE {{corpus}}.approved_version SET state='held' WHERE targets->'version' ? id;
            UPDATE {{corpus}}.representation_revision SET state='held' WHERE targets->'representation' ? id;
            RETURN changed;
        END $function$
