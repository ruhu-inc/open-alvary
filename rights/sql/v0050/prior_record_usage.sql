CREATE OR REPLACE FUNCTION {{policy}}.record_usage(entry jsonb)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    refusal text := entry->>'refusal';
BEGIN
    IF refusal !~ '^[a-z_]{1,40}$' THEN
        RAISE EXCEPTION 'Invalid usage entry' USING ERRCODE = '22023';
    END IF;
    INSERT INTO {{accounts}}.usage_day AS u (organisation_id, day, credential, class, requests, bytes, refusals)
    VALUES (entry->>'organisation', (statement_timestamp() AT TIME ZONE 'UTC')::date, entry->>'credential',
        entry->>'class', CASE WHEN refusal IS NULL THEN 1 ELSE 0 END,
        greatest(0, coalesce((entry->>'bytes')::bigint, 0)),
        CASE WHEN refusal IS NULL THEN '{}'::jsonb ELSE jsonb_build_object(refusal, 1) END)
    ON CONFLICT (organisation_id, day, credential, class) DO UPDATE SET
        requests = u.requests + EXCLUDED.requests,
        bytes = u.bytes + EXCLUDED.bytes,
        refusals = CASE WHEN refusal IS NULL THEN u.refusals
            ELSE jsonb_set(u.refusals, ARRAY[refusal], to_jsonb(coalesce((u.refusals->>refusal)::bigint, 0) + 1)) END;
END
$function$
