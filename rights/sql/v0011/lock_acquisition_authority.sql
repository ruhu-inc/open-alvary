CREATE OR REPLACE FUNCTION {{policy}}.lock_acquisition_authority(aid uuid, rev bigint, cid text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE assessment {{policy}}.acquisition_assessment; privacy {{policy}}.privacy_review; ctl {{policy}}.controller_record;
        BEGIN
            SELECT * INTO assessment FROM {{policy}}.acquisition_assessment WHERE id=aid AND revision=rev AND collection_id=cid;
            SELECT * INTO privacy FROM {{policy}}.privacy_review WHERE id=assessment.privacy_review_id
                AND revision=assessment.privacy_review_revision AND collection_id=cid;
            SELECT * INTO ctl FROM {{policy}}.controller_record WHERE id=privacy.controller_id
                AND revision=privacy.controller_revision AND collection_id=cid;
            PERFORM 1 FROM {{policy}}.actor WHERE id IN (assessment.actor_id,privacy.actor_id,ctl.actor_id)
                ORDER BY id FOR SHARE;
            PERFORM 1 FROM {{policy}}.revision_head WHERE (kind='acquisition' AND scope_id=aid::text)
                OR (kind='privacy' AND scope_id=privacy.id::text)
                OR (kind='controller' AND scope_id=ctl.id::text) ORDER BY kind,scope_id FOR SHARE;
        END $function$
