-- LIF-01 audited metadata publication: release maintainers project or suspend public metadata
-- only through this command, with evidence, an expected revision, an idempotent receipt and an
-- audit event. Direct release-role writes to metadata_projection are revoked.
CREATE TABLE {{policy}}.release_receipt (
    command_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES {{policy}}.actor(id),
    collection_id text NOT NULL,
    work_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('project_metadata','suspend_metadata')),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    evidence_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (work_id, revision),
    FOREIGN KEY (evidence_id, collection_id) REFERENCES {{policy}}.evidence(id, collection_id)
);

CREATE FUNCTION {{policy}}.apply_release_command(envelope jsonb)
RETURNS TABLE(revision bigint, replayed boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    aid uuid := {{policy}}.current_actor();
    cid text := envelope->>'collection_id';
    wid text := envelope->>'work_id';
    cmd uuid := (envelope->>'command_id')::uuid;
    action_name text := envelope->>'action';
    expected bigint := (envelope->>'expected_revision')::bigint;
    reason_value text := envelope->>'reason';
    evidence uuid := (envelope->>'evidence_id')::uuid;
    body jsonb := envelope->'payload';
    fingerprint text := encode(sha256(convert_to(envelope::text, 'UTF8')), 'hex');
    previous {{policy}}.release_receipt;
    current_revision bigint;
BEGIN
    IF envelope IS NULL OR octet_length(envelope::text) > 65536 OR cmd IS NULL OR cid IS NULL OR wid IS NULL
        OR expected IS NULL OR expected < 0 OR evidence IS NULL
        OR reason_value IS NULL OR length(btrim(reason_value)) NOT BETWEEN 1 AND 2048
        OR action_name IS NULL OR action_name NOT IN ('project_metadata','suspend_metadata')
        OR (action_name = 'project_metadata' AND (jsonb_typeof(body) IS DISTINCT FROM 'object'
            OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(body) k)
                IS DISTINCT FROM ARRAY['citation','reference_url','title']))
        OR (action_name = 'suspend_metadata' AND body IS NOT NULL AND body <> 'null'::jsonb) THEN
        RAISE EXCEPTION 'Invalid release command' USING ERRCODE = '22023';
    END IF;
    IF aid IS NULL OR NOT {{policy}}.assigned(cid, 'release') THEN
        RAISE EXCEPTION 'Release authority required' USING ERRCODE = '42501';
    END IF;
    PERFORM {{base}}.identity_check_legacy_authority(true);
    PERFORM pg_advisory_xact_lock(hashtextextended('release:' || cmd::text, 0));
    SELECT * INTO previous FROM {{policy}}.release_receipt WHERE command_id = cmd;
    IF previous.command_id IS NOT NULL THEN
        IF previous.actor_id <> aid THEN
            RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE = '42501';
        END IF;
        IF previous.payload_hash <> fingerprint THEN
            RAISE EXCEPTION 'Command retry changed' USING ERRCODE = '40001';
        END IF;
        RETURN QUERY SELECT previous.revision, true;
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('release-work:' || wid, 0));
    SELECT coalesce(max(r.revision), 0) INTO current_revision FROM {{policy}}.release_receipt r WHERE r.work_id = wid;
    IF current_revision <> expected THEN
        RAISE EXCEPTION 'Stale release revision' USING ERRCODE = '40001';
    END IF;
    IF action_name = 'project_metadata' THEN
        INSERT INTO {{corpus}}.metadata_projection(work_id,collection_id,title,citation,reference_url,suspended)
            VALUES (wid, cid, body->>'title', body->>'citation', body->>'reference_url', false)
            ON CONFLICT (work_id) DO UPDATE SET title = excluded.title, citation = excluded.citation,
                reference_url = excluded.reference_url, suspended = false
            WHERE {{corpus}}.metadata_projection.collection_id = excluded.collection_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Work belongs to another collection' USING ERRCODE = '23514';
        END IF;
    ELSE
        UPDATE {{corpus}}.metadata_projection SET suspended = true WHERE work_id = wid AND collection_id = cid;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'No projected metadata to suspend' USING ERRCODE = '22023';
        END IF;
    END IF;
    INSERT INTO {{policy}}.release_receipt(command_id,actor_id,collection_id,work_id,action,payload_hash,evidence_id,revision)
        VALUES (cmd, aid, cid, wid, action_name, fingerprint, evidence, expected + 1);
    INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
        VALUES (aid, cid, evidence, 'metadata_' || action_name, wid, expected + 1, reason_value);
    RETURN QUERY SELECT expected + 1, false;
END
$function$;

REVOKE ALL ON FUNCTION {{policy}}.apply_release_command(jsonb) FROM PUBLIC;
