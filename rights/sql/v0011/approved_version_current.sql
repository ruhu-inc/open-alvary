CREATE OR REPLACE FUNCTION {{policy}}.approved_version_current(vid text, purpose_value text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT {{policy}}.approved_version_current_m33(vid,purpose_value) AND {{policy}}.gazette_dependencies_current(vid,purpose_value)
        $function$
