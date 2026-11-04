CREATE OR REPLACE FUNCTION {{policy}}.release_bytes(reservation uuid)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
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
$function$
