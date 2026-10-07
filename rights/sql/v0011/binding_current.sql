CREATE OR REPLACE FUNCTION {{policy}}.binding_current(bid uuid)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{staging}}.synthetic_expression_binding b JOIN {{base}}.identity_expressions x ON x.id=b.expression_id
                JOIN {{base}}.identity_works w ON w.id=x.work_id JOIN {{base}}.identity_manifestations m ON m.id=b.manifestation_id
                WHERE b.id=bid AND b.state='active' AND w.identity_status='identified'
                AND b.identity_hash=encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(x),'manifestation',to_jsonb(m))::text,'UTF8')),'hex'))
        $function$
