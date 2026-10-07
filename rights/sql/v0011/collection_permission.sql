CREATE OR REPLACE FUNCTION {{policy}}.collection_permission(cid text, operation text, vid text DEFAULT NULL::text)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE d {{policy}}.collection_decision; o {{policy}}.version_override; value text;
        BEGIN
            IF operation IS NULL OR operation NOT IN ({{vocabulary_discover}},{{vocabulary_acquire}},{{vocabulary_retain}},{{vocabulary_external_process}},{{vocabulary_redistribute_metadata}},{{vocabulary_redistribute_text}},{{vocabulary_quote}},{{vocabulary_derive}},{{vocabulary_commercial_reuse}},{{vocabulary_publish_fixture}}) THEN RETURN false; END IF;
            SELECT r.* INTO d FROM {{policy}}.collection_decision r JOIN {{policy}}.revision_head h
                ON h.kind='collection' AND h.scope_id=r.collection_id AND h.revision=r.revision
                JOIN {{policy}}.actor a ON a.id=r.actor_id AND a.active WHERE r.collection_id=cid;
            IF d.collection_id IS NULL OR d.state<>'approved' OR d.basis_type='unknown'
                OR NOT d.conditions_satisfied OR d.valid_from>statement_timestamp()
                OR d.expires_at<=statement_timestamp() THEN RETURN false; END IF;
            EXECUTE format('SELECT ($1).%I::text',operation) INTO value USING d;
            IF value IS DISTINCT FROM 'allow' THEN RETURN false; END IF;
            IF operation={{vocabulary_redistribute_metadata}} AND (d.metadata_privacy_class<>'no_personal_data' OR d.privacy_review_id IS NOT NULL)
                AND NOT {{policy}}.privacy_current(d.privacy_review_id,d.privacy_review_revision,cid) THEN RETURN false; END IF;
            IF vid IS NOT NULL THEN
                IF NOT EXISTS(SELECT 1 FROM {{policy}}.version_binding WHERE version_id=vid AND collection_id=cid) THEN RETURN false; END IF;
                SELECT r.* INTO o FROM {{policy}}.version_override r JOIN {{policy}}.revision_head h
                    ON h.kind='override' AND h.scope_id=r.version_id AND h.revision=r.revision
                    JOIN {{policy}}.actor a ON a.id=r.actor_id AND a.active
                    WHERE r.version_id=vid AND r.collection_id=cid;
                IF o.version_id IS NOT NULL THEN
                    EXECUTE format('SELECT ($1).%I::text',operation) INTO value USING o;
                    IF o.state<>'approved' OR value IS DISTINCT FROM 'allow'
                        OR o.valid_from>statement_timestamp() OR o.expires_at<=statement_timestamp() THEN RETURN false; END IF;
                END IF;
            END IF;
            RETURN true;
        END $function$
