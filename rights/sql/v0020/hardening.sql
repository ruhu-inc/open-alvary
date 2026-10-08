-- Two-phase epoch close: 'closing' stops serving before the clean-close marker is journaled.
DO $do$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_catalog.pg_constraint
        WHERE conrelid = '{{policy}}.serving_epoch'::regclass AND contype = 'c' LOOP
        EXECUTE format('ALTER TABLE {{policy}}.serving_epoch DROP CONSTRAINT %I', c.conname);
    END LOOP;
END
$do$;
ALTER TABLE {{policy}}.serving_epoch ADD COLUMN open_revision bigint CHECK (open_revision >= 0);
ALTER TABLE {{policy}}.serving_epoch
    ADD CONSTRAINT serving_epoch_state CHECK (state IN ('opening','open','closing','closed','abandoned')),
    ADD CONSTRAINT serving_epoch_sequence_order CHECK (close_sequence > open_sequence),
    ADD CONSTRAINT serving_epoch_close_revision CHECK (close_revision >= 0),
    ADD CONSTRAINT serving_epoch_active CHECK (state NOT IN ('opening','open')
        OR (closed_at IS NULL AND close_sequence IS NULL AND close_revision IS NULL)),
    ADD CONSTRAINT serving_epoch_open CHECK (state NOT IN ('open','closing','closed') OR open_sequence IS NOT NULL),
    ADD CONSTRAINT serving_epoch_closing CHECK (state <> 'closing'
        OR (close_revision IS NOT NULL AND closed_at IS NOT NULL AND close_sequence IS NULL)),
    ADD CONSTRAINT serving_epoch_closed CHECK (state <> 'closed'
        OR (close_revision IS NOT NULL AND closed_at IS NOT NULL AND close_sequence IS NOT NULL)),
    ADD CONSTRAINT serving_epoch_abandoned CHECK (state <> 'abandoned' OR closed_at IS NOT NULL);
DROP INDEX {{policy}}.serving_epoch_one_active;
CREATE UNIQUE INDEX serving_epoch_one_active ON {{policy}}.serving_epoch ((true))
    WHERE state IN ('opening','open','closing');

-- Privacy and controller authority that narrows is a metadata denial for its collection.
CREATE FUNCTION {{policy}}.journal_privacy_denial() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    previous jsonb;
    current jsonb := to_jsonb(NEW);
BEGIN
    IF TG_TABLE_NAME = 'privacy_review' THEN
        SELECT to_jsonb(v) INTO previous FROM {{policy}}.privacy_review v
            WHERE v.id = NEW.id AND v.revision = NEW.revision - 1;
    ELSE
        SELECT to_jsonb(c) INTO previous FROM {{policy}}.controller_record c
            WHERE c.id = NEW.id AND c.revision = NEW.revision - 1;
    END IF;
    IF previous IS NULL OR previous->>'state' <> 'approved' THEN
        RETURN NULL;
    END IF;
    IF current->>'state' <> 'approved'
        OR (current->>'expires_at')::timestamptz < (previous->>'expires_at')::timestamptz
        OR (current->>'valid_from')::timestamptz > (previous->>'valid_from')::timestamptz
        OR (TG_TABLE_NAME = 'privacy_review' AND (current->>'clearance' IS DISTINCT FROM previous->>'clearance'
            OR current->>'cleared_hash' IS DISTINCT FROM previous->>'cleared_hash')) THEN
        PERFORM {{policy}}.record_denial('publication_deny', ARRAY[NEW.collection_id], false);
    END IF;
    RETURN NULL;
END
$function$;

-- Deactivating an actor voids every decision they authored: journal each affected collection.
CREATE FUNCTION {{policy}}.journal_actor_denial() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    scopes text[];
BEGIN
    IF OLD.active AND NOT NEW.active THEN
        scopes := ARRAY(SELECT DISTINCT collection_id FROM (
            SELECT collection_id FROM {{policy}}.collection_decision WHERE actor_id = NEW.id
            UNION SELECT collection_id FROM {{policy}}.version_override WHERE actor_id = NEW.id
            UNION SELECT collection_id FROM {{policy}}.privacy_review WHERE actor_id = NEW.id
            UNION SELECT collection_id FROM {{policy}}.controller_record WHERE actor_id = NEW.id) s
            ORDER BY 1 LIMIT 1000);
        IF cardinality(scopes) > 0 THEN
            PERFORM {{policy}}.record_denial('publication_deny', scopes, false);
        END IF;
    END IF;
    RETURN NULL;
END
$function$;

REVOKE ALL ON FUNCTION {{policy}}.journal_privacy_denial() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.journal_actor_denial() FROM PUBLIC;
CREATE TRIGGER journal_privacy_denial AFTER INSERT ON {{policy}}.privacy_review
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.journal_privacy_denial();
CREATE TRIGGER journal_privacy_denial AFTER INSERT ON {{policy}}.controller_record
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.journal_privacy_denial();
CREATE TRIGGER journal_actor_denial AFTER UPDATE OF active ON {{policy}}.actor
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.journal_actor_denial();
ALTER TABLE {{policy}}.privacy_review ENABLE ALWAYS TRIGGER journal_privacy_denial;
ALTER TABLE {{policy}}.controller_record ENABLE ALWAYS TRIGGER journal_privacy_denial;
ALTER TABLE {{policy}}.actor ENABLE ALWAYS TRIGGER journal_actor_denial;
