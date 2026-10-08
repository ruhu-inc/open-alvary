-- SYS-07 serving epochs and restore recovery. An epoch binds to one database instance, so an
-- "open" epoch copied by a dump never authorizes serving in a restored or cloned database.
CREATE TABLE {{policy}}.instance_binding (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    system_identifier numeric NOT NULL,
    database_oid oid NOT NULL,
    bound_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
-- Opening is committed before the journal marker, so the journal never names an epoch the
-- database cannot explain. Epochs copied by a restore are abandoned unless cleanly closed.
CREATE TABLE {{policy}}.serving_epoch (
    epoch_id uuid PRIMARY KEY,
    system_identifier numeric NOT NULL,
    database_oid oid NOT NULL,
    state text NOT NULL CHECK (state IN ('opening','open','closed','abandoned')),
    opened_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    open_sequence bigint CHECK (open_sequence > 0),
    closed_at timestamptz,
    close_sequence bigint CHECK (close_sequence > open_sequence),
    close_revision bigint CHECK (close_revision >= 0),
    CHECK ((state IN ('open','closed')) = (open_sequence IS NOT NULL) OR state = 'abandoned'),
    CHECK ((state = 'closed') = (close_sequence IS NOT NULL AND close_revision IS NOT NULL)),
    CHECK ((state IN ('closed','abandoned')) = (closed_at IS NOT NULL))
);
CREATE UNIQUE INDEX serving_epoch_one_active ON {{policy}}.serving_epoch ((true))
    WHERE state IN ('opening','open');
-- Each recovery run on a database instance, with its predicate inputs and decision.
CREATE TABLE {{policy}}.recovery_record (
    id uuid PRIMARY KEY,
    system_identifier numeric NOT NULL,
    database_oid oid NOT NULL,
    recovered_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    decision text NOT NULL CHECK (decision IN ('continuity_verified','conservative_reset')),
    journal_sequence bigint NOT NULL CHECK (journal_sequence >= 0),
    journal_chain text NOT NULL CHECK (journal_chain ~ '^[a-f0-9]{64}$'),
    predicate jsonb NOT NULL,
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2048)
);
-- A collection stays denied for public serving until a decision revision above its floor exists.
CREATE TABLE {{policy}}.recovery_floor (
    collection_id text NOT NULL,
    recovery_id uuid NOT NULL REFERENCES {{policy}}.recovery_record(id),
    revision bigint NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (collection_id, recovery_id)
);

CREATE FUNCTION {{policy}}.serving_permitted() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM {{policy}}.serving_epoch e JOIN {{policy}}.instance_binding b
            ON b.system_identifier = e.system_identifier AND b.database_oid = e.database_oid
        WHERE e.state = 'open'
            AND e.system_identifier = (SELECT system_identifier FROM pg_catalog.pg_control_system())
            AND e.database_oid = (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()))
$function$;

CREATE FUNCTION {{policy}}.serving_allowed(cid text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
    SELECT {{policy}}.serving_permitted() AND NOT EXISTS (
        SELECT 1 FROM {{policy}}.recovery_floor f
        WHERE f.collection_id = cid AND coalesce((SELECT h.revision FROM {{policy}}.revision_head h
            WHERE h.kind = 'collection' AND h.scope_id = cid), 0) <= f.revision)
$function$;

REVOKE ALL ON FUNCTION {{policy}}.serving_permitted() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.serving_allowed(text) FROM PUBLIC;
