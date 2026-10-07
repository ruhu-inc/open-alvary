CREATE OR REPLACE FUNCTION {{policy}}.acquisition_current(aid uuid, rev bigint, cid text, purpose_value text, hash text DEFAULT NULL::text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{policy}}.acquisition_assessment a JOIN {{policy}}.revision_head h
                ON h.kind='acquisition' AND h.scope_id=a.id::text AND h.revision=a.revision
                JOIN {{policy}}.actor actor ON actor.id=a.actor_id AND actor.active
                JOIN {{base}}.identity_collections col ON col.id=a.collection_id
                WHERE a.id=aid AND a.revision=rev AND a.collection_id=cid AND a.purpose=purpose_value
                    AND a.state='approved' AND a.acquire='allow' AND a.retain='allow'
                    AND a.valid_from<=statement_timestamp() AND a.expires_at>statement_timestamp()
                    AND a.retention_deadline>statement_timestamp()
                    AND ((a.privacy_class='no_personal_data' AND a.privacy_review_id IS NULL AND col.document_class NOT IN ('judgment','other'))
                        OR {{policy}}.privacy_current(a.privacy_review_id,a.privacy_review_revision,cid,hash)))
        $function$
