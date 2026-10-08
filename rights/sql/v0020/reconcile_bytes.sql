CREATE OR REPLACE FUNCTION {{policy}}.reconcile_bytes(fence_seconds numeric, conservative_seconds numeric)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    r {{policy}}.byte_reservation;
    now_at timestamptz := statement_timestamp();
    released integer := 0;
    ambiguous integer := 0;
    conservative integer := 0;
    batch uuid[];
BEGIN
    IF fence_seconds IS NULL OR fence_seconds < 0 OR conservative_seconds IS NULL OR conservative_seconds <= 0 THEN
        RAISE EXCEPTION 'Explicit reconciliation bounds required' USING ERRCODE = '22023';
    END IF;
    -- Bounded batch of due reservations; their buckets are locked in global key order before any
    -- settlement, the same order reserve_bytes uses, so reconciliation cannot deadlock with it.
    batch := ARRAY(SELECT b.reservation_id FROM {{policy}}.byte_reservation b
        WHERE (b.state = 'reserved' AND b.lease_expires_at <= now_at)
            OR (b.state = 'emitting' AND b.lease_expires_at + make_interval(secs => fence_seconds) <= now_at)
            OR (b.state = 'ambiguous' AND b.ambiguous_at + make_interval(secs => conservative_seconds) <= now_at)
        ORDER BY b.reservation_id LIMIT 500);
    PERFORM 1 FROM {{policy}}.byte_bucket k WHERE k.scope_key IN (
        SELECT s.scope_key FROM {{policy}}.byte_reservation_scope s WHERE s.reservation_id = ANY(batch))
        ORDER BY k.scope_key FOR UPDATE;
    FOR r IN SELECT b.* FROM {{policy}}.byte_reservation b WHERE b.reservation_id = ANY(batch)
        ORDER BY b.reservation_id FOR UPDATE SKIP LOCKED LOOP
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
$function$
