CREATE OR REPLACE FUNCTION {{policy}}.assigned(cid text, cap text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{policy}}.assignment WHERE actor_id={{policy}}.current_actor()
                AND collection_id=cid AND capability=cap)
        $function$
