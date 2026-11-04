CREATE OR REPLACE FUNCTION {{policy}}.release_admission(lease uuid)
 RETURNS void
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$ DELETE FROM {{policy}}.admission_lease WHERE lease_id = lease $function$
