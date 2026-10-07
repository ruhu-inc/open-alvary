CREATE OR REPLACE FUNCTION {{policy}}.lock_gazette_dependencies(vid text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE item record;rights_rev bigint;verification_rev bigint;
        BEGIN
            FOR item IN SELECT b.id,b.item_collection_id FROM {{staging}}.gazette_item_binding b WHERE b.issue_version_id=vid ORDER BY b.item_collection_id,b.id FOR SHARE LOOP
                IF NOT {{policy}}.assigned(item.item_collection_id,{{vocabulary_acquire}}) THEN RAISE EXCEPTION 'Independent item acquisition assignment required' USING ERRCODE='42501'; END IF;
                PERFORM 1 FROM {{corpus}}.gazette_correspondence_head WHERE binding_id=item.id FOR SHARE;
                SELECT r.item_rights_revision,r.item_verification_revision INTO rights_rev,verification_rev FROM {{staging}}.gazette_item_review r
                    JOIN {{corpus}}.gazette_correspondence g ON g.review_id=r.id JOIN {{corpus}}.gazette_correspondence_head h ON h.correspondence_id=g.id WHERE h.binding_id=item.id;
                PERFORM {{policy}}.lock_synthetic_approval_policy(item.item_collection_id,rights_rev,verification_rev);
            END LOOP;
        END $function$
