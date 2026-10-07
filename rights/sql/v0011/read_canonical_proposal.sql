CREATE OR REPLACE FUNCTION {{policy}}.read_canonical_proposal(pid uuid, requested_purpose text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE meta record; result jsonb;
        BEGIN
            SELECT id,artifact_id,collection_id,snapshot_id INTO meta FROM {{staging}}.canonical_proposal WHERE id=pid;
            IF meta.id IS NULL THEN RETURN NULL; END IF;
            PERFORM {{policy}}.lock_assembly_input(meta.artifact_id,meta.collection_id,requested_purpose,false);
            PERFORM 1 FROM {{staging}}.snapshot_head WHERE id=meta.snapshot_id FOR SHARE;
            IF NOT {{policy}}.canonical_current(pid,requested_purpose) THEN RETURN NULL; END IF;
            SELECT jsonb_build_object('proposal_id',id,'snapshot_id',snapshot_id,'snapshot_revision',snapshot_revision,'snapshot_hash',snapshot_hash,
                'serialization_profile_id',serialization_profile_id,'profile_hash',profile_hash,'content_hash',content_hash,'canonical_text',convert_from(canonical_bytes,'UTF8'),'payload',payload)
                INTO result FROM {{staging}}.canonical_proposal WHERE id=pid AND state='active' FOR SHARE;
            RETURN result;
        END $function$
