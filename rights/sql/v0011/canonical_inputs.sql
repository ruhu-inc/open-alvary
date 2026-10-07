CREATE OR REPLACE FUNCTION {{policy}}.canonical_inputs(command jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE snap record; run {{staging}}.synthetic_run; source jsonb; ordered jsonb; actor uuid;
        BEGIN
            IF command IS NULL OR octet_length(command::text)>65536 OR NOT {{policy}}.operator_keys(command,
                ARRAY['command_id','collection_id','reason','action','proposal_id','snapshot_id','snapshot_revision','snapshot_hash','purpose','plan'],ARRAY['serialization_profile_id'])
                OR command->>'action' IS DISTINCT FROM 'record_canonical_proposal'
                OR coalesce(command->>'serialization_profile_id','OA-text-1')<>'OA-text-1'
                OR jsonb_typeof(command->'snapshot_revision') IS DISTINCT FROM 'number'
                OR command->>'snapshot_revision' !~ '^[0-9]{1,2}$' OR (command->>'snapshot_revision')::integer NOT BETWEEN 1 AND 32
                OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string' OR length(btrim(command->>'reason')) NOT BETWEEN 1 AND 2048
                OR command->>'proposal_id' IS NULL OR command->>'command_id' IS NULL THEN RAISE EXCEPTION 'Invalid canonical command' USING ERRCODE='22023'; END IF;
            actor:={{policy}}.current_actor(); PERFORM 1 FROM {{policy}}.actor a WHERE a.id=actor FOR SHARE;
            IF actor IS NULL OR NOT {{policy}}.is_native_operator() OR NOT {{policy}}.assigned(command->>'collection_id',{{vocabulary_acquire}})
                OR NOT {{policy}}.assigned(command->>'collection_id','content') THEN RAISE EXCEPTION 'Scoped native content/acquisition session required' USING ERRCODE='42501'; END IF;
            SELECT t.id,t.revision,t.snapshot_hash,t.run_id,t.artifact_id,t.state INTO snap FROM {{staging}}.staging_snapshot t
                WHERE t.id=(command->>'snapshot_id')::uuid AND t.revision=(command->>'snapshot_revision')::bigint;
            IF snap.id IS NULL OR snap.state<>'active' OR snap.snapshot_hash IS DISTINCT FROM command->>'snapshot_hash' THEN RAISE EXCEPTION 'Current exact snapshot required' USING ERRCODE='42501'; END IF;
            SELECT r.* INTO run FROM {{staging}}.synthetic_run r WHERE r.id=snap.run_id;
            PERFORM {{policy}}.lock_assembly_input(snap.artifact_id,command->>'collection_id',command->>'purpose',true);
            PERFORM 1 FROM {{staging}}.snapshot_head h WHERE h.id=snap.id FOR SHARE;
            IF NOT EXISTS(SELECT 1 FROM {{staging}}.snapshot_head h WHERE h.id=snap.id AND h.revision=snap.revision AND h.run_id=snap.run_id)
                OR EXISTS(SELECT 1 FROM {{staging}}.snapshot_selection sel JOIN {{staging}}.adapter_candidate c ON c.id=sel.candidate_id
                    WHERE sel.snapshot_id=snap.id AND sel.revision=snap.revision AND c.state<>'active') THEN RAISE EXCEPTION 'Active current selection required' USING ERRCODE='42501'; END IF;
            SELECT jsonb_object_agg(c.id::text,c.payload),jsonb_agg(c.payload ORDER BY sel.position) INTO source,ordered
                FROM {{staging}}.snapshot_selection sel JOIN {{staging}}.adapter_candidate c ON c.id=sel.candidate_id
                WHERE sel.snapshot_id=snap.id AND sel.revision=snap.revision;
            RETURN jsonb_build_object('source',source,'candidates',ordered,'order',(SELECT payload->'order' FROM {{staging}}.staging_snapshot WHERE id=snap.id AND revision=snap.revision));
        END $function$
