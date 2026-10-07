CREATE OR REPLACE FUNCTION {{policy}}.lock_collection(cid text, expected bigint)
 RETURNS bigint
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE actual bigint;
        BEGIN
            IF NOT ({{policy}}.assigned(cid,'rights') OR {{policy}}.assigned(cid,'release')) THEN
                RAISE EXCEPTION 'Collection authority required' USING ERRCODE='42501';
            END IF;
            SELECT revision INTO actual FROM {{policy}}.revision_head
                WHERE kind='collection' AND scope_id=cid FOR UPDATE;
            IF actual IS DISTINCT FROM expected THEN
                RAISE EXCEPTION 'Stale policy revision' USING ERRCODE='40001';
            END IF;
            RETURN actual;
        END $function$
