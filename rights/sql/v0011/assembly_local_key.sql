CREATE OR REPLACE FUNCTION {{policy}}.assembly_local_key(value jsonb, nullable boolean)
 RETURNS boolean
 LANGUAGE sql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT coalesce((nullable AND (value IS NULL OR value='null'::jsonb)) OR
                (jsonb_typeof(value)='string' AND value#>>'{}' ~ '^[a-zA-Z0-9:_-]{1,64}$'),false)
        $function$
