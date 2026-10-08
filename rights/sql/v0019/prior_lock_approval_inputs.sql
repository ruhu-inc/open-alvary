CREATE OR REPLACE FUNCTION {{policy}}.lock_approval_inputs(cid text, rights_expected bigint, content_expected bigint)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE d {{policy}}.collection_decision; v {{policy}}.verification_policy;
        BEGIN
            IF NOT {{policy}}.assigned(cid,'release') THEN
                RAISE EXCEPTION 'Release assignment required' USING ERRCODE='42501';
            END IF;
            PERFORM 1 FROM {{policy}}.revision_head WHERE scope_id=cid AND kind IN ('collection','verification')
                ORDER BY kind FOR UPDATE;
            SELECT r.* INTO d FROM {{policy}}.collection_decision r JOIN {{policy}}.revision_head h
                ON h.kind='collection' AND h.scope_id=r.collection_id AND h.revision=r.revision WHERE r.collection_id=cid;
            SELECT r.* INTO v FROM {{policy}}.verification_policy r JOIN {{policy}}.revision_head h
                ON h.kind='verification' AND h.scope_id=r.collection_id AND h.revision=r.revision WHERE r.collection_id=cid;
            IF d.revision IS DISTINCT FROM rights_expected OR v.revision IS DISTINCT FROM content_expected THEN
                RAISE EXCEPTION 'Stale approval inputs' USING ERRCODE='40001';
            END IF;
            IF d.state<>'approved' OR v.state<>'approved' OR d.actor_id=v.actor_id
                OR d.valid_from>statement_timestamp() OR d.expires_at<=statement_timestamp()
                OR v.valid_from>statement_timestamp() OR v.expires_at<=statement_timestamp()
                OR NOT EXISTS(SELECT 1 FROM {{policy}}.actor WHERE id=d.actor_id AND active)
                OR NOT EXISTS(SELECT 1 FROM {{policy}}.actor WHERE id=v.actor_id AND active) THEN
                RAISE EXCEPTION 'Independent current reviews required' USING ERRCODE='42501';
            END IF;
            IF (SELECT count(*) FROM {{policy}}.verification_material WHERE collection_id=cid AND revision=v.revision)<>6 THEN
                RAISE EXCEPTION 'Material coverage incomplete' USING ERRCODE='23514';
            END IF;
            RETURN true;
        END $function$
