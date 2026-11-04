CREATE OR REPLACE FUNCTION {{policy}}.admit(request jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    now_at timestamptz := statement_timestamp();
    item jsonb;
    bucket {{policy}}.admission_bucket;
    seconds numeric := (request->>'lease_seconds')::numeric;
    waits numeric[] := ARRAY[]::numeric[];
    blocked text[] := ARRAY[]::text[];
    remaining jsonb := '{}'::jsonb;
    active bigint;
    lease uuid := gen_random_uuid();
BEGIN
    IF jsonb_typeof(request->'buckets') <> 'array' OR jsonb_typeof(request->'concurrency') <> 'array'
        OR jsonb_array_length(request->'buckets') > 8 OR jsonb_array_length(request->'concurrency') > 8
        OR seconds IS NULL OR seconds <= 0 OR seconds > 600 THEN
        RAISE EXCEPTION 'Invalid admission request' USING ERRCODE = '22023';
    END IF;
    -- Stable lock order: every bucket row by key, then every concurrency scope by key.
    FOR item IN SELECT value FROM jsonb_array_elements(request->'buckets') ORDER BY value->>'key' LOOP
        INSERT INTO {{policy}}.admission_bucket(scope_key,capacity,refill_per_minute,tokens,last_refill_at)
            VALUES (item->>'key', (item->>'capacity')::numeric, (item->>'refill_per_minute')::numeric,
                (item->>'capacity')::numeric, now_at)
            ON CONFLICT (scope_key) DO NOTHING;
        SELECT * INTO bucket FROM {{policy}}.admission_bucket WHERE scope_key = item->>'key' FOR UPDATE;
        -- Clock rollback adds nothing and never removes tokens; catch-up is never counted twice.
        bucket.capacity := (item->>'capacity')::numeric;
        bucket.refill_per_minute := (item->>'refill_per_minute')::numeric;
        bucket.tokens := least(bucket.capacity, bucket.tokens
            + greatest(0, extract(epoch FROM now_at - bucket.last_refill_at)) * bucket.refill_per_minute / 60);
        bucket.last_refill_at := greatest(bucket.last_refill_at, now_at);
        UPDATE {{policy}}.admission_bucket SET capacity = bucket.capacity,
            refill_per_minute = bucket.refill_per_minute, tokens = bucket.tokens,
            last_refill_at = bucket.last_refill_at WHERE scope_key = bucket.scope_key;
        IF bucket.tokens < 1 THEN
            blocked := blocked || bucket.scope_key;
            waits := waits || ((1 - bucket.tokens) * 60 / bucket.refill_per_minute);
        END IF;
    END LOOP;
    FOR item IN SELECT value FROM jsonb_array_elements(request->'concurrency') ORDER BY value->>'key' LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('admission:' || (item->>'key'), 0));
        DELETE FROM {{policy}}.admission_lease WHERE scope_key = item->>'key' AND expires_at <= now_at;
        SELECT count(*) INTO active FROM {{policy}}.admission_lease WHERE scope_key = item->>'key';
        IF active >= (item->>'limit')::bigint THEN
            blocked := blocked || (item->>'key');
            waits := waits || (SELECT extract(epoch FROM min(expires_at) - now_at)
                FROM {{policy}}.admission_lease WHERE scope_key = item->>'key');
        END IF;
    END LOOP;
    IF cardinality(waits) > 0 THEN
        -- A rejected admission debits nothing and leases nothing.
        RETURN jsonb_build_object('admitted', false, 'blocked', to_jsonb(blocked),
            'retry_after', greatest(1, ceil((SELECT max(w) FROM unnest(waits) w))));
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(request->'buckets') ORDER BY value->>'key' LOOP
        UPDATE {{policy}}.admission_bucket SET tokens = tokens - 1 WHERE scope_key = item->>'key'
            RETURNING * INTO bucket;
        remaining := remaining || jsonb_build_object(bucket.scope_key, jsonb_build_object(
            'remaining', floor(bucket.tokens),
            'reset', ceil((bucket.capacity - bucket.tokens) * 60 / bucket.refill_per_minute)));
    END LOOP;
    INSERT INTO {{policy}}.admission_lease(lease_id,scope_key,phase,expires_at)
        SELECT lease, value->>'key', 'retrieval', now_at + make_interval(secs => seconds)
        FROM jsonb_array_elements(request->'concurrency');
    RETURN jsonb_build_object('admitted', true, 'lease_id', lease, 'buckets', remaining);
END
$function$
