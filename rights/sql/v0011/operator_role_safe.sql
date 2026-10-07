CREATE OR REPLACE FUNCTION {{policy}}.operator_role_safe(subject oid)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            WITH RECURSIVE inherited(oid) AS (
                SELECT subject UNION
                SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN inherited i ON i.oid=m.member
            ) SELECT NOT EXISTS (
                SELECT 1 FROM inherited i JOIN pg_catalog.pg_roles r ON r.oid=i.oid
                WHERE r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
                    OR (r.oid<>subject AND r.rolname NOT IN ({{role_literal_698c8026af}},{{role_literal_64bc633714}},{{role_literal_0efa318b48}},{{role_literal_7d239390fb}})))
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid=subject
                    AND r.rolname IN ({{role_literal_698c8026af}},{{role_literal_64bc633714}},{{role_literal_0efa318b48}},{{role_literal_7d239390fb}},{{role_literal_fbce7fd9ca}}))
                AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname IN ({{base_literal}},{{policy_literal}},{{staging_literal}},{{corpus_literal}})
                    AND has_schema_privilege(subject,n.oid,'CREATE'))
                AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname IN ({{base_literal}},{{policy_literal}},{{staging_literal}},{{corpus_literal}}) AND (c.relowner=subject OR
                        (n.nspname={{base_literal}} AND c.relkind IN ('r','p') AND
                            has_table_privilege(subject,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE'))))
        $function$
