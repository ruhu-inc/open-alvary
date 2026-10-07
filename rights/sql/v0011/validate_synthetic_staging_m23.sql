CREATE OR REPLACE FUNCTION {{policy}}.validate_synthetic_staging_m23()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE actor uuid; native boolean; capacity bigint; occupied bigint; evidence uuid;
        BEGIN
            actor:={{policy}}.current_actor();
            SELECT a.database_role_name IS NOT NULL INTO native FROM {{policy}}.actor a WHERE a.id=actor;
            IF TG_OP='UPDATE' AND (NEW.purpose,NEW.acquired_by) IS DISTINCT FROM (OLD.purpose,OLD.acquired_by) THEN
                RAISE EXCEPTION 'Staging qualification is immutable' USING ERRCODE='55000'; END IF;
            IF NOT coalesce(native,false) THEN
                IF TG_OP='INSERT' AND (NEW.purpose IS NOT NULL OR NEW.acquired_by IS NOT NULL) THEN
                    RAISE EXCEPTION 'Native acquisition subject required' USING ERRCODE='42501'; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP='UPDATE' THEN
 IF {{policy}}.lifecycle_transition_allowed(NEW.id,OLD.state,NEW.state) THEN RETURN NEW; END IF;

                IF NEW.state='erased' AND OLD.state<>'erased' THEN
                    IF NOT EXISTS(SELECT 1 FROM {{policy}}.audit_event WHERE kind='staging_erasure'
                        AND scope_id=NEW.id::text AND revision=1 AND actor_id=actor) THEN
                        RAISE EXCEPTION 'Audited erasure required' USING ERRCODE='42501'; END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Native staging lifecycle writer unavailable' USING ERRCODE='42501';
            END IF;
            PERFORM 1 FROM {{policy}}.actor a WHERE a.id=actor FOR SHARE;
            PERFORM {{base}}.identity_check_legacy_authority(true);
            PERFORM {{policy}}.lock_acquisition_authority(NEW.assessment_id,NEW.assessment_revision,NEW.collection_id);
            IF NEW.acquired_by IS DISTINCT FROM actor OR NEW.purpose IS NULL OR NEW.state<>'staged'
                OR NOT isfinite(NEW.retention_deadline) OR NOT
                {{policy}}.private_operation_allowed(NEW.assessment_id,NEW.assessment_revision,NEW.collection_id,
                    NEW.purpose,NEW.artifact_hash,{{vocabulary_acquire}}) THEN
                RAISE EXCEPTION 'Exact synthetic acquisition authority required' USING ERRCODE='42501'; END IF;
            SELECT maximum_bytes INTO capacity FROM {{policy}}.synthetic_staging_limit WHERE collection_id=NEW.collection_id FOR UPDATE;
            IF capacity IS NULL THEN RAISE EXCEPTION 'Explicit synthetic staging limit required' USING ERRCODE='42501'; END IF;
            SELECT coalesce(sum(octet_length(private_bytes)),0) INTO occupied FROM {{staging}}.staged_artifact WHERE collection_id=NEW.collection_id;
            IF occupied+NEW.size_bytes>capacity THEN RAISE EXCEPTION 'Synthetic staging capacity exceeded' USING ERRCODE='54000'; END IF;
            SELECT evidence_id INTO evidence FROM {{policy}}.acquisition_assessment WHERE id=NEW.assessment_id
                AND revision=NEW.assessment_revision AND collection_id=NEW.collection_id;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(actor,NEW.collection_id,evidence,'synthetic_staging',NEW.id::text,1,'Original synthetic artifact staged');
            RETURN NEW;
        END $function$
