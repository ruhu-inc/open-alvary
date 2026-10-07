CREATE OR REPLACE FUNCTION {{policy}}.is_native_operator()
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{policy}}.actor WHERE id={{policy}}.current_actor() AND database_role_name IS NOT NULL)
        $function$
