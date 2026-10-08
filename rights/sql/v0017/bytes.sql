-- BUD absolute-byte ledger: capacity C, held H (unresolved reservations) and spent S (settled debt
-- not yet refilled); available = max(0, C - H - S). Refill only reduces S. Capacities, refill rates,
-- leases and reconciliation bounds are supplied by operator configuration (D3); none is defaulted.
CREATE TABLE {{policy}}.byte_bucket (
    scope_key text PRIMARY KEY CHECK (scope_key ~ '^[a-z]+:[A-Za-z0-9_-]{1,128}$'),
    generation_id uuid NOT NULL UNIQUE,
    capacity bigint NOT NULL CHECK (capacity >= 1),
    refill_per_minute numeric NOT NULL CHECK (refill_per_minute > 0),
    held bigint NOT NULL DEFAULT 0 CHECK (held >= 0),
    spent numeric NOT NULL DEFAULT 0 CHECK (spent >= 0),
    last_refill_at timestamptz NOT NULL
);
CREATE TABLE {{policy}}.byte_reservation (
    reservation_id uuid PRIMARY KEY,
    admission_id uuid NOT NULL UNIQUE,
    reserved_bytes bigint NOT NULL CHECK (reserved_bytes >= 1),
    state text NOT NULL CHECK (state IN ('reserved','emitting','released','settled','ambiguous')),
    state_revision integer NOT NULL DEFAULT 1,
    lease_expires_at timestamptz NOT NULL,
    settled_bytes bigint CHECK (settled_bytes BETWEEN 0 AND reserved_bytes),
    refunded_bytes bigint CHECK (refunded_bytes BETWEEN 0 AND reserved_bytes),
    settlement_mode text CHECK (settlement_mode IN ('receipt','reconciled','conservative','released')),
    receipt_id text CHECK (length(receipt_id) BETWEEN 1 AND 200),
    ambiguous_at timestamptz,
    settled_at timestamptz,
    -- charged = reserved - refunded, and terminal states carry complete settlement.
    CHECK ((state IN ('released','settled')) = (settlement_mode IS NOT NULL AND settled_at IS NOT NULL
        AND settled_bytes IS NOT NULL AND refunded_bytes IS NOT NULL)),
    CHECK (settled_bytes IS NULL OR settled_bytes + refunded_bytes = reserved_bytes),
    CHECK (state <> 'released' OR settled_bytes = 0)
);
CREATE TABLE {{policy}}.byte_reservation_scope (
    reservation_id uuid NOT NULL REFERENCES {{policy}}.byte_reservation(reservation_id),
    scope_key text NOT NULL REFERENCES {{policy}}.byte_bucket(scope_key),
    generation_id uuid NOT NULL,
    PRIMARY KEY (reservation_id, scope_key)
);
CREATE INDEX byte_reservation_open ON {{policy}}.byte_reservation (lease_expires_at)
    WHERE state IN ('reserved','emitting','ambiguous');

