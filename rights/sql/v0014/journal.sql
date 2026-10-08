-- SYS-07 step 1: every denial commits with a pending delivery record in the same transaction.
-- LIF-02: one locked monotonic row allocates publication revisions in commit order.
CREATE TABLE {{policy}}.publication_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT INTO {{policy}}.publication_revision DEFAULT VALUES;
CREATE TABLE {{policy}}.denial_delivery (
    event_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('publication_deny','erasure')),
    affected text[] NOT NULL CHECK (cardinality(affected) BETWEEN 1 AND 1000),
    revision bigint NOT NULL UNIQUE CHECK (revision > 0),
    revision_domain text NOT NULL CHECK (revision_domain IN ('publication','account')),
    occurred_at timestamptz NOT NULL,
    erasure boolean NOT NULL,
    durability text NOT NULL DEFAULT 'pending' CHECK (durability IN ('pending','durable')),
    journal_sequence bigint UNIQUE CHECK (journal_sequence > 0),
    journal_chain text CHECK (journal_chain ~ '^[a-f0-9]{64}$'),
    durable_at timestamptz,
    CHECK ((durability = 'durable') = (journal_sequence IS NOT NULL AND journal_chain IS NOT NULL
        AND durable_at IS NOT NULL))
);
CREATE INDEX denial_delivery_pending ON {{policy}}.denial_delivery (revision) WHERE durability = 'pending';

CREATE FUNCTION {{policy}}.record_denial(event_kind text, ids text[], erase boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    allocated bigint;
    id uuid := gen_random_uuid();
BEGIN
    -- The row lock is held until commit, so allocation order is commit order.
    UPDATE {{policy}}.publication_revision SET revision = revision + 1 WHERE singleton
        RETURNING revision INTO allocated;
    INSERT INTO {{policy}}.denial_delivery(event_id,kind,affected,revision,revision_domain,occurred_at,erasure)
        VALUES (id, event_kind, ids, allocated, 'publication', statement_timestamp(), erase);
    RETURN id;
END
$function$;

CREATE FUNCTION {{policy}}.journal_permission_denial() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
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
$function$;

CREATE FUNCTION {{policy}}.journal_metadata_suspension() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF NOT OLD.suspended AND (TG_OP = 'DELETE' OR NEW.suspended) THEN
        PERFORM {{policy}}.record_denial('publication_deny', ARRAY[OLD.work_id], false);
    END IF;
    RETURN NULL;
END
$function$;

CREATE FUNCTION {{policy}}.journal_staging_erasure() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF OLD.private_bytes IS NOT NULL AND NEW.private_bytes IS NULL THEN
        PERFORM {{policy}}.record_denial('erasure', ARRAY[OLD.id::text], true);
    END IF;
    RETURN NULL;
END
$function$;

CREATE FUNCTION {{policy}}.denial_delivery_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    -- Only the pending -> durable receipt transition is allowed; payloads never change.
    IF TG_OP = 'UPDATE' AND OLD.durability = 'pending' AND NEW.durability = 'durable'
        AND (NEW.event_id,NEW.kind,NEW.affected,NEW.revision,NEW.revision_domain,NEW.occurred_at,NEW.erasure)
            IS NOT DISTINCT FROM
            (OLD.event_id,OLD.kind,OLD.affected,OLD.revision,OLD.revision_domain,OLD.occurred_at,OLD.erasure)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Denial delivery records are append-only' USING ERRCODE = '42501';
END
$function$;

REVOKE ALL ON FUNCTION {{policy}}.record_denial(text, text[], boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.journal_permission_denial() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.journal_metadata_suspension() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.journal_staging_erasure() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.denial_delivery_immutable() FROM PUBLIC;

CREATE TRIGGER journal_permission_denial AFTER INSERT ON {{policy}}.collection_decision
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.journal_permission_denial();
CREATE TRIGGER journal_permission_denial AFTER INSERT ON {{policy}}.version_override
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.journal_permission_denial();
CREATE TRIGGER journal_metadata_suspension AFTER UPDATE OF suspended OR DELETE ON {{corpus}}.metadata_projection
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.journal_metadata_suspension();
CREATE TRIGGER journal_staging_erasure AFTER UPDATE OF private_bytes ON {{staging}}.staged_artifact
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.journal_staging_erasure();
CREATE TRIGGER denial_delivery_immutable BEFORE UPDATE OR DELETE ON {{policy}}.denial_delivery
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.denial_delivery_immutable();
CREATE TRIGGER denial_delivery_no_truncate BEFORE TRUNCATE ON {{policy}}.denial_delivery
    FOR EACH STATEMENT EXECUTE FUNCTION {{policy}}.denial_delivery_immutable();
ALTER TABLE {{policy}}.collection_decision ENABLE ALWAYS TRIGGER journal_permission_denial;
ALTER TABLE {{policy}}.version_override ENABLE ALWAYS TRIGGER journal_permission_denial;
ALTER TABLE {{corpus}}.metadata_projection ENABLE ALWAYS TRIGGER journal_metadata_suspension;
ALTER TABLE {{staging}}.staged_artifact ENABLE ALWAYS TRIGGER journal_staging_erasure;
ALTER TABLE {{policy}}.denial_delivery ENABLE ALWAYS TRIGGER denial_delivery_immutable;
ALTER TABLE {{policy}}.denial_delivery ENABLE ALWAYS TRIGGER denial_delivery_no_truncate;
