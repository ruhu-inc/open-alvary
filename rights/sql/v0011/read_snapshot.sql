CREATE OR REPLACE FUNCTION {{policy}}.read_snapshot(snapshot_uuid uuid, rev bigint, requested_purpose text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE item record; run {{staging}}.synthetic_run; result jsonb;
        BEGIN
            IF rev IS NULL OR rev NOT BETWEEN 1 AND 32 THEN RAISE EXCEPTION 'Bounded snapshot read required' USING ERRCODE='22023'; END IF;
            SELECT id,revision,run_id,artifact_id,parent_hash,payload_hash INTO item FROM {{staging}}.staging_snapshot WHERE id=snapshot_uuid AND revision=rev AND state='active';
            IF item.id IS NULL THEN RETURN NULL; END IF;
            SELECT r.* INTO run FROM {{staging}}.synthetic_run r WHERE r.id=item.run_id;
            PERFORM {{policy}}.lock_assembly_input(item.artifact_id,run.collection_id,requested_purpose,false);
            SELECT jsonb_build_object('snapshot_id',id,'revision',revision,'run_id',run_id,'parent_hash',parent_hash,'payload_hash',payload_hash,'snapshot_hash',snapshot_hash,'payload',payload)
                INTO result FROM {{staging}}.staging_snapshot WHERE id=snapshot_uuid AND revision=rev AND state='active' FOR SHARE;
            RETURN result;
        END $function$
