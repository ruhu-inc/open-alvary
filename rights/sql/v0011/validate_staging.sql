CREATE OR REPLACE FUNCTION {{policy}}.validate_staging()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE deadline timestamptz;
        BEGIN
            IF NOT {{policy}}.assigned(NEW.collection_id,{{vocabulary_acquire}}) THEN
                RAISE EXCEPTION 'Acquisition assignment required' USING ERRCODE='42501';
            END IF;
            IF TG_OP='UPDATE' THEN
                IF (NEW.id,NEW.collection_id,NEW.assessment_id,NEW.assessment_revision,NEW.artifact_hash,
                    NEW.size_bytes,NEW.acquired_at,NEW.retention_deadline)
                    IS DISTINCT FROM (OLD.id,OLD.collection_id,OLD.assessment_id,OLD.assessment_revision,
                    OLD.artifact_hash,OLD.size_bytes,OLD.acquired_at,OLD.retention_deadline)
                    OR (NEW.private_bytes IS DISTINCT FROM OLD.private_bytes AND NEW.state<>'erased') THEN
                    RAISE EXCEPTION 'Staging lineage is immutable' USING ERRCODE='55000';
                END IF;
                IF NEW.state='erased' THEN RETURN NEW; END IF;
 IF {{policy}}.lifecycle_transition_allowed(NEW.id,OLD.state,NEW.state) THEN RETURN NEW; END IF;

            END IF;
            SELECT retention_deadline INTO deadline FROM {{policy}}.acquisition_assessment
                WHERE id=NEW.assessment_id AND revision=NEW.assessment_revision AND collection_id=NEW.collection_id;
            IF NOT {{policy}}.staging_allowed(NEW.assessment_id,NEW.assessment_revision,NEW.collection_id,NEW.artifact_hash)
                OR deadline IS NULL OR NEW.retention_deadline>deadline OR NEW.acquired_at>statement_timestamp()
                OR NEW.state NOT IN ('staged','processing','validated') THEN
                RAISE EXCEPTION 'Current acquisition authority required' USING ERRCODE='42501';
            END IF;
            RETURN NEW;
        END $function$
