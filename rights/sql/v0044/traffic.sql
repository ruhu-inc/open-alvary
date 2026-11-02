CREATE TABLE {{accounts}}.traffic_day (
    day date NOT NULL,
    metric text NOT NULL CHECK (metric ~ '^[a-z][a-z_.]{0,39}$'),
    label text NOT NULL CHECK (label ~ '^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$'),
    count bigint NOT NULL CHECK (count >= 0),
    PRIMARY KEY (day, metric, label)
) WITH (fillfactor = 80);

CREATE TABLE {{accounts}}.traffic_record_day (
    day date NOT NULL,
    source_id text NOT NULL CHECK (source_id ~ '^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$'),
    views bigint NOT NULL DEFAULT 0 CHECK (views >= 0),
    texts bigint NOT NULL DEFAULT 0 CHECK (texts >= 0),
    PRIMARY KEY (day, source_id)
) PARTITION BY RANGE (day);

DO $partitions$
DECLARE
    this_month date := date_trunc('month', statement_timestamp() AT TIME ZONE 'UTC')::date;
    opening date;
BEGIN
    FOR ahead IN 0..3 LOOP
        opening := this_month + make_interval(months => ahead);
        EXECUTE format(
            'CREATE TABLE {{accounts}}.%I PARTITION OF {{accounts}}.traffic_record_day'
            ' FOR VALUES FROM (%L) TO (%L) WITH (fillfactor = 80)',
            'traffic_record_day_' || to_char(opening, 'YYYY_MM'), opening, (opening + interval '1 month')::date
        );
    END LOOP;
END
$partitions$;

CREATE TABLE {{accounts}}.traffic_record_month (
    month date NOT NULL CHECK (extract(day FROM month) = 1),
    source_id text NOT NULL CHECK (source_id ~ '^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$'),
    views bigint NOT NULL DEFAULT 0 CHECK (views >= 0),
    texts bigint NOT NULL DEFAULT 0 CHECK (texts >= 0),
    PRIMARY KEY (month, source_id)
) WITH (fillfactor = 80);

CREATE TABLE {{accounts}}.traffic_top (
    period text NOT NULL CHECK (period ~ '^([0-9]{1,3}d|[0-9]{4}-[0-9]{2})$'),
    rank smallint NOT NULL CHECK (rank >= 1),
    source_id text NOT NULL CHECK (source_id ~ '^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$'),
    views bigint NOT NULL CHECK (views >= 0),
    computed_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (period, rank)
);

CREATE TABLE {{accounts}}.traffic_visitors (
    day date PRIMARY KEY,
    salt bytea CHECK (octet_length(salt) = 32),
    registers smallint[] NOT NULL DEFAULT array_fill(0::smallint, ARRAY[4096])
        CHECK (cardinality(registers) = 4096)
);

CREATE FUNCTION {{policy}}.record_traffic(batch jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
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
$function$;

CREATE FUNCTION {{policy}}.traffic_salt(candidate bytea) RETURNS bytea
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
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
$function$;

REVOKE ALL ON FUNCTION {{policy}}.record_traffic(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.traffic_salt(bytea) FROM PUBLIC;
