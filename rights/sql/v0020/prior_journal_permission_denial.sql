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
BEGIN
    IF TG_TABLE_NAME = 'collection_decision' THEN
        scope := NEW.collection_id;
        SELECT to_jsonb(d) INTO previous FROM {{policy}}.collection_decision d
            WHERE d.collection_id = NEW.collection_id AND d.revision = NEW.revision - 1;
    ELSE
        scope := current->>'version_id';
        SELECT to_jsonb(o) INTO previous FROM {{policy}}.version_override o
            WHERE o.version_id = current->>'version_id' AND o.revision = NEW.revision - 1;
    END IF;
    -- Any operation that was allowed by an approved revision and is no longer allowed is a denial.
    IF previous IS NOT NULL AND previous->>'state' = 'approved' AND (
        current->>'state' <> 'approved' OR EXISTS (
            SELECT 1 FROM unnest(ARRAY[{{vocabulary_discover}},{{vocabulary_acquire}},{{vocabulary_retain}},
                {{vocabulary_external_process}},{{vocabulary_redistribute_metadata}},
                {{vocabulary_redistribute_text}},{{vocabulary_quote}},{{vocabulary_derive}},
                {{vocabulary_commercial_reuse}},{{vocabulary_publish_fixture}}]) operation
            WHERE previous->>operation = 'allow' AND current->>operation <> 'allow'))
    THEN
        PERFORM {{policy}}.record_denial('publication_deny', ARRAY[scope], false);
    END IF;
    RETURN NULL;
END
$function$
