CREATE OR REPLACE FUNCTION {{policy}}.oa_norm(v text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT normalize(replace(replace(v,E'\r\n',E'\n'),E'\r',E'\n'),NFC)
        $function$
