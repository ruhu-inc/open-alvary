CREATE OR REPLACE FUNCTION {{policy}}.reserve_assembly_capacity_m32(artifact uuid, cid text, extra bigint, added_rows integer)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE capacity bigint; family_bytes bigint; family_rows bigint;
        BEGIN
            SELECT maximum_bytes INTO capacity FROM {{policy}}.synthetic_staging_limit WHERE collection_id=cid FOR UPDATE;
            SELECT coalesce(sum(bytes),0),count(*) INTO family_bytes,family_rows FROM (
                SELECT octet_length(payload::text) bytes FROM {{staging}}.adapter_candidate WHERE artifact_id=artifact
                UNION ALL SELECT octet_length(payload::text) FROM {{staging}}.staging_snapshot WHERE artifact_id=artifact
                UNION ALL SELECT octet_length(payload::text)+octet_length(canonical_bytes) FROM {{staging}}.canonical_proposal WHERE artifact_id=artifact) q;
            IF capacity IS NULL OR extra<0 OR added_rows<1 OR family_rows+added_rows>128 OR family_bytes+extra>2097152
                OR {{policy}}.staging_live_bytes(cid)+extra>capacity THEN RAISE EXCEPTION 'Synthetic assembly capacity exceeded' USING ERRCODE='54000'; END IF;
        END $function$
