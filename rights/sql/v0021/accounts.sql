-- MAC-03–05, MAC-08, MAC-13: people, organisations, access and credentials in their own schema.
-- The serving role reaches it only through check_credential and record_usage, which return no
-- email, identity or name. Times are the database's; invariants that two writers could race on
-- (the last owner, overlapping access periods) lock the organisation row before they look.
CREATE SCHEMA {{accounts}};
REVOKE ALL ON SCHEMA {{accounts}} FROM PUBLIC;

CREATE TABLE {{accounts}}.person (
    id text PRIMARY KEY CHECK (id ~ '^acc_[0-9A-HJKMNP-TV-Z]{26}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    email text NOT NULL CHECK (email = btrim(email) AND char_length(email) BETWEEN 3 AND 320
        AND position('@' IN email) > 1),
    email_verified_at timestamptz,
    display_name text CHECK (display_name = btrim(display_name) AND char_length(display_name) BETWEEN 1 AND 100),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','suspended','closed')),
    second_factor text NOT NULL DEFAULT 'none'
        CHECK (second_factor IN ('none','passkey','totp','passkey_and_totp')),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    closed_at timestamptz,
    CHECK ((state = 'closed') = (closed_at IS NOT NULL))
);
CREATE UNIQUE INDEX person_open_email ON {{accounts}}.person (email) WHERE state <> 'closed';

-- Audit entries name people only through this table; its row goes with the account (N07).
CREATE TABLE {{accounts}}.actor (
    id text PRIMARY KEY CHECK (id ~ '^act_[0-9A-HJKMNP-TV-Z]{26}$'),
    person_id text UNIQUE REFERENCES {{accounts}}.person (id) ON DELETE CASCADE,
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 320)
);

CREATE TABLE {{accounts}}.policy_acceptance (
    person_id text NOT NULL REFERENCES {{accounts}}.person (id) ON DELETE CASCADE,
    document text NOT NULL CHECK (document IN ('terms','acceptable_use','privacy')),
    version text NOT NULL CHECK (version ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    accepted_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (person_id, document, version)
);

-- One account per provider identity, never matched by email (MAC-08, F06).
CREATE TABLE {{accounts}}.identity (
    id text PRIMARY KEY CHECK (id ~ '^idn_[0-9A-HJKMNP-TV-Z]{26}$'),
    person_id text NOT NULL REFERENCES {{accounts}}.person (id) ON DELETE CASCADE,
    method text NOT NULL CHECK (method IN ('google','microsoft','email','passkey')),
    issuer text NOT NULL CHECK (issuer ~ '^https://'),
    subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 255),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 320),
    linked_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    last_used_at timestamptz,
    UNIQUE (issuer, subject)
);
CREATE INDEX identity_person ON {{accounts}}.identity (person_id);

