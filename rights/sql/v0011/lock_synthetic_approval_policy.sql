CREATE OR REPLACE FUNCTION {{policy}}.lock_synthetic_approval_policy(cid text, rights_rev bigint, verification_rev bigint)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            -- Lock reviewer actors before heads, matching command/revocation order.
            PERFORM 1 FROM {{policy}}.actor a WHERE a.id IN (SELECT actor_id FROM {{policy}}.collection_decision WHERE collection_id=cid AND revision=rights_rev UNION SELECT actor_id FROM {{policy}}.verification_policy WHERE collection_id=cid AND revision=verification_rev) ORDER BY a.id FOR SHARE;
            PERFORM 1 FROM {{policy}}.revision_head WHERE scope_id=cid AND kind IN ('collection','verification') ORDER BY kind FOR SHARE;
            IF NOT {{policy}}.approval_policy_current(cid,rights_rev,verification_rev) THEN RAISE EXCEPTION 'Independent exact current collection reviews required' USING ERRCODE='42501'; END IF;
        END $function$
