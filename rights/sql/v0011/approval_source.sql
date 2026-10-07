CREATE OR REPLACE FUNCTION {{policy}}.approval_source(pid uuid, cid text, purpose_value text, write boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE proposal {{staging}}.canonical_proposal;inputs jsonb;compiled jsonb;raw {{staging}}.staged_artifact;
        BEGIN
            SELECT id,artifact_id,snapshot_id INTO proposal.id,proposal.artifact_id,proposal.snapshot_id FROM {{staging}}.canonical_proposal WHERE id=pid;
            IF proposal.id IS NULL THEN RAISE EXCEPTION 'Current private proposal required' USING ERRCODE='42501'; END IF;
            raw:={{policy}}.lock_assembly_input(proposal.artifact_id,cid,purpose_value,write);
            PERFORM 1 FROM {{staging}}.snapshot_head WHERE id=proposal.snapshot_id FOR SHARE;
            IF NOT {{policy}}.canonical_current(pid,purpose_value) OR NOT EXISTS(SELECT 1 FROM {{policy}}.acquisition_assessment a WHERE a.id=raw.assessment_id AND a.revision=raw.assessment_revision AND a.privacy_class='no_personal_data' AND a.privacy_review_id IS NULL) THEN
                RAISE EXCEPTION 'Current complete nonpersonal synthetic proposal required' USING ERRCODE='42501'; END IF;
            SELECT * INTO proposal FROM {{staging}}.canonical_proposal WHERE id=pid FOR SHARE;
            SELECT jsonb_build_object('source',jsonb_object_agg(d.id::text,d.payload),'order',t.payload->'order') INTO inputs
                FROM {{staging}}.staging_snapshot t JOIN {{staging}}.snapshot_selection sel ON sel.snapshot_id=t.id AND sel.revision=t.revision
                JOIN {{staging}}.adapter_candidate d ON d.id=sel.candidate_id WHERE t.id=proposal.snapshot_id AND t.revision=proposal.snapshot_revision GROUP BY t.payload;
            compiled:={{policy}}.oa_compile(inputs,proposal.payload->'plan');
            IF convert_to(compiled->>'text','UTF8') IS DISTINCT FROM proposal.canonical_bytes OR compiled->'projection' IS DISTINCT FROM proposal.payload->'projection'
                OR jsonb_array_length(compiled->'projection'->'incomplete_tables')<>0 OR octet_length(proposal.canonical_bytes)=0
                THEN RAISE EXCEPTION 'Incomplete or inconsistent proposal cannot be approved' USING ERRCODE='23514'; END IF;
            RETURN jsonb_build_object('proposal',to_jsonb(proposal),'inputs',inputs,'structure',{{policy}}.approval_structure(inputs,proposal.payload->'plan',compiled->'projection'));
        END $function$