-- Reserve N bytes across every scope atomically; idempotent per admission.
CREATE FUNCTION {{policy}}.reserve_bytes(request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
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
$function$;

-- Move held bytes to spent for every scope of one reservation, in stable order.
CREATE FUNCTION {{policy}}.settle_reservation_scopes(reservation uuid, amount bigint, sent bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    scope {{policy}}.byte_reservation_scope;
    changed integer;
BEGIN
    FOR scope IN SELECT * FROM {{policy}}.byte_reservation_scope WHERE reservation_id = reservation ORDER BY scope_key LOOP
        UPDATE {{policy}}.byte_bucket SET held = held - amount, spent = spent + sent
            WHERE scope_key = scope.scope_key AND generation_id = scope.generation_id;
        GET DIAGNOSTICS changed = ROW_COUNT;
        IF changed <> 1 THEN
            RAISE EXCEPTION 'Byte bucket generation changed' USING ERRCODE = '40001';
        END IF;
    END LOOP;
END
$function$;

-- reserved -> emitting, durably, before any byte reaches transport. A stale or expired
-- reservation cannot emit.
CREATE FUNCTION {{policy}}.begin_emission(reservation uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    changed integer;
BEGIN
    UPDATE {{policy}}.byte_reservation SET state = 'emitting', state_revision = state_revision + 1
        WHERE reservation_id = reservation AND state = 'reserved' AND lease_expires_at > statement_timestamp();
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END
$function$;

-- reserved -> released (nothing was emitted). Idempotent; never from emitting or ambiguous.
CREATE FUNCTION {{policy}}.release_bytes(reservation uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    r {{policy}}.byte_reservation;
BEGIN
    SELECT * INTO r FROM {{policy}}.byte_reservation WHERE reservation_id = reservation FOR UPDATE;
    IF r.state = 'released' THEN RETURN; END IF;
    IF r.state IS DISTINCT FROM 'reserved' THEN
        RAISE EXCEPTION 'Only unsent reservations can be released' USING ERRCODE = '40001';
    END IF;
    PERFORM {{policy}}.settle_reservation_scopes(reservation, r.reserved_bytes, 0);
    UPDATE {{policy}}.byte_reservation SET state = 'released', state_revision = state_revision + 1,
        settled_bytes = 0, refunded_bytes = reserved_bytes, settlement_mode = 'released',
        settled_at = statement_timestamp() WHERE reservation_id = reservation;
END
$function$;

-- emitting -> settled with a completed transport receipt for the proven sent bytes.
CREATE FUNCTION {{policy}}.settle_bytes(reservation uuid, sent bigint, receipt text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    r {{policy}}.byte_reservation;
BEGIN
    SELECT * INTO r FROM {{policy}}.byte_reservation WHERE reservation_id = reservation FOR UPDATE;
    IF r.state = 'settled' AND r.settled_bytes = sent AND r.receipt_id = receipt THEN RETURN; END IF;
    IF r.state IS DISTINCT FROM 'emitting' OR sent IS NULL OR sent NOT BETWEEN 0 AND r.reserved_bytes
        OR receipt IS NULL THEN
        RAISE EXCEPTION 'Conflicting or late byte settlement' USING ERRCODE = '40001';
    END IF;
    PERFORM {{policy}}.settle_reservation_scopes(reservation, r.reserved_bytes, sent);
    UPDATE {{policy}}.byte_reservation SET state = 'settled', state_revision = state_revision + 1,
        settled_bytes = sent, refunded_bytes = reserved_bytes - sent, settlement_mode = 'receipt',
        receipt_id = receipt, settled_at = statement_timestamp() WHERE reservation_id = reservation;
END
$function$;

-- Maintenance reconciliation (D3 sets fence and conservative bounds). Expired unsent
-- reservations release; expired, fenced emissions become ambiguous; ambiguous reservations
-- older than the conservative bound settle at every reserved byte. Nothing ambiguous is refunded.
CREATE FUNCTION {{policy}}.reconcile_bytes(fence_seconds numeric, conservative_seconds numeric) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    r {{policy}}.byte_reservation;
    now_at timestamptz := statement_timestamp();
    released integer := 0;
    ambiguous integer := 0;
    conservative integer := 0;
BEGIN
    IF fence_seconds IS NULL OR fence_seconds < 0 OR conservative_seconds IS NULL OR conservative_seconds <= 0 THEN
        RAISE EXCEPTION 'Explicit reconciliation bounds required' USING ERRCODE = '22023';
    END IF;
    FOR r IN SELECT * FROM {{policy}}.byte_reservation WHERE state IN ('reserved','emitting','ambiguous')
        ORDER BY reservation_id FOR UPDATE SKIP LOCKED LOOP
        IF r.state = 'reserved' AND r.lease_expires_at <= now_at THEN
            PERFORM {{policy}}.release_bytes(r.reservation_id);
            released := released + 1;
        ELSIF r.state = 'emitting' AND r.lease_expires_at + make_interval(secs => fence_seconds) <= now_at THEN
            UPDATE {{policy}}.byte_reservation SET state = 'ambiguous', state_revision = state_revision + 1,
                ambiguous_at = now_at WHERE reservation_id = r.reservation_id;
            ambiguous := ambiguous + 1;
        ELSIF r.state = 'ambiguous' AND r.ambiguous_at + make_interval(secs => conservative_seconds) <= now_at THEN
            PERFORM {{policy}}.settle_reservation_scopes(r.reservation_id, r.reserved_bytes, r.reserved_bytes);
            UPDATE {{policy}}.byte_reservation SET state = 'settled', state_revision = state_revision + 1,
                settled_bytes = reserved_bytes, refunded_bytes = 0, settlement_mode = 'conservative',
                settled_at = now_at WHERE reservation_id = r.reservation_id;
            conservative := conservative + 1;
        END IF;
    END LOOP;
    RETURN jsonb_build_object('released', released, 'ambiguous', ambiguous, 'conservative', conservative);
END
$function$;

-- ambiguous -> settled from durable transport evidence measured after fencing.
CREATE FUNCTION {{policy}}.reconcile_reservation(reservation uuid, measured bigint, receipt text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    r {{policy}}.byte_reservation;
BEGIN
    SELECT * INTO r FROM {{policy}}.byte_reservation WHERE reservation_id = reservation FOR UPDATE;
    IF r.state = 'settled' AND r.settled_bytes = measured AND r.receipt_id = receipt THEN RETURN; END IF;
    IF r.state IS DISTINCT FROM 'ambiguous' OR measured IS NULL OR measured NOT BETWEEN 0 AND r.reserved_bytes
        OR receipt IS NULL THEN
        RAISE EXCEPTION 'Only fenced ambiguous reservations accept durable evidence' USING ERRCODE = '40001';
    END IF;
    PERFORM {{policy}}.settle_reservation_scopes(reservation, r.reserved_bytes, measured);
    UPDATE {{policy}}.byte_reservation SET state = 'settled', state_revision = state_revision + 1,
        settled_bytes = measured, refunded_bytes = reserved_bytes - measured, settlement_mode = 'reconciled',
        receipt_id = receipt, settled_at = statement_timestamp() WHERE reservation_id = reservation;
END
$function$;

REVOKE ALL ON FUNCTION {{policy}}.reserve_bytes(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.settle_reservation_scopes(uuid, bigint, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.begin_emission(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.release_bytes(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.settle_bytes(uuid, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.reconcile_bytes(numeric, numeric) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.reconcile_reservation(uuid, bigint, text) FROM PUBLIC;