CREATE TABLE {{accounts}}.organisation (
    id text PRIMARY KEY CHECK (id ~ '^org_[0-9A-HJKMNP-TV-Z]{26}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    name text NOT NULL CHECK (name = btrim(name) AND char_length(name) BETWEEN 1 AND 120),
    kind text NOT NULL DEFAULT 'individual'
        CHECK (kind IN ('individual','company','academic','non_profit','government','other')),
    country text CHECK (country ~ '^[A-Z]{2}$'),
    website text CHECK (website ~ '^https://[^[:space:]]{1,2000}$'),
    -- The organisation made at someone's first sign-in; kept when deleted, so never recreated.
    personal_of text UNIQUE REFERENCES {{accounts}}.person (id) ON DELETE SET NULL,
    created_by text REFERENCES {{accounts}}.person (id) ON DELETE SET NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','suspended','ownerless','deleted')),
    member_limit integer NOT NULL DEFAULT 50 CHECK (member_limit BETWEEN 1 AND 100000),
    invitation_limit integer NOT NULL DEFAULT 20 CHECK (invitation_limit BETWEEN 1 AND 100000),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    deleted_at timestamptz,
    CHECK ((state = 'deleted') = (deleted_at IS NOT NULL))
);
CREATE INDEX organisation_creator ON {{accounts}}.organisation (created_by) WHERE state <> 'deleted';

CREATE TABLE {{accounts}}.membership (
    id text PRIMARY KEY CHECK (id ~ '^mem_[0-9A-HJKMNP-TV-Z]{26}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    organisation_id text NOT NULL REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    person_id text NOT NULL REFERENCES {{accounts}}.person (id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('owner','admin','developer','viewer')),
    joined_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (organisation_id, person_id)
);
CREATE INDEX membership_person ON {{accounts}}.membership (person_id);

CREATE TABLE {{accounts}}.invitation (
    id text PRIMARY KEY CHECK (id ~ '^inv_[0-9A-HJKMNP-TV-Z]{26}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    organisation_id text NOT NULL REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    email text NOT NULL CHECK (email = btrim(email) AND char_length(email) BETWEEN 3 AND 320),
    role text NOT NULL CHECK (role IN ('owner','admin','developer','viewer')),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','accepted','revoked','expired','void')),
    invited_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT statement_timestamp() + interval '7 days',
    ended_at timestamptz,
    CHECK ((state = 'pending') = (ended_at IS NULL))
);
CREATE UNIQUE INDEX invitation_pending ON {{accounts}}.invitation (organisation_id, email) WHERE state = 'pending';

CREATE TABLE {{accounts}}.tier (
    id text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{1,31}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
    description text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 500),
    includes text[] NOT NULL CHECK (cardinality(includes) >= 1 AND includes <@ ARRAY['rest','mcp','bulk']),
    allowance jsonb NOT NULL CHECK (jsonb_typeof(allowance) = 'object'),
    published boolean NOT NULL DEFAULT false
);

CREATE TABLE {{accounts}}.entitlement (
    id text PRIMARY KEY CHECK (id ~ '^ent_[0-9A-HJKMNP-TV-Z]{26}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    organisation_id text NOT NULL REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    tier_id text NOT NULL REFERENCES {{accounts}}.tier (id),
    source text NOT NULL CHECK (source IN ('payment','waiver','manual')),
    scope jsonb NOT NULL CHECK (scope = '{"all": true}'::jsonb OR (
        jsonb_typeof(scope->'jurisdictions') = 'array'
        AND jsonb_array_length(scope->'jurisdictions') BETWEEN 1 AND 100
        AND scope - 'jurisdictions' = '{}'::jsonb)),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz CHECK (ends_at > starts_at),
    state text NOT NULL CHECK (state IN ('scheduled','active','lapsed','revoked','ended')),
    reference text CHECK (char_length(reference) <= 500),
    reason text CHECK (char_length(reason) <= 2000),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX entitlement_organisation ON {{accounts}}.entitlement (organisation_id, starts_at DESC);

CREATE TABLE {{accounts}}.entitlement_event (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entitlement_id text NOT NULL REFERENCES {{accounts}}.entitlement (id) ON DELETE CASCADE,
    at timestamptz NOT NULL DEFAULT statement_timestamp(),
    actor_type text NOT NULL CHECK (actor_type IN ('person','operator','system')),
    actor_ref text NOT NULL,
    from_state text,
    to_state text NOT NULL,
    reason text CHECK (char_length(reason) <= 2000)
);

-- A request for access (paid, or free or reduced: MAC-04's waiver request), one open at a time.
CREATE TABLE {{accounts}}.access_request (
    id text PRIMARY KEY CHECK (id ~ '^wrq_[0-9A-HJKMNP-TV-Z]{26}$'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    organisation_id text NOT NULL REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK (char_length(purpose) BETWEEN 1 AND 2000),
    expected_volume text NOT NULL CHECK (char_length(expected_volume) BETWEEN 1 AND 1000),
    scope_requested jsonb NOT NULL CHECK (scope_requested = '{"all": true}'::jsonb OR (
        jsonb_typeof(scope_requested->'jurisdictions') = 'array'
        AND jsonb_array_length(scope_requested->'jurisdictions') BETWEEN 1 AND 100
        AND scope_requested - 'jurisdictions' = '{}'::jsonb)),
    reduced boolean NOT NULL DEFAULT false,
    contact_name text NOT NULL CHECK (char_length(contact_name) BETWEEN 1 AND 100),
    contact_email text NOT NULL CHECK (char_length(contact_email) BETWEEN 3 AND 320),
    state text NOT NULL DEFAULT 'submitted'
        CHECK (state IN ('submitted','needs_information','granted','declined','withdrawn')),
    decision jsonb,
    renewable_after timestamptz,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE UNIQUE INDEX access_request_open ON {{accounts}}.access_request (organisation_id)
    WHERE state IN ('submitted','needs_information');

-- Keys are stored as a keyed hash of the secret; the holder is kept for history, not as a
-- foreign key, so a departed member's revoked keys stay listed.
CREATE TABLE {{accounts}}.api_key (
    key_id text PRIMARY KEY CHECK (key_id ~ '^[0-9a-f]{32}$'),
    kind text NOT NULL CHECK (kind IN ('personal','service')),
    organisation_id text NOT NULL REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    holder_membership text,
    holder_person text,
    created_by text NOT NULL,
    name text NOT NULL CHECK (name = btrim(name) AND char_length(name) BETWEEN 1 AND 80),
    secret_hash bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    last_four text NOT NULL CHECK (last_four ~ '^[A-Za-z0-9_-]{4}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    expires_at timestamptz NOT NULL,
    last_used_at timestamptz,
    revoked_at timestamptz,
    revoked_reason text CHECK (revoked_reason IN
        ('holder','administrator','rotated','member_left','organisation_deleted','account_closed','operator')),
    rotated_from text,
    CHECK ((kind = 'personal') = (holder_membership IS NOT NULL AND holder_person IS NOT NULL)),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '366 days'),
    CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
);
CREATE INDEX api_key_organisation ON {{accounts}}.api_key (organisation_id, created_at DESC);
CREATE INDEX api_key_holder ON {{accounts}}.api_key (holder_membership) WHERE revoked_at IS NULL;

-- A tool's consent for one person, organisation and resource (MAC-05, MAC-07).
CREATE TABLE {{accounts}}.client_grant (
    id text PRIMARY KEY CHECK (id ~ '^grt_[0-9A-HJKMNP-TV-Z]{26}$'),
    organisation_id text NOT NULL REFERENCES {{accounts}}.organisation (id) ON DELETE CASCADE,
    person_id text NOT NULL,
    membership_id text NOT NULL,
    client_id text NOT NULL CHECK (char_length(client_id) BETWEEN 1 AND 2000),
    client_name text NOT NULL CHECK (char_length(client_name) BETWEEN 1 AND 200),
    client_label text NOT NULL CHECK (client_label IN ('verified_publisher','from_host','unverified')),
    client_host text CHECK (char_length(client_host) <= 255),
    resource text NOT NULL CHECK (resource IN ('api','mcp')),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    last_used_at timestamptz,
    revoked_at timestamptz,
    revoked_reason text CHECK (revoked_reason IN
        ('person','administrator','member_left','organisation_deleted','account_closed','operator')),
    CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
);
CREATE INDEX client_grant_organisation ON {{accounts}}.client_grant (organisation_id, created_at DESC);
CREATE INDEX client_grant_membership ON {{accounts}}.client_grant (membership_id) WHERE revoked_at IS NULL;

-- One append-only log of access changes (MAC-13.7). Retention removes only expired entries.
CREATE TABLE {{accounts}}.audit_event (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id text NOT NULL UNIQUE CHECK (id ~ '^evt_[0-9A-HJKMNP-TV-Z]{26}$'),
    at timestamptz NOT NULL DEFAULT statement_timestamp(),
    actor_type text NOT NULL CHECK (actor_type IN ('person','operator','system')),
    actor_ref text NOT NULL CHECK (char_length(actor_ref) BETWEEN 1 AND 100),
    organisation_id text,
    action text NOT NULL CHECK (action ~ '^[a-z_]+\.[a-z_]+$'),
    target_type text,
    target_id text,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(summary) = 'object'),
    network_prefix text CHECK (char_length(network_prefix) <= 64),
    client_family text CHECK (char_length(client_family) <= 64),
    request_id text CHECK (char_length(request_id) <= 64)
);
CREATE INDEX audit_event_organisation ON {{accounts}}.audit_event (organisation_id, seq DESC);

CREATE TABLE {{accounts}}.usage_day (
    organisation_id text NOT NULL,
    day date NOT NULL,
    credential text NOT NULL CHECK (credential ~ '^(key_[0-9a-f]{32}|grt_[0-9A-HJKMNP-TV-Z]{26})$'),
    class text NOT NULL CHECK (class IN ('rest','mcp','bulk')),
    requests bigint NOT NULL DEFAULT 0 CHECK (requests >= 0),
    bytes bigint NOT NULL DEFAULT 0 CHECK (bytes >= 0),
    refusals jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(refusals) = 'object'),
    PRIMARY KEY (organisation_id, day, credential, class)
);

CREATE TABLE {{accounts}}.console_session (
    token_hash bytea PRIMARY KEY CHECK (octet_length(token_hash) = 32),
    person_id text NOT NULL REFERENCES {{accounts}}.person (id) ON DELETE CASCADE,
    csrf text NOT NULL CHECK (char_length(csrf) BETWEEN 32 AND 128),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    expires_at timestamptz NOT NULL,
    authenticated_at timestamptz NOT NULL,
    second_factor_at timestamptz,
    client_family text CHECK (char_length(client_family) <= 64),
    network_prefix text CHECK (char_length(network_prefix) <= 64),
    revoked_at timestamptz
);
CREATE INDEX console_session_person ON {{accounts}}.console_session (person_id);

-- A sign-in started in one browser and finished in the same one, within 10 minutes.
CREATE TABLE {{accounts}}.sign_in_flow (
    state_hash bytea PRIMARY KEY CHECK (octet_length(state_hash) = 32),
    browser_hash bytea NOT NULL CHECK (octet_length(browser_hash) = 32),
    nonce text NOT NULL,
    verifier text NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('sign_in','reauthenticate','second_factor','link')),
    session_hash bytea,
    return_to text NOT NULL CHECK (return_to ~ '^/console(/[A-Za-z0-9/_?=&%.-]*)?$' AND char_length(return_to) <= 500),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT statement_timestamp() + interval '10 minutes'
);

CREATE FUNCTION {{policy}}.accounts_membership_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF OLD.role = 'owner' AND (TG_OP = 'DELETE' OR NEW.role <> 'owner') THEN
        -- Two owners leaving at once queue on the organisation row; the second sees the first.
        PERFORM 1 FROM {{accounts}}.organisation
            WHERE id = OLD.organisation_id AND state IN ('active','suspended') FOR UPDATE;
        IF FOUND AND NOT EXISTS (
            SELECT 1 FROM {{accounts}}.membership m JOIN {{accounts}}.person p ON p.id = m.person_id
            WHERE m.organisation_id = OLD.organisation_id AND m.role = 'owner' AND m.id <> OLD.id
                AND p.state = 'active') THEN
            RAISE EXCEPTION 'last_owner' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE {{accounts}}.api_key SET revoked_at = statement_timestamp(), revoked_reason = 'member_left'
            WHERE holder_membership = OLD.id AND revoked_at IS NULL;
        UPDATE {{accounts}}.client_grant SET revoked_at = statement_timestamp(), revoked_reason = 'member_left'
            WHERE membership_id = OLD.id AND revoked_at IS NULL;
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER membership_guard BEFORE UPDATE OF role OR DELETE ON {{accounts}}.membership
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.accounts_membership_guard();

CREATE FUNCTION {{policy}}.accounts_entitlement_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF NEW.state IN ('scheduled','active') THEN
        PERFORM 1 FROM {{accounts}}.organisation WHERE id = NEW.organisation_id FOR UPDATE;
        IF EXISTS (
            SELECT 1 FROM {{accounts}}.entitlement e
            WHERE e.organisation_id = NEW.organisation_id AND e.id <> NEW.id
                AND e.state IN ('scheduled','active')
                AND tstzrange(e.starts_at, e.ends_at, '[)') && tstzrange(NEW.starts_at, NEW.ends_at, '[)')) THEN
            RAISE EXCEPTION 'entitlement_overlap' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER entitlement_guard BEFORE INSERT OR UPDATE ON {{accounts}}.entitlement
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.accounts_entitlement_guard();

CREATE FUNCTION {{policy}}.accounts_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    -- Only the retention purge, which sets this flag for its own transaction, removes entries,
    -- and only those past their retention.
    IF TG_OP = 'DELETE' AND current_setting('open_alvary.retention_purge', true) = 'on'
        AND OLD.at < statement_timestamp() - interval '2 years' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'append_only' USING ERRCODE = '42501';
END
$function$;
CREATE TRIGGER audit_event_append_only BEFORE UPDATE OR DELETE ON {{accounts}}.audit_event
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.accounts_append_only();
CREATE TRIGGER entitlement_event_append_only BEFORE UPDATE OR DELETE ON {{accounts}}.entitlement_event
    FOR EACH ROW EXECUTE FUNCTION {{policy}}.accounts_append_only();

-- The organisation's access now, computed from the stored state and the period in database time
-- (MAC-04, N13): a late job never delays a refusal.
CREATE FUNCTION {{policy}}.accounts_access(organisation text) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    now_at timestamptz := statement_timestamp();
    current_row {{accounts}}.entitlement;
    latest {{accounts}}.entitlement;
    tier_row {{accounts}}.tier;
BEGIN
    SELECT * INTO current_row FROM {{accounts}}.entitlement
        WHERE organisation_id = organisation AND state IN ('scheduled','active')
            AND starts_at <= now_at AND (ends_at IS NULL OR ends_at > now_at)
        ORDER BY starts_at DESC LIMIT 1;
    IF FOUND THEN
        SELECT * INTO tier_row FROM {{accounts}}.tier WHERE id = current_row.tier_id;
        RETURN jsonb_build_object('state', 'active', 'entitlement', current_row.id,
            'tier', jsonb_build_object('id', tier_row.id, 'includes', to_jsonb(tier_row.includes),
                'allowance', tier_row.allowance),
            'scope', current_row.scope, 'ends_at', current_row.ends_at);
    END IF;
    SELECT * INTO latest FROM {{accounts}}.entitlement
        WHERE organisation_id = organisation AND starts_at <= now_at
        ORDER BY starts_at DESC LIMIT 1;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('state', 'none');
    END IF;
    RETURN jsonb_build_object('state',
        CASE WHEN latest.state IN ('scheduled','active') THEN 'lapsed' ELSE latest.state END,
        'entitlement', latest.id, 'ends_at', latest.ends_at);
END
$function$;

-- The runtime's only view of accounts: is this credential usable now, for which organisation,
-- with what access. Returns no email, identity or name. Last use is recorded at most once a
-- minute per credential.
CREATE FUNCTION {{policy}}.check_credential(request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    now_at timestamptz := statement_timestamp();
    key_row {{accounts}}.api_key;
    grant_row {{accounts}}.client_grant;
    organisation_row {{accounts}}.organisation;
    person_state text;
    member_role text;
    credential text;
    access jsonb;
BEGIN
    IF request->>'kind' = 'key' THEN
        IF coalesce(request->>'key_id', '') !~ '^[0-9a-f]{32}$'
            OR coalesce(request->>'secret_hash', '') !~ '^[0-9a-f]{64}$' THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        SELECT * INTO key_row FROM {{accounts}}.api_key WHERE key_id = request->>'key_id';
        IF NOT FOUND OR key_row.secret_hash <> decode(request->>'secret_hash', 'hex')
            OR key_row.revoked_at IS NOT NULL OR key_row.expires_at <= now_at THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        credential := 'key_' || key_row.key_id;
        SELECT * INTO organisation_row FROM {{accounts}}.organisation WHERE id = key_row.organisation_id;
        IF key_row.kind = 'personal' THEN
            SELECT p.state, m.role INTO person_state, member_role
                FROM {{accounts}}.membership m JOIN {{accounts}}.person p ON p.id = m.person_id
                WHERE m.id = key_row.holder_membership;
            IF NOT FOUND OR member_role = 'viewer' THEN
                RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
            END IF;
        END IF;
        UPDATE {{accounts}}.api_key SET last_used_at = now_at
            WHERE key_id = key_row.key_id
                AND (last_used_at IS NULL OR last_used_at < now_at - interval '1 minute');
    ELSIF request->>'kind' = 'grant' THEN
        SELECT * INTO grant_row FROM {{accounts}}.client_grant WHERE id = request->>'grant_id';
        IF NOT FOUND OR grant_row.revoked_at IS NOT NULL OR grant_row.resource <> request->>'resource'
            OR NOT EXISTS (SELECT 1 FROM {{accounts}}.identity i
                WHERE i.person_id = grant_row.person_id AND i.issuer = request->>'issuer'
                    AND i.subject = request->>'subject') THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        credential := grant_row.id;
        SELECT * INTO organisation_row FROM {{accounts}}.organisation WHERE id = grant_row.organisation_id;
        SELECT p.state, m.role INTO person_state, member_role
            FROM {{accounts}}.membership m JOIN {{accounts}}.person p ON p.id = m.person_id
            WHERE m.id = grant_row.membership_id;
        IF NOT FOUND OR member_role = 'viewer' THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        UPDATE {{accounts}}.client_grant SET last_used_at = now_at
            WHERE id = grant_row.id
                AND (last_used_at IS NULL OR last_used_at < now_at - interval '1 minute');
    ELSE
        RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
    END IF;
    IF person_state IS NOT NULL AND person_state <> 'active' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'account_inactive');
    END IF;
    IF organisation_row.state IN ('suspended','deleted') THEN
        RETURN jsonb_build_object('ok', false, 'code', 'organisation_suspended');
    END IF;
    access := {{policy}}.accounts_access(organisation_row.id);
    IF access->>'state' <> 'active' THEN
        RETURN jsonb_build_object('ok', false, 'code',
            CASE WHEN access->>'state' IN ('none','scheduled') THEN 'entitlement_required'
                ELSE 'entitlement_lapsed' END);
    END IF;
    RETURN jsonb_build_object('ok', true, 'organisation', organisation_row.id, 'credential', credential,
        'tier', access->'tier', 'scope', access->'scope', 'ends_at', access->'ends_at');
END
$function$;

-- Daily totals per credential and class; no query text (MAC-14).
CREATE FUNCTION {{policy}}.record_usage(entry jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    refusal text := entry->>'refusal';
BEGIN
    IF refusal !~ '^[a-z_]{1,40}$' THEN
        RAISE EXCEPTION 'Invalid usage entry' USING ERRCODE = '22023';
    END IF;
    INSERT INTO {{accounts}}.usage_day AS u (organisation_id, day, credential, class, requests, bytes, refusals)
    VALUES (entry->>'organisation', (statement_timestamp() AT TIME ZONE 'UTC')::date, entry->>'credential',
        entry->>'class', CASE WHEN refusal IS NULL THEN 1 ELSE 0 END,
        greatest(0, coalesce((entry->>'bytes')::bigint, 0)),
        CASE WHEN refusal IS NULL THEN '{}'::jsonb ELSE jsonb_build_object(refusal, 1) END)
    ON CONFLICT (organisation_id, day, credential, class) DO UPDATE SET
        requests = u.requests + EXCLUDED.requests,
        bytes = u.bytes + EXCLUDED.bytes,
        refusals = CASE WHEN refusal IS NULL THEN u.refusals
            ELSE jsonb_set(u.refusals, ARRAY[refusal], to_jsonb(coalesce((u.refusals->>refusal)::bigint, 0) + 1)) END;
END
$function$;

REVOKE ALL ON FUNCTION {{policy}}.accounts_membership_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.accounts_entitlement_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.accounts_append_only() FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.accounts_access(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.check_credential(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION {{policy}}.record_usage(jsonb) FROM PUBLIC;
