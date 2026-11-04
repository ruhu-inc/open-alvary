CREATE OR REPLACE FUNCTION {{policy}}.traffic_salt(candidate bytea)
 RETURNS bytea
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    today date := (statement_timestamp() AT TIME ZONE 'UTC')::date;
    days_salt bytea;
BEGIN
    -- The first instance to ask on a new day sets the day's salt and clears yesterday's, whether or not
    -- the daily job has run: without it, yesterday's sketch can no longer be matched to anyone.
    UPDATE {{accounts}}.traffic_visitors SET salt = NULL WHERE day < today AND traffic_visitors.salt IS NOT NULL;
    INSERT INTO {{accounts}}.traffic_visitors (day, salt) VALUES (today, candidate) ON CONFLICT (day) DO NOTHING;
    SELECT v.salt INTO days_salt FROM {{accounts}}.traffic_visitors AS v WHERE v.day = today;
    RETURN days_salt;
END
$function$
