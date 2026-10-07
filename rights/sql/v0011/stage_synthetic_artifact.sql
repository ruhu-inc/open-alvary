CREATE OR REPLACE FUNCTION {{policy}}.stage_synthetic_artifact(artifact_id uuid, cid text, assessment uuid, rev bigint, requested_purpose text, bytes bytea, deadline timestamp with time zone)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE existing {{staging}}.staged_artifact; actor uuid; hash text;
        BEGIN
            actor:={{policy}}.current_actor();
            IF actor IS NULL OR NOT EXISTS(SELECT 1 FROM {{policy}}.actor a WHERE a.id=actor AND a.database_role_name IS NOT NULL)
                OR NOT {{policy}}.assigned(cid,{{vocabulary_acquire}}) THEN
                RAISE EXCEPTION 'Native acquisition subject required' USING ERRCODE='42501'; END IF;
            PERFORM 1 FROM {{policy}}.actor a WHERE a.id=actor FOR SHARE;
            PERFORM {{base}}.identity_check_legacy_authority(true);
            IF bytes IS NULL OR octet_length(bytes)>10485760 THEN RAISE EXCEPTION 'Artifact bound exceeded' USING ERRCODE='22023'; END IF;
            hash:=encode(sha256(bytes),'hex');
            PERFORM {{policy}}.lock_acquisition_authority(assessment,rev,cid);
            IF NOT {{policy}}.private_operation_allowed(assessment,rev,cid,requested_purpose,hash,{{vocabulary_acquire}}) THEN
                RAISE EXCEPTION 'Exact current acquisition authority required' USING ERRCODE='42501'; END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_2e686ad867}}||artifact_id::text,0));
            SELECT * INTO existing FROM {{staging}}.staged_artifact WHERE id=artifact_id FOR UPDATE;
            IF existing.id IS NOT NULL THEN
                IF (existing.collection_id,existing.assessment_id,existing.assessment_revision,existing.purpose,
                    existing.artifact_hash,existing.retention_deadline,existing.acquired_by) IS DISTINCT FROM
                    (cid,assessment,rev,requested_purpose,hash,deadline,actor) THEN
                    RAISE EXCEPTION 'Staging retry changed' USING ERRCODE='40001'; END IF;
                IF existing.private_bytes IS NULL OR existing.retention_deadline<=statement_timestamp() THEN
                    RAISE EXCEPTION 'Retry cannot restore erased or expired bytes' USING ERRCODE='42501'; END IF;
                RETURN true;
            END IF;
            INSERT INTO {{staging}}.staged_artifact(id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,
                private_bytes,retention_deadline,purpose,acquired_by)
            VALUES(artifact_id,cid,assessment,rev,hash,octet_length(bytes),bytes,deadline,requested_purpose,actor);
            RETURN false;
        END $function$
