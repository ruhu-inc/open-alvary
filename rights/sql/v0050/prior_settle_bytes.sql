CREATE OR REPLACE FUNCTION {{policy}}.settle_bytes(reservation uuid, sent bigint, receipt text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
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
$function$
