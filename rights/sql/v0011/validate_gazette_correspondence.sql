CREATE OR REPLACE FUNCTION {{policy}}.validate_gazette_correspondence()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM {{staging}}.gazette_item_binding b JOIN {{staging}}.gazette_item_review r ON r.binding_id=b.id
                JOIN {{corpus}}.version_node n ON n.id=b.notice_node_id AND n.version_id=b.issue_version_id
                JOIN {{corpus}}.representation_revision rep ON rep.id=r.representation_id AND rep.version_id=b.issue_version_id
                WHERE b.id=NEW.binding_id AND r.id=NEW.review_id AND NEW.artifact_id=b.artifact_id AND r.artifact_id=b.artifact_id
                    AND NEW.issue_collection_id=b.issue_collection_id AND NEW.item_collection_id=b.item_collection_id
                    AND NEW.representation_id=r.representation_id AND NEW.start_byte=n.start_byte AND NEW.end_byte=n.end_byte AND NEW.span_hash=n.span_hash)
                THEN RAISE EXCEPTION 'Exact issue-owned reviewed notice correspondence required' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $function$
