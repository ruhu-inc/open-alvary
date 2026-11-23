-- The home page's live totals (owner, 2026-11-23): how the site and the API are used over the last 30 days,
-- summed over everyone. Read-only. It answers about no one: it returns totals of the day counts the website
-- and the credential gate already keep, never a row, an organisation, a credential or a searched word, and a
-- total under 10 comes back as null so that no single visitor or organisation shows through.
CREATE FUNCTION {{policy}}.public_usage_totals() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
    WITH bounds AS (
        SELECT (statement_timestamp() AT TIME ZONE 'UTC')::date AS today
    ),
    site AS (
        SELECT
            coalesce(sum(t.count) FILTER (WHERE t.metric = 'records' AND t.label = 'texts'), 0) AS texts_read,
            coalesce(sum(t.count) FILTER (WHERE t.metric = 'searches' AND t.label = 'all'), 0) AS searches,
            coalesce(sum(t.count) FILTER (WHERE t.metric = 'searches' AND t.label = 'none_found'), 0) AS empty
        FROM {{accounts}}.traffic_day AS t, bounds AS b
        WHERE t.day > b.today - 30
    ),
    calls AS (
        SELECT
            coalesce(sum(u.requests) FILTER (WHERE u.class IN ('rest', 'mcp')), 0) AS requests,
            count(DISTINCT u.organisation_id) FILTER (WHERE u.day > b.today - 7 AND u.requests > 0) AS organisations
        FROM {{accounts}}.usage_day AS u, bounds AS b
        WHERE u.day > b.today - 30
    )
    SELECT jsonb_build_object(
        'days', 30,
        'texts_read', CASE WHEN site.texts_read >= 10 THEN site.texts_read END,
        'searches', CASE WHEN site.searches >= 10 THEN site.searches END,
        'searches_answered', CASE WHEN site.searches - site.empty >= 10 THEN site.searches - site.empty END,
        'api_requests', CASE WHEN calls.requests >= 10 THEN calls.requests END,
        'organisations_week', CASE WHEN calls.organisations >= 10 THEN calls.organisations END
    )
    FROM site, calls
$function$;

REVOKE ALL ON FUNCTION {{policy}}.public_usage_totals() FROM PUBLIC;
