CREATE OR REPLACE FUNCTION {{policy}}.validate_approved_cell()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE t {{corpus}}.approved_table;
        BEGIN
            SELECT * INTO t FROM {{corpus}}.approved_table WHERE id=NEW.table_id;
            IF NEW.row_index+NEW.row_span>t.row_count OR NEW.column_index+NEW.column_span>t.column_count
                OR EXISTS(SELECT 1 FROM {{corpus}}.approved_cell a WHERE a.table_id=t.id AND a.row_index<NEW.row_index+NEW.row_span AND a.row_index+a.row_span>NEW.row_index AND a.column_index<NEW.column_index+NEW.column_span AND a.column_index+a.column_span>NEW.column_index)
                THEN RAISE EXCEPTION 'Invalid or overlapping approved cell' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $function$
