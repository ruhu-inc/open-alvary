CREATE OR REPLACE FUNCTION {{policy}}.allocate_publication_revision()
 RETURNS bigint
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
    UPDATE {{policy}}.publication_revision SET revision = revision + 1 WHERE singleton RETURNING revision
$function$
