CREATE OR REPLACE FUNCTION {{policy}}.validate_approval_commit()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE review {{staging}}.snapshot_review;version {{corpus}}.approved_version;purpose_value text;commit_meta {{policy}}.approval_commit;
        BEGIN
            IF NEW.actor_id IS DISTINCT FROM {{policy}}.current_actor() OR NOT {{policy}}.is_native_operator() OR NOT {{policy}}.assigned(NEW.collection_id,{{vocabulary_acquire}}) THEN RAISE EXCEPTION 'Native approval authority expired before commit' USING ERRCODE='42501'; END IF;
            IF NEW.result->>'version_id' IS NULL THEN
                SELECT * INTO review FROM {{staging}}.snapshot_review WHERE command_id=NEW.command_id;
                IF review.id IS NULL OR review.state<>'active' OR review.actor_id<>NEW.actor_id OR NOT {{policy}}.assigned(NEW.collection_id,'content')
                    OR NOT {{policy}}.binding_current(review.binding_id) OR NOT {{policy}}.canonical_current(review.proposal_id,review.payload->>'purpose')
                    OR NOT EXISTS(SELECT 1 FROM {{policy}}.verification_policy v JOIN {{policy}}.revision_head h ON h.kind='verification' AND h.scope_id=v.collection_id AND h.revision=v.revision
                        WHERE v.collection_id=NEW.collection_id AND v.revision=review.verification_revision AND v.actor_id=NEW.actor_id AND v.state='approved' AND v.valid_from<=statement_timestamp() AND v.expires_at>statement_timestamp())
                    THEN RAISE EXCEPTION 'Exact source comparison expired before commit' USING ERRCODE='42501'; END IF;
            ELSE
                SELECT * INTO version FROM {{corpus}}.approved_version WHERE id=NEW.result->>'version_id';
                SELECT purpose INTO purpose_value FROM {{staging}}.staged_artifact WHERE id=version.artifact_id;
                IF NOT {{policy}}.assigned(NEW.collection_id,'release') OR NOT {{policy}}.approved_version_current_m33(version.id,purpose_value) THEN RAISE EXCEPTION 'Approval inputs expired before commit' USING ERRCODE='42501'; END IF;
                SELECT * INTO commit_meta FROM {{policy}}.approval_commit WHERE rep_id=NEW.result->>'representation_id';
                SELECT * INTO review FROM {{staging}}.snapshot_review WHERE id=commit_meta.review_id;
                IF NOT {{policy}}.canonical_current(review.proposal_id,purpose_value) THEN RAISE EXCEPTION 'Snapshot changed before commit' USING ERRCODE='42501'; END IF;
            END IF;
            RETURN NULL;
        END $function$
