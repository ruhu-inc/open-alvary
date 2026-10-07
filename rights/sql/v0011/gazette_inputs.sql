CREATE OR REPLACE FUNCTION {{policy}}.gazette_inputs(bid uuid, issue_cid text, item_cid text, purpose_value text, write boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE binding {{staging}}.gazette_item_binding;v record;n {{corpus}}.version_node;rep record;
        BEGIN
            SELECT id,artifact_id,issue_version_id,issue_collection_id,item_collection_id INTO binding.id,binding.artifact_id,binding.issue_version_id,binding.issue_collection_id,binding.item_collection_id FROM {{staging}}.gazette_item_binding WHERE id=bid;
            IF binding.id IS NULL OR binding.issue_collection_id IS DISTINCT FROM issue_cid OR binding.item_collection_id IS DISTINCT FROM item_cid
                OR NOT {{policy}}.assigned(item_cid,{{vocabulary_acquire}}) THEN RAISE EXCEPTION 'Exact scoped item binding required' USING ERRCODE='42501'; END IF;
            PERFORM {{policy}}.lock_assembly_input(binding.artifact_id,issue_cid,purpose_value,write);
            SELECT * INTO binding FROM {{staging}}.gazette_item_binding WHERE id=bid FOR SHARE;
            PERFORM 1 FROM {{corpus}}.representation_head WHERE version_id=binding.issue_version_id FOR SHARE;
            IF NOT {{policy}}.gazette_identity_current(bid) OR NOT {{policy}}.approved_version_current_m33(binding.issue_version_id,purpose_value) THEN RAISE EXCEPTION 'Current issue and identified item required' USING ERRCODE='42501'; END IF;
            SELECT id,expression_id,artifact_id,collection_id,rights_revision,verification_revision,content_hash,profile_hash,snapshot_hash,serialization_profile_id,state INTO v FROM {{corpus}}.approved_version WHERE id=binding.issue_version_id;
            PERFORM {{policy}}.lock_synthetic_approval_policy(issue_cid,v.rights_revision,v.verification_revision);
            SELECT * INTO n FROM {{corpus}}.version_node WHERE id=binding.notice_node_id;
            SELECT r.id,r.version_id,r.revision,r.record_set_hash,r.state INTO rep FROM {{corpus}}.representation_revision r JOIN {{corpus}}.representation_head h ON h.rep_id=r.id WHERE h.version_id=v.id;
            RETURN jsonb_build_object('binding',to_jsonb(binding)-'payload','binding_hash',{{policy}}.gazette_binding_hash(bid),'version',to_jsonb(v)-ARRAY['payload','canonical_bytes'],
                'notice',to_jsonb(n),'representation',to_jsonb(rep)-'payload');
        END $function$
