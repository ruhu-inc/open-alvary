CREATE OR REPLACE FUNCTION {{policy}}.gazette_identity_current(bid uuid)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{staging}}.gazette_item_binding g JOIN {{base}}.identity_expressions x ON x.id=g.item_expression_id
                JOIN {{base}}.identity_works w ON w.id=x.work_id WHERE g.id=bid AND g.state='active' AND w.identity_status='identified'
                AND g.identity_hash=encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(x))::text,'UTF8')),'hex'))
        $function$
