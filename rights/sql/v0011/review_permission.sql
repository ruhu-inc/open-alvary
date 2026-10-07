CREATE OR REPLACE FUNCTION {{policy}}.review_permission(cid text, operation text, vid text DEFAULT NULL::text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT ({{policy}}.assigned(cid,'rights') OR {{policy}}.assigned(cid,'content'))
                AND {{policy}}.collection_permission(cid,operation,vid)
        $function$
