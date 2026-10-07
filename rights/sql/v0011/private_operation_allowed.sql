CREATE OR REPLACE FUNCTION {{policy}}.private_operation_allowed(aid uuid, rev bigint, cid text, requested_purpose text, hash text, operation text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT coalesce(operation IN ({{vocabulary_discover}},{{vocabulary_acquire}},{{vocabulary_retain}},{{vocabulary_derive}}) AND
                {{policy}}.assigned(cid,{{vocabulary_acquire}}) AND EXISTS (
                    SELECT 1 FROM {{policy}}.actor WHERE id={{policy}}.current_actor() AND database_role_name IS NOT NULL)
                AND {{policy}}.acquisition_current(aid,rev,cid,requested_purpose,hash) AND EXISTS (
                    SELECT 1 FROM {{policy}}.acquisition_assessment a WHERE a.id=aid AND a.revision=rev
                        AND a.collection_id=cid AND a.artifact_hash=hash AND a.purpose=requested_purpose
                        AND CASE operation WHEN {{vocabulary_discover}} THEN a.discover WHEN {{vocabulary_acquire}} THEN a.acquire
                            WHEN {{vocabulary_retain}} THEN a.retain WHEN {{vocabulary_derive}} THEN a.derive ELSE NULL END='allow'
                        AND (a.privacy_review_id IS NULL OR EXISTS (
                            SELECT 1 FROM {{policy}}.privacy_review v WHERE v.id=a.privacy_review_id
                                AND v.revision=a.privacy_review_revision AND v.collection_id=cid
                                AND v.purpose=requested_purpose))),false)
        $function$
