CREATE OR REPLACE FUNCTION {{policy}}.validate_approved_grid()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF (SELECT coalesce(sum(row_span*column_span),0) FROM {{corpus}}.approved_cell WHERE table_id=NEW.id)<>NEW.row_count*NEW.column_count THEN
                RAISE EXCEPTION 'Complete approved cell grid required' USING ERRCODE='23514'; END IF;
            RETURN NULL;
        END $function$
