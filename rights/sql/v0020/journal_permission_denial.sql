CREATE OR REPLACE FUNCTION {{policy}}.journal_permission_denial()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    previous jsonb;
    current jsonb := to_jsonb(NEW);
    scope text;
    operations text[] := ARRAY[{{vocabulary_discover}},{{vocabulary_acquire}},{{vocabulary_retain}},
        {{vocabulary_external_process}},{{vocabulary_redistribute_metadata}},
        {{vocabulary_redistribute_text}},{{vocabulary_quote}},{{vocabulary_derive}},
        {{vocabulary_commercial_reuse}},{{vocabulary_publish_fixture}}];
    wider boolean;
BEGIN
    -- Conservative: any revision that is not provably at least as permissive as the one it
    -- replaces is journaled as a denial. Extra tombstones are harmless; a missed one is not.
    IF TG_TABLE_NAME = 'collection_decision' THEN
        scope := NEW.collection_id;
        SELECT to_jsonb(d) INTO previous FROM {{policy}}.collection_decision d
            WHERE d.collection_id = NEW.collection_id AND d.revision = NEW.revision - 1;
        IF previous IS NULL OR previous->>'state' <> 'approved' THEN
            RETURN NULL;  -- nothing was permitted before this revision
        END IF;
    ELSE
        scope := current->>'version_id';
        SELECT to_jsonb(o) INTO previous FROM {{policy}}.version_override o
            WHERE o.version_id = current->>'version_id' AND o.revision = NEW.revision - 1;
        IF previous IS NULL THEN
            -- An override can only narrow its collection: a first override that is not an approved
            -- allow-everything record withdraws something.
            IF current->>'state' = 'approved' AND NOT EXISTS (
                SELECT 1 FROM unnest(operations) operation WHERE current->>operation <> 'allow') THEN
                RETURN NULL;
            END IF;
            PERFORM {{policy}}.record_denial('publication_deny', ARRAY[scope], false);
            RETURN NULL;
        END IF;
        IF previous->>'state' <> 'approved' THEN
            RETURN NULL;
        END IF;
    END IF;
    wider := current->>'state' = 'approved'
        AND (current->>'valid_from')::timestamptz <= (previous->>'valid_from')::timestamptz
        AND (current->>'expires_at')::timestamptz >= (previous->>'expires_at')::timestamptz
        AND NOT EXISTS (SELECT 1 FROM unnest(operations) operation
            WHERE previous->>operation = 'allow' AND current->>operation <> 'allow');
    IF TG_TABLE_NAME = 'collection_decision' THEN
        wider := wider AND current->>'basis_type' <> 'unknown'
            AND (current->>'conditions_satisfied')::boolean
            AND current->>'metadata_privacy_class' IS NOT DISTINCT FROM previous->>'metadata_privacy_class'
            AND current->>'privacy_review_id' IS NOT DISTINCT FROM previous->>'privacy_review_id'
            AND (current->>'privacy_review_revision')::bigint IS NOT DISTINCT FROM
                (previous->>'privacy_review_revision')::bigint;
    END IF;
    IF NOT wider THEN
        PERFORM {{policy}}.record_denial('publication_deny', ARRAY[scope], false);
    END IF;
    RETURN NULL;
END
$function$
