CREATE OR REPLACE FUNCTION {{policy}}.gazette_item_policy_current(cid text, rights_rev bigint, verification_rev bigint)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT {{policy}}.approval_policy_current(cid,rights_rev,verification_rev) AND EXISTS(SELECT 1 FROM {{policy}}.collection_decision WHERE collection_id=cid AND revision=rights_rev AND retain='allow')
        $function$
