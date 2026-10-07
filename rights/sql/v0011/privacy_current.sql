CREATE OR REPLACE FUNCTION {{policy}}.privacy_current(pid uuid, rev bigint, cid text, hash text DEFAULT NULL::text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(
                SELECT 1 FROM {{policy}}.privacy_review v
                JOIN {{policy}}.revision_head vh ON vh.kind='privacy' AND vh.scope_id=v.id::text AND vh.revision=v.revision
                JOIN {{policy}}.controller_record ctl ON ctl.id=v.controller_id AND ctl.revision=v.controller_revision
                JOIN {{policy}}.revision_head ch ON ch.kind='controller' AND ch.scope_id=ctl.id::text AND ch.revision=ctl.revision
                JOIN {{policy}}.actor va ON va.id=v.actor_id AND va.active
                JOIN {{policy}}.actor ca ON ca.id=ctl.actor_id AND ca.active
                WHERE v.id=pid AND v.revision=rev AND v.collection_id=cid AND ctl.collection_id=cid
                    AND v.state='approved' AND ctl.state='approved'
                    AND v.valid_from<=statement_timestamp() AND v.expires_at>statement_timestamp()
                    AND ctl.valid_from<=statement_timestamp() AND ctl.expires_at>statement_timestamp()
                    AND (v.clearance='cleared' OR (v.clearance='redacted' AND v.cleared_hash=hash)))
        $function$
