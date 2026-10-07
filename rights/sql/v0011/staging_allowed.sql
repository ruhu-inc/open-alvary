CREATE OR REPLACE FUNCTION {{policy}}.staging_allowed(aid uuid, rev bigint, cid text, hash text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT {{policy}}.assigned(cid,{{vocabulary_acquire}}) AND EXISTS(
                SELECT 1 FROM {{policy}}.acquisition_assessment a WHERE a.id=aid AND a.revision=rev
                    AND {{policy}}.acquisition_current(aid,rev,cid,a.purpose,hash))
        $function$
