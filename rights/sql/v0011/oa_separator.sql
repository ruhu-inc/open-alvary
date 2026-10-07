CREATE OR REPLACE FUNCTION {{policy}}.oa_separator(st jsonb, kind text)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE bytes integer; value text;
        BEGIN
            bytes:=octet_length(st->>'text');
            value:=CASE WHEN kind IN ('block','cell') THEN E'\n\n' WHEN kind='column' THEN E'\t' ELSE E'\n' END;
            st:=jsonb_set(st,ARRAY['separators',kind],(st->'separators'->kind)||to_jsonb(bytes));
            RETURN jsonb_set(st,'{text}',to_jsonb((st->>'text')||value));
        END $function$
