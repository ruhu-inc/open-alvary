CREATE OR REPLACE FUNCTION {{policy}}.record_traffic(batch jsonb)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    today date := (statement_timestamp() AT TIME ZONE 'UTC')::date;
    sketch jsonb;
BEGIN
    -- In key order, so that instances writing at once cannot deadlock. A count for a day long past (an
    -- instance left idle) is dropped: it would land in a month that may be gone.
    INSERT INTO {{accounts}}.traffic_day AS t (day, metric, label, count)
    SELECT r.day, r.metric, r.label, sum(r.n)
    FROM jsonb_to_recordset(coalesce(batch->'counts', '[]')) AS r(day date, metric text, label text, n bigint)
    WHERE r.n > 0 AND r.day BETWEEN today - 35 AND today + 1
    GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    ON CONFLICT (day, metric, label) DO UPDATE SET count = t.count + EXCLUDED.count;

    INSERT INTO {{accounts}}.traffic_record_day AS t (day, source_id, views, texts)
    SELECT r.day, r.source_id, sum(r.views), sum(r.texts)
    FROM jsonb_to_recordset(coalesce(batch->'records', '[]'))
        AS r(day date, source_id text, views bigint, texts bigint)
    WHERE r.views >= 0 AND r.texts >= 0 AND r.day BETWEEN today - 35 AND today + 1
    GROUP BY 1, 2 ORDER BY 1, 2
    ON CONFLICT (day, source_id) DO UPDATE SET views = t.views + EXCLUDED.views, texts = t.texts + EXCLUDED.texts;

    INSERT INTO {{accounts}}.traffic_record_month AS t (month, source_id, views, texts)
    SELECT date_trunc('month', r.day)::date, r.source_id, sum(r.views), sum(r.texts)
    FROM jsonb_to_recordset(coalesce(batch->'records', '[]'))
        AS r(day date, source_id text, views bigint, texts bigint)
    WHERE r.views >= 0 AND r.texts >= 0 AND r.day BETWEEN today - 35 AND today + 1
    GROUP BY 1, 2 ORDER BY 1, 2
    ON CONFLICT (month, source_id) DO UPDATE SET views = t.views + EXCLUDED.views, texts = t.texts + EXCLUDED.texts;

    FOR sketch IN SELECT value FROM jsonb_array_elements(coalesce(batch->'visitors', '[]')) LOOP
        UPDATE {{accounts}}.traffic_visitors AS v
        SET registers = (
            SELECT array_agg(greatest(kept, added) ORDER BY place)
            FROM unnest(
                v.registers,
                ARRAY(SELECT e::smallint FROM jsonb_array_elements_text(sketch->'registers') AS e)
            ) WITH ORDINALITY AS m(kept, added, place)
        )
        WHERE v.day = (sketch->>'day')::date;
    END LOOP;
END
$function$
