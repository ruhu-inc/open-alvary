CREATE OR REPLACE FUNCTION {{policy}}.current_actor()
 RETURNS uuid
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT a.id FROM {{policy}}.actor a JOIN pg_catalog.pg_roles r ON r.oid=a.database_role
            WHERE a.active AND NOT r.rolsuper AND NOT r.rolbypassrls AND r.rolname=session_user::text
                AND a.identity_kind='synthetic' AND (
                    (a.database_role_name IS NULL AND NOT r.rolcanlogin)
                    OR (r.rolcanlogin AND a.database_role_name=r.rolname
                        AND a.authenticated_until>statement_timestamp()
                        AND (r.rolvaliduntil IS NULL OR r.rolvaliduntil>statement_timestamp())
                        AND {{policy}}.operator_role_safe(r.oid)))
        $function$
