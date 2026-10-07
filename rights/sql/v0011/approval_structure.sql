CREATE OR REPLACE FUNCTION {{policy}}.approval_structure(inputs jsonb, plan jsonb, projection jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE st jsonb;
        BEGIN
            st:={{policy}}.approval_structure_scope(jsonb_build_object('offset',0,'nodes','[]'::jsonb,'tables','[]'::jsonb,'order','[]'::jsonb),inputs,plan,projection,NULL,NULL);
            IF jsonb_array_length(st->'nodes')>200 THEN RAISE EXCEPTION 'Approved node bound exceeded' USING ERRCODE='54000'; END IF;
            RETURN st||jsonb_build_object('markers',projection->'markers','normalizations',projection->'normalizations','separators',projection->'separators');
        END $function$
