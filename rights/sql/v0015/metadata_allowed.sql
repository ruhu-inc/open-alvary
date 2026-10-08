CREATE OR REPLACE FUNCTION {{policy}}.metadata_allowed(cid text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$ SELECT {{policy}}.serving_allowed(cid) AND {{policy}}.collection_permission(cid,{{vocabulary_redistribute_metadata}}) $function$
