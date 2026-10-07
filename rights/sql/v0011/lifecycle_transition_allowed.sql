CREATE OR REPLACE FUNCTION {{policy}}.lifecycle_transition_allowed(artifact uuid, previous text, following text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{policy}}.staging_lifecycle_event e WHERE e.artifact_id=artifact
                AND e.from_state=previous AND e.to_state=following AND e.actor_id={{policy}}.current_actor()
                AND e.transaction_id=pg_current_xact_id())
        $function$
