CREATE OR REPLACE FUNCTION {{policy}}.reserve_bytes(request jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    now_at timestamptz := statement_timestamp();
    amount bigint := (request->>'bytes')::bigint;
    seconds numeric := (request->>'lease_seconds')::numeric;
    item jsonb;
    bucket {{policy}}.byte_bucket;
    blocked text[] := ARRAY[]::text[];
    waits numeric[] := ARRAY[]::numeric[];
    existing uuid;
    reservation uuid := gen_random_uuid();
BEGIN
    IF amount IS NULL OR amount < 1 OR seconds IS NULL OR seconds <= 0 OR seconds > 3600
        OR jsonb_typeof(request->'scopes') <> 'array' OR jsonb_array_length(request->'scopes') NOT BETWEEN 1 AND 8
        OR (request->>'admission_id') IS NULL THEN
        RAISE EXCEPTION 'Invalid byte reservation request' USING ERRCODE = '22023';
    END IF;
    SELECT reservation_id INTO existing FROM {{policy}}.byte_reservation
        WHERE admission_id = (request->>'admission_id')::uuid;
    IF existing IS NOT NULL THEN
        RETURN jsonb_build_object('reserved', true, 'reservation_id', existing, 'replayed', true);
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(request->'scopes') ORDER BY value->>'key' LOOP
        INSERT INTO {{policy}}.byte_bucket(scope_key,generation_id,capacity,refill_per_minute,last_refill_at)
            VALUES (item->>'key', gen_random_uuid(), (item->>'capacity')::bigint,
                (item->>'refill_per_minute')::numeric, now_at)
            ON CONFLICT (scope_key) DO NOTHING;
        SELECT * INTO bucket FROM {{policy}}.byte_bucket WHERE scope_key = item->>'key' FOR UPDATE;
        -- Capacity and refill changes apply in place; held and spent are never rescaled.
        bucket.capacity := (item->>'capacity')::bigint;
        bucket.refill_per_minute := (item->>'refill_per_minute')::numeric;
        bucket.spent := greatest(0, bucket.spent
            - greatest(0, extract(epoch FROM now_at - bucket.last_refill_at)) * bucket.refill_per_minute / 60);
        bucket.last_refill_at := greatest(bucket.last_refill_at, now_at);
        UPDATE {{policy}}.byte_bucket SET capacity = bucket.capacity, refill_per_minute = bucket.refill_per_minute,
            spent = bucket.spent, last_refill_at = bucket.last_refill_at WHERE scope_key = bucket.scope_key;
        IF bucket.capacity - bucket.held - bucket.spent < amount THEN
            blocked := blocked || bucket.scope_key;
            waits := waits || ((amount - (bucket.capacity - bucket.held - bucket.spent)) * 60 / bucket.refill_per_minute);
        END IF;
    END LOOP;
    IF cardinality(blocked) > 0 THEN
        RETURN jsonb_build_object('reserved', false, 'blocked', to_jsonb(blocked),
            'retry_after', greatest(1, ceil((SELECT max(w) FROM unnest(waits) w))));
    END IF;
    INSERT INTO {{policy}}.byte_reservation(reservation_id,admission_id,reserved_bytes,state,lease_expires_at)
        VALUES (reservation, (request->>'admission_id')::uuid, amount, 'reserved', now_at + make_interval(secs => seconds));
    FOR item IN SELECT value FROM jsonb_array_elements(request->'scopes') ORDER BY value->>'key' LOOP
        UPDATE {{policy}}.byte_bucket SET held = held + amount WHERE scope_key = item->>'key' RETURNING * INTO bucket;
        INSERT INTO {{policy}}.byte_reservation_scope VALUES (reservation, bucket.scope_key, bucket.generation_id);
    END LOOP;
    RETURN jsonb_build_object('reserved', true, 'reservation_id', reservation, 'replayed', false);
END
$function$
