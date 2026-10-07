CREATE OR REPLACE FUNCTION {{policy}}.immutable_record()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$ BEGIN
            RAISE EXCEPTION 'Policy history is append-only' USING ERRCODE='55000';
        END $function$
