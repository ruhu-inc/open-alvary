CREATE OR REPLACE FUNCTION {{policy}}.validate_assembly_update()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF TG_OP<>'UPDATE' OR (to_jsonb(NEW)-ARRAY['state','payload','parent_revision']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','payload','parent_revision'])
                OR (NEW.payload IS DISTINCT FROM OLD.payload AND NOT(NEW.state='erased' AND NEW.payload IS NULL))
                OR NOT ((NEW.state='held' AND OLD.state='active') OR (NEW.state='erasure_required' AND OLD.state IN ('active','held'))
                    OR (NEW.state='erased' AND OLD.state IN ('active','held','erasure_required')))
                OR NOT EXISTS(SELECT 1 FROM {{policy}}.assembly_lifecycle_event e WHERE e.artifact_id=NEW.artifact_id AND e.target=NEW.state
                    AND e.actor_id={{policy}}.current_actor() AND e.transaction_id=pg_current_xact_id()) THEN
                RAISE EXCEPTION 'Immutable audited assembly lifecycle required' USING ERRCODE='55000'; END IF;
            RETURN NEW;
        END $function$
