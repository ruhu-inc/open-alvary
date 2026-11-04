CREATE OR REPLACE FUNCTION {{policy}}.begin_emission(reservation uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    changed integer;
BEGIN
    UPDATE {{policy}}.byte_reservation SET state = 'emitting', state_revision = state_revision + 1
        WHERE reservation_id = reservation AND state = 'reserved' AND lease_expires_at > statement_timestamp();
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END
$function$
