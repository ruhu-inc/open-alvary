CREATE OR REPLACE FUNCTION {{policy}}.validate_gazette_commit()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE b {{staging}}.gazette_item_binding;r {{staging}}.gazette_item_review;purpose_value text;gid uuid;
        BEGIN
            SELECT * INTO b FROM {{staging}}.gazette_item_binding WHERE id=NEW.binding_id;
            SELECT purpose INTO purpose_value FROM {{staging}}.staged_artifact WHERE id=b.artifact_id;
            IF NEW.actor_id IS DISTINCT FROM {{policy}}.current_actor() OR NOT {{policy}}.is_native_operator() OR NOT {{policy}}.assigned(b.item_collection_id,{{vocabulary_acquire}}) OR NOT {{policy}}.assigned(b.issue_collection_id,{{vocabulary_acquire}})
                OR NOT {{policy}}.gazette_identity_current(b.id) OR NOT {{policy}}.approved_version_current_m33(b.issue_version_id,purpose_value) THEN RAISE EXCEPTION 'Gazette authority expired before commit' USING ERRCODE='42501'; END IF;
            gid:=(NEW.result->>'correspondence_id')::uuid;
            IF gid IS NOT NULL THEN
                IF NOT {{policy}}.assigned(b.item_collection_id,'release') OR NOT {{policy}}.assigned(b.issue_collection_id,'release') OR NOT {{policy}}.gazette_correspondence_current(gid,purpose_value) THEN RAISE EXCEPTION 'Correspondence changed before commit' USING ERRCODE='42501'; END IF;
            ELSE
                SELECT * INTO r FROM {{staging}}.gazette_item_review WHERE command_id=NEW.command_id;
                IF r.state IS DISTINCT FROM 'active' OR NOT {{policy}}.assigned(b.item_collection_id,'content') OR NOT {{policy}}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision)
                    OR NOT EXISTS(SELECT 1 FROM {{corpus}}.representation_head h JOIN {{corpus}}.representation_revision rep ON rep.id=h.rep_id WHERE h.version_id=b.issue_version_id AND h.rep_id=r.representation_id AND rep.record_set_hash=r.record_set_hash) THEN RAISE EXCEPTION 'Identification changed before commit' USING ERRCODE='42501'; END IF;
            END IF;
            RETURN NULL;
        END $function$
