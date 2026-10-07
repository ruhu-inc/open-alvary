CREATE OR REPLACE FUNCTION {{policy}}.synthetic_binding_hash(bid uuid)
 RETURNS text
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT encode(sha256(convert_to((to_jsonb(b)-ARRAY['payload','state'])::text,'UTF8')),'hex') FROM {{staging}}.synthetic_expression_binding b WHERE b.id=bid
        $function$
