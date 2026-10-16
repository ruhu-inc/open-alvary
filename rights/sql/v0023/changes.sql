-- SUB C1 (changes-contracts.md): the public change log. Events are appended in the transaction
-- that changes the public catalogue, ordered by the LIF-02 publication revision, never edited.
CREATE SCHEMA {{changes}};

CREATE TABLE {{changes}}.feed_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    epoch uuid NOT NULL DEFAULT gen_random_uuid(),
    epoch_started_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    epoch_reason text NOT NULL DEFAULT 'initial' CHECK (char_length(epoch_reason) BETWEEN 1 AND 500),
    baselined boolean NOT NULL DEFAULT false,
    baseline_at timestamptz,
    purged_through_revision bigint NOT NULL DEFAULT 0 CHECK (purged_through_revision >= 0)
);
INSERT INTO {{changes}}.feed_state DEFAULT VALUES;

CREATE TABLE {{changes}}.change_event (
    event_id uuid NOT NULL,
    event_revision integer NOT NULL CHECK (event_revision >= 1),
    event_type text NOT NULL CHECK (event_type IN ('catalogue_added', 'source_publication_observed',
        'text_version_published', 'metadata_corrected', 'legal_effect_recorded', 'proposal_recorded',
        'treatment_recorded', 'withdrawal', 'coverage_changed')),
    logical_event_key text NOT NULL CHECK (char_length(logical_event_key) BETWEEN 1 AND 300),
    reviewed_revision integer NOT NULL CHECK (reviewed_revision >= 1),
    publication_revision bigint NOT NULL CHECK (publication_revision > 0),
    feed_epoch uuid NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    observed_at timestamptz,
    publisher_published_at text CHECK (publisher_published_at ~ '^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$'),
    legal_effective jsonb CHECK (legal_effective IS NULL OR jsonb_typeof(legal_effective) = 'object'),
    jurisdiction_id text NOT NULL CHECK (char_length(jurisdiction_id) BETWEEN 1 AND 160),
    work_id text NOT NULL CHECK (char_length(work_id) BETWEEN 1 AND 160),
    version_id text CHECK (char_length(version_id) BETWEEN 1 AND 160),
    document_class text CHECK (char_length(document_class) <= 64),
    language text CHECK (char_length(language) <= 64),
    change_reason text CHECK (change_reason IN ('first_text', 'source_revision', 'extraction_correction',
        'redaction', 'unknown', 'restored', 'corrected')),
    supersedes_event_id uuid,
    supersedes_revision integer CHECK (supersedes_revision >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (event_id, event_revision),
    UNIQUE (event_type, logical_event_key, reviewed_revision),
    CHECK ((supersedes_event_id IS NULL) = (supersedes_revision IS NULL))
);
CREATE INDEX change_event_position ON {{changes}}.change_event (publication_revision, event_id, event_revision);
CREATE INDEX change_event_work ON {{changes}}.change_event (work_id, publication_revision);

-- The last public projection of each work that events were computed from.
CREATE TABLE {{changes}}.published_state (
    work_id text PRIMARY KEY CHECK (char_length(work_id) BETWEEN 1 AND 160),
    jurisdiction_id text NOT NULL,
    visible boolean NOT NULL,
    projection jsonb NOT NULL CHECK (jsonb_typeof(projection) = 'object'),
    projection_hash text NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{64}$'),
    readable_versions text[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TABLE {{changes}}.coverage_state (
    jurisdiction_id text PRIMARY KEY CHECK (char_length(jurisdiction_id) BETWEEN 1 AND 160),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    snapshot_hash text NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE FUNCTION {{policy}}.changes_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    -- Only the retention purge, which sets this flag for its own transaction, removes events,
    -- and only those past retention (400 days, D19 proposal).
    IF TG_OP = 'DELETE' AND current_setting('open_alvary.retention_purge', true) = 'on'
        AND OLD.committed_at < statement_timestamp() - interval '400 days' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'append_only' USING ERRCODE = '42501';
END
$function$;
CREATE TRIGGER change_event_append_only BEFORE UPDATE OR DELETE ON {{changes}}.change_event
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.changes_append_only();

-- LIF-02: one locked row allocates publication revisions in commit order; the lock is held until
-- the publishing transaction commits. Never a sequence.
CREATE FUNCTION {{policy}}.allocate_publication_revision() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
    UPDATE {{policy}}.publication_revision SET revision = revision + 1 WHERE singleton RETURNING revision
$function$;

REVOKE ALL ON FUNCTION {{policy}}.changes_append_only() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.allocate_publication_revision() FROM PUBLIC;
