CREATE OR REPLACE FUNCTION {{policy}}.validate_snapshot_head()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF (NEW.id,NEW.run_id) IS DISTINCT FROM (OLD.id,OLD.run_id) OR NEW.revision<>OLD.revision+1 THEN
                RAISE EXCEPTION 'Snapshot head cannot retarget or rewind' USING ERRCODE='55000'; END IF;
            RETURN NEW;
        END $function$
