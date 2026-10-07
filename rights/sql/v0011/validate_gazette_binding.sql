CREATE OR REPLACE FUNCTION {{policy}}.validate_gazette_binding()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE v {{corpus}}.approved_version;source_binding {{staging}}.synthetic_expression_binding;n {{corpus}}.version_node;x record;
        BEGIN
            PERFORM {{base}}.identity_check_legacy_authority(true);
            SELECT * INTO v FROM {{corpus}}.approved_version WHERE id=NEW.issue_version_id;
            PERFORM 1 FROM {{staging}}.staged_artifact WHERE id=v.artifact_id FOR UPDATE;
            SELECT * INTO source_binding FROM {{staging}}.synthetic_expression_binding WHERE id=v.binding_id FOR SHARE;
            IF v.id IS NULL OR v.state<>'active' OR NOT {{policy}}.binding_current(source_binding.id)
                OR NOT {{policy}}.approval_policy_current(v.collection_id,v.rights_revision,v.verification_revision)
                OR NOT EXISTS(SELECT 1 FROM {{corpus}}.representation_head h JOIN {{corpus}}.representation_revision r ON r.id=h.rep_id WHERE h.version_id=v.id AND r.state='active')
                OR NOT EXISTS(SELECT 1 FROM {{staging}}.staged_artifact a JOIN {{policy}}.acquisition_assessment d ON d.id=a.assessment_id AND d.revision=a.assessment_revision
                    WHERE a.id=v.artifact_id AND a.state IN ('staged','processing','validated') AND a.retention_deadline>statement_timestamp() AND d.derive='allow' AND d.retain='allow'
                    AND {{policy}}.acquisition_current(a.assessment_id,a.assessment_revision,a.collection_id,a.purpose,a.artifact_hash))
                OR NOT EXISTS(SELECT 1 FROM {{base}}.identity_works WHERE id=source_binding.work_id AND document_class='gazette')
                THEN RAISE EXCEPTION 'Current identified synthetic gazette issue required' USING ERRCODE='23514'; END IF;
            SELECT d.* INTO n FROM {{corpus}}.version_node d WHERE d.id=NEW.notice_node_id AND d.version_id=v.id;
            IF n.id IS NULL OR n.node_kind<>'notice' OR n.parent_id IS NOT NULL OR n.start_byte IS NULL THEN RAISE EXCEPTION 'Complete issue-owned root notice required' USING ERRCODE='23514'; END IF;
            PERFORM 1 FROM {{base}}.identity_expressions e JOIN {{base}}.identity_works w ON w.id=e.work_id WHERE e.id=NEW.item_expression_id FOR SHARE OF e,w;
            SELECT e.work_id,w.collection_id,e.language_id,e.edition_kind,w.document_class,w.identity_status,w.jurisdiction_id,
                encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(e))::text,'UTF8')),'hex') AS digest INTO x
                FROM {{base}}.identity_expressions e JOIN {{base}}.identity_works w ON w.id=e.work_id WHERE e.id=NEW.item_expression_id;
            IF x.work_id IS NULL OR x.identity_status<>'identified' OR x.document_class IN ('gazette','judgment','other') OR x.edition_kind<>'original'
                OR x.language_id IS DISTINCT FROM (SELECT language_id FROM {{base}}.identity_expressions WHERE id=v.expression_id)
                OR x.jurisdiction_id IS DISTINCT FROM (SELECT jurisdiction_id FROM {{base}}.identity_works WHERE id=source_binding.work_id)
                OR x.collection_id=v.collection_id OR NOT EXISTS(SELECT 1 FROM {{policy}}.evidence WHERE id=NEW.evidence_id AND collection_id=x.collection_id)
                OR NOT EXISTS(SELECT 1 FROM {{base}}.identity_work_manifestations WHERE work_id=x.work_id AND manifestation_id=source_binding.manifestation_id)
                OR (SELECT count(*) FROM {{staging}}.gazette_item_binding WHERE issue_version_id=v.id)>=50 OR NEW.state<>'active'
                THEN RAISE EXCEPTION 'Explicit independently scoped item identity required' USING ERRCODE='23514'; END IF;
            NEW.item_work_id:=x.work_id;NEW.item_collection_id:=x.collection_id;NEW.identity_hash:=x.digest;
            NEW.issue_expression_id:=v.expression_id;NEW.issue_collection_id:=v.collection_id;NEW.artifact_id:=v.artifact_id;NEW.manifestation_id:=source_binding.manifestation_id;
            SELECT id INTO NEW.anchor_id FROM {{corpus}}.approved_anchor WHERE version_id=v.id AND start_byte=n.start_byte AND end_byte=n.end_byte AND profile_hash=v.profile_hash;
            IF NEW.anchor_id IS NULL THEN RAISE EXCEPTION 'Persisted issue-owned notice anchor required' USING ERRCODE='23514'; END IF;
            NEW.payload:=jsonb_build_object('item_expression_id',NEW.item_expression_id,'item_work_id',NEW.item_work_id,'identity_hash',NEW.identity_hash,
                'notice_node_id',NEW.notice_node_id,'issue_version_id',v.id,'anchor_id',NEW.anchor_id);
            PERFORM {{policy}}.reserve_assembly_capacity(v.artifact_id,v.collection_id,octet_length(NEW.payload::text),1);
            RETURN NEW;
        END $function$
