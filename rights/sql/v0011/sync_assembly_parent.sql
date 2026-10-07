CREATE OR REPLACE FUNCTION {{policy}}.sync_assembly_parent()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF NEW.state IN ('held_pending_reassessment','erasure_required','erased') AND NEW.state IS DISTINCT FROM OLD.state THEN
                PERFORM {{policy}}.transition_assembly(NEW.id,CASE NEW.state WHEN 'held_pending_reassessment' THEN 'held' ELSE NEW.state END,'raw_parent_state');
            END IF;
            RETURN NEW;
        END $function$
