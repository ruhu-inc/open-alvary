CREATE OR REPLACE FUNCTION {{policy}}.gazette_binding_hash(bid uuid)
 RETURNS text
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT encode(sha256(convert_to((to_jsonb(g)-ARRAY['payload','state'])::text,'UTF8')),'hex') FROM {{staging}}.gazette_item_binding g WHERE id=bid
        $function$
