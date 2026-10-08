CREATE OR REPLACE FUNCTION {{policy}}.approval_policy_current(cid text, rights_rev bigint, verification_rev bigint)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{policy}}.collection_decision d JOIN {{policy}}.verification_policy v USING(collection_id)
                JOIN {{policy}}.revision_head dh ON dh.kind='collection' AND dh.scope_id=cid AND dh.revision=d.revision
                JOIN {{policy}}.revision_head vh ON vh.kind='verification' AND vh.scope_id=cid AND vh.revision=v.revision
                JOIN {{policy}}.actor da ON da.id=d.actor_id AND da.active JOIN {{policy}}.actor va ON va.id=v.actor_id AND va.active
                WHERE d.collection_id=cid AND d.revision=rights_rev AND v.revision=verification_rev AND d.actor_id<>v.actor_id
                AND d.state='approved' AND v.state='approved' AND d.valid_from<=statement_timestamp() AND d.expires_at>statement_timestamp()
                AND v.valid_from<=statement_timestamp() AND v.expires_at>statement_timestamp()
                AND EXISTS(SELECT 1 FROM {{policy}}.assignment WHERE actor_id=da.id AND collection_id=cid AND capability='rights')
                AND EXISTS(SELECT 1 FROM {{policy}}.assignment WHERE actor_id=va.id AND collection_id=cid AND capability='content')
                AND d.metadata_privacy_class='no_personal_data' AND d.privacy_review_id IS NULL
                AND d.basis_type<>'unknown' AND d.conditions_satisfied AND d.redistribute_text='allow' AND d.quote='allow' AND d.derive='allow'
                AND (SELECT count(*) FROM {{policy}}.verification_material m WHERE m.collection_id=cid AND m.revision=v.revision)=6)
        $function$
