CREATE OR REPLACE FUNCTION {{policy}}.read_approved_synthetic_version_m33(vid text, purpose_value text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE meta record;result jsonb;
        BEGIN
            SELECT artifact_id,collection_id INTO meta FROM {{corpus}}.approved_version WHERE id=vid;
            IF meta.artifact_id IS NULL THEN RETURN NULL; END IF;
            PERFORM {{policy}}.lock_assembly_input(meta.artifact_id,meta.collection_id,purpose_value,false);
            PERFORM 1 FROM {{corpus}}.representation_head WHERE version_id=vid FOR SHARE;
            PERFORM {{policy}}.lock_synthetic_approval_policy(meta.collection_id,(SELECT rights_revision FROM {{corpus}}.approved_version WHERE id=vid),(SELECT verification_revision FROM {{corpus}}.approved_version WHERE id=vid));
            IF NOT {{policy}}.approved_version_current(vid,purpose_value) THEN RETURN NULL; END IF;
            SELECT jsonb_build_object('version_id',v.id,'expression_id',v.expression_id,'current_rep_id',r.id,'rep_revision',r.revision,
                'snapshot_hash',r.snapshot_hash,'profile_hash',r.profile_hash,'serialization_profile_id',v.serialization_profile_id,'content_hash',v.content_hash,'canonical_text',convert_from(v.canonical_bytes,'UTF8'),
                'record_set',r.payload,'publication_eligible',false) INTO result FROM {{corpus}}.approved_version v JOIN {{corpus}}.representation_head h ON h.version_id=v.id
                JOIN {{corpus}}.representation_revision r ON r.id=h.rep_id WHERE v.id=vid;
            RETURN result;
        END $function$
