CREATE OR REPLACE FUNCTION {{policy}}.operator_keys(data jsonb, required text[], optional text[] DEFAULT ARRAY[]::text[])
 RETURNS boolean
 LANGUAGE sql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT CASE WHEN jsonb_typeof(data)='object' THEN
                data ?& required AND NOT EXISTS (
                    SELECT 1 FROM jsonb_object_keys(data) k WHERE NOT k=ANY(required||optional))
                ELSE false END
        $function$
