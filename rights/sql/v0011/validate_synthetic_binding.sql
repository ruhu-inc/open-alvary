CREATE OR REPLACE FUNCTION {{policy}}.validate_synthetic_binding()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE raw {{staging}}.staged_artifact;
        BEGIN
            SELECT a.* INTO raw FROM {{staging}}.staged_artifact a WHERE a.id=NEW.artifact_id FOR UPDATE;
            PERFORM {{base}}.identity_check_legacy_authority(true);
            IF raw.id IS NULL OR raw.state NOT IN ('staged','processing','validated') OR raw.retention_deadline<=statement_timestamp()
                OR NOT EXISTS(SELECT 1 FROM {{policy}}.evidence e WHERE e.id=NEW.evidence_id AND e.collection_id=NEW.collection_id)
                OR NEW.state<>'active' THEN RAISE EXCEPTION 'Current synthetic identity declaration required' USING ERRCODE='23514'; END IF;
            PERFORM 1 FROM {{base}}.identity_expressions x JOIN {{base}}.identity_works w ON w.id=x.work_id
                JOIN {{base}}.identity_manifestations m ON m.id=NEW.manifestation_id
                WHERE x.id=NEW.expression_id FOR SHARE OF x,w,m;
            IF NOT EXISTS(SELECT 1 FROM {{base}}.identity_expressions x JOIN {{base}}.identity_works w ON w.id=x.work_id
                JOIN {{base}}.identity_manifestations m ON m.id=NEW.manifestation_id WHERE x.id=NEW.expression_id
                AND w.identity_status='identified' AND NEW.identity_hash=encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(x),'manifestation',to_jsonb(m))::text,'UTF8')),'hex')) THEN RAISE EXCEPTION 'Exact declared identity facts required' USING ERRCODE='23514'; END IF;
            NEW.payload:=jsonb_build_object('binding_id',NEW.id,'identity_hash',NEW.identity_hash,'expression_id',NEW.expression_id,
                'manifestation_id',NEW.manifestation_id,'artifact_hash',NEW.artifact_hash);
            PERFORM {{policy}}.reserve_assembly_capacity(NEW.artifact_id,NEW.collection_id,octet_length(NEW.payload::text),1);
            RETURN NEW;
        END $function$
