CREATE OR REPLACE FUNCTION {{policy}}.hold_stale_gazette(artifact uuid)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE meta record;targets jsonb;changed integer;next_rev bigint;audit uuid;
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision,purpose INTO meta FROM {{staging}}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT jsonb_build_object('review',coalesce((SELECT jsonb_agg(r.id) FROM {{staging}}.gazette_item_review r JOIN {{staging}}.gazette_item_binding b ON b.id=r.binding_id WHERE r.artifact_id=artifact AND r.state='active' AND
                (NOT {{policy}}.approved_version_current_m33(b.issue_version_id,meta.purpose) OR NOT {{policy}}.gazette_identity_current(b.id) OR NOT {{policy}}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision)
                OR NOT EXISTS(SELECT 1 FROM {{corpus}}.representation_head WHERE version_id=b.issue_version_id AND rep_id=r.representation_id))),'[]'),
                'correspondence',coalesce((SELECT jsonb_agg(g.id) FROM {{corpus}}.gazette_correspondence g JOIN {{staging}}.gazette_item_binding b ON b.id=g.binding_id JOIN {{staging}}.gazette_item_review r ON r.id=g.review_id WHERE g.artifact_id=artifact AND g.state='active' AND
                    (NOT {{policy}}.approved_version_current_m33(b.issue_version_id,meta.purpose) OR NOT {{policy}}.gazette_identity_current(b.id) OR NOT {{policy}}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision)
                    OR NOT EXISTS(SELECT 1 FROM {{corpus}}.representation_head WHERE version_id=b.issue_version_id AND rep_id=g.representation_id))),'[]')) INTO targets;
            changed:=jsonb_array_length(targets->'review')+jsonb_array_length(targets->'correspondence');
            IF changed=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(revision),0)+1 INTO next_rev FROM {{policy}}.assembly_lifecycle_event WHERE artifact_id=artifact;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {{policy}}.current_actor(),meta.collection_id,evidence_id,'assembly_lifecycle',artifact::text,next_rev,'Gazette item authority no longer current' FROM {{policy}}.acquisition_assessment WHERE id=meta.assessment_id AND revision=meta.assessment_revision RETURNING id INTO audit;
            INSERT INTO {{policy}}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id) VALUES(artifact,next_rev,{{policy}}.current_actor(),'held','derivation_not_current',changed,audit);
            UPDATE {{staging}}.gazette_item_review SET state='held' WHERE targets->'review' ? id::text;
            UPDATE {{corpus}}.gazette_correspondence SET state='held' WHERE targets->'correspondence' ? id::text;
            RETURN changed;
        END $function$
