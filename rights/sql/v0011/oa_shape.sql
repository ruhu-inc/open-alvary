CREATE OR REPLACE FUNCTION {{policy}}.oa_shape(body jsonb, required text[], optional text[], arrays text[])
 RETURNS void
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE k text;
        BEGIN
            IF NOT {{policy}}.operator_keys(body,required,optional) THEN RAISE EXCEPTION 'Invalid canonical shape' USING ERRCODE='22023'; END IF;
            FOREACH k IN ARRAY arrays LOOP
                IF jsonb_typeof(coalesce(body->k,'[]'::jsonb)) IS DISTINCT FROM 'array'
                    OR jsonb_array_length(coalesce(body->k,'[]'::jsonb))>10000 THEN RAISE EXCEPTION 'Bounded canonical array required' USING ERRCODE='22023'; END IF;
            END LOOP;
        END $function$
