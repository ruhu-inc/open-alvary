CREATE OR REPLACE FUNCTION {{policy}}.canonical_current(pid uuid, requested_purpose text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{staging}}.canonical_proposal c JOIN {{staging}}.staging_snapshot t ON t.id=c.snapshot_id AND t.revision=c.snapshot_revision
                JOIN {{staging}}.snapshot_head h ON h.id=t.id AND h.revision=t.revision AND h.run_id=t.run_id
                WHERE c.id=pid AND c.state='active' AND t.state='active' AND t.snapshot_hash=c.snapshot_hash
                    AND {{policy}}.assembly_input_current(c.artifact_id,requested_purpose)
                    AND NOT EXISTS(SELECT 1 FROM {{staging}}.snapshot_selection sel JOIN {{staging}}.adapter_candidate d ON d.id=sel.candidate_id
                        WHERE sel.snapshot_id=t.id AND sel.revision=t.revision AND d.state<>'active'))
        $function$
