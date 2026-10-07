"""Private policy/staging foundations; opt-in role provisioning, no live cutover.

SQLite and the legacy pilot remain unchanged. Test namespaces own isolated child schemas.
Functions initially have no public execution privileges. Provision a non-superuser guard
before using them with runtime roles; no approved records are seeded by this migration.
"""

from hashlib import sha256

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

PERMISSIONS = (
    "discover",
    "acquire",
    "retain",
    "external_process",
    "redistribute_metadata",
    "redistribute_text",
    "quote",
    "derive",
    "commercial_reuse",
    "publish_fixture",
)


def _names():
    base = op.get_bind().exec_driver_sql("SELECT current_schema()").scalar_one()
    names = {
        kind: f"{base}_{kind}" if base.startswith("test_") else kind
        for kind in ("policy", "staging", "corpus")
    }
    quote = op.get_bind().dialect.identifier_preparer.quote_identifier
    return base, quote(base), {kind: quote(value) for kind, value in names.items()}


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    base, b, names = _names()
    p, s, c = (names[key] for key in ("policy", "staging", "corpus"))
    guard = "oa_" + sha256(base.encode()).hexdigest()[:12] + "_guard"
    for schema in names.values():
        op.execute(f"CREATE SCHEMA {schema}; REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
    vector = ",".join(f"{name} {p}.permission NOT NULL DEFAULT 'unknown'" for name in PERMISSIONS)
    op.execute(f"""
        CREATE TYPE {p}.permission AS ENUM ('allow','deny','unknown');
        CREATE TABLE {p}.actor (
            id uuid PRIMARY KEY,
            database_role oid NOT NULL UNIQUE,
            identity_kind text NOT NULL CHECK (identity_kind = 'synthetic'),
            active boolean NOT NULL DEFAULT false
        );
        CREATE TABLE {p}.assignment (
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            collection_id text NOT NULL REFERENCES {b}.identity_collections(id),
            capability text NOT NULL CHECK (capability IN ('rights','content','acquire','release')),
            PRIMARY KEY (actor_id,collection_id,capability)
        );
        CREATE TABLE {p}.evidence (
            id uuid PRIMARY KEY,
            collection_id text NOT NULL REFERENCES {b}.identity_collections(id),
            private_reference text NOT NULL CHECK (length(btrim(private_reference)) BETWEEN 1 AND 2048),
            evidence_hash text NOT NULL CHECK (evidence_hash ~ '^[a-f0-9]{{64}}$'),
            observed_at timestamptz NOT NULL,
            UNIQUE (id,collection_id)
        );
        CREATE TABLE {p}.revision_head (
            kind text NOT NULL CHECK (kind IN ('controller','privacy','acquisition','collection','verification','override')),
            scope_id text NOT NULL,
            revision bigint NOT NULL CHECK (revision >= 0),
            PRIMARY KEY (kind,scope_id)
        );
        CREATE TABLE {p}.audit_event (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            collection_id text NOT NULL REFERENCES {b}.identity_collections(id),
            evidence_id uuid NOT NULL,
            kind text NOT NULL,
            scope_id text NOT NULL,
            revision bigint NOT NULL,
            reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2048),
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            UNIQUE (kind,scope_id,revision),
            FOREIGN KEY (evidence_id,collection_id) REFERENCES {p}.evidence(id,collection_id)
        );
    """)
    # Every reviewed record pins an exact actor, evidence, validity window and revision.
    common = f"""
        revision bigint NOT NULL CHECK (revision > 0),
        collection_id text NOT NULL REFERENCES {b}.identity_collections(id),
        actor_id uuid NOT NULL REFERENCES {p}.actor(id),
        evidence_id uuid NOT NULL,
        valid_from timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','approved','revoked')),
        reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2048),
        CHECK (expires_at > valid_from),
        FOREIGN KEY (evidence_id,collection_id) REFERENCES {p}.evidence(id,collection_id)
    """
    op.execute(f"""
        CREATE TABLE {p}.controller_record (
            id uuid NOT NULL, {common},
            legal_name text NOT NULL CHECK (length(btrim(legal_name)) > 0),
            postal_address text NOT NULL CHECK (length(btrim(postal_address)) > 0),
            jurisdiction_id varchar(160) NOT NULL REFERENCES {b}.jurisdictions(id),
            privacy_contact text NOT NULL CHECK (length(btrim(privacy_contact)) > 0),
            accountable_role text NOT NULL CHECK (length(btrim(accountable_role)) > 0),
            PRIMARY KEY (id,revision), UNIQUE (id,revision,collection_id)
        );
        CREATE TABLE {p}.privacy_review (
            id uuid NOT NULL, {common},
            controller_id uuid NOT NULL, controller_revision bigint NOT NULL,
            purpose text NOT NULL CHECK (length(btrim(purpose)) > 0),
            lawful_basis text NOT NULL CHECK (length(btrim(lawful_basis)) > 0),
            assessment_reference text NOT NULL CHECK (length(btrim(assessment_reference)) > 0),
            clearance text NOT NULL CHECK (clearance IN ('pending','cleared','redacted','rejected')),
            cleared_hash text CHECK (cleared_hash ~ '^[a-f0-9]{{64}}$'),
            PRIMARY KEY (id,revision), UNIQUE (id,revision,collection_id),
            FOREIGN KEY (controller_id,controller_revision,collection_id)
                REFERENCES {p}.controller_record(id,revision,collection_id),
            CHECK (clearance <> 'redacted' OR cleared_hash IS NOT NULL)
        );
        CREATE TABLE {p}.acquisition_assessment (
            id uuid NOT NULL, {common},
            discover {p}.permission NOT NULL DEFAULT 'unknown',
            acquire {p}.permission NOT NULL DEFAULT 'unknown',
            retain {p}.permission NOT NULL DEFAULT 'unknown',
            derive {p}.permission NOT NULL DEFAULT 'unknown',
            privacy_class text NOT NULL CHECK (privacy_class IN ('no_personal_data','personal_data','unknown')),
            classification_reason text NOT NULL CHECK (length(btrim(classification_reason)) > 0),
            privacy_review_id uuid, privacy_review_revision bigint,
            purpose text NOT NULL CHECK (length(btrim(purpose)) > 0),
            retention_deadline timestamptz NOT NULL,
            PRIMARY KEY (id,revision), UNIQUE (id,revision,collection_id),
            FOREIGN KEY (privacy_review_id,privacy_review_revision,collection_id)
                REFERENCES {p}.privacy_review(id,revision,collection_id),
            CHECK ((privacy_review_id IS NULL) = (privacy_review_revision IS NULL)),
            CHECK (privacy_class = 'no_personal_data' OR privacy_review_id IS NOT NULL),
            CHECK (retention_deadline > valid_from AND retention_deadline <= expires_at)
        );
        CREATE TABLE {p}.collection_decision (
            {common}, {vector},
            basis_type text NOT NULL CHECK (basis_type IN ('open_licence','direct_permission',
                'statutory_exclusion','public_domain_expiry','government_work_rule',
                'original_authorship','statutory_exception','unknown')),
            conditions_satisfied boolean NOT NULL DEFAULT false,
            metadata_privacy_class text NOT NULL CHECK (metadata_privacy_class IN
                ('no_personal_data','personal_data','unknown')),
            privacy_reason text NOT NULL CHECK (length(btrim(privacy_reason)) > 0),
            privacy_review_id uuid, privacy_review_revision bigint,
            PRIMARY KEY (collection_id,revision),
            FOREIGN KEY (privacy_review_id,privacy_review_revision,collection_id)
                REFERENCES {p}.privacy_review(id,revision,collection_id),
            CHECK ((privacy_review_id IS NULL) = (privacy_review_revision IS NULL)),
            CHECK (metadata_privacy_class = 'no_personal_data' OR privacy_review_id IS NOT NULL)
        );
        CREATE TABLE {p}.verification_policy (
            {common},
            sample_numerator integer NOT NULL CHECK (sample_numerator > 0),
            sample_denominator integer NOT NULL CHECK (sample_denominator >= sample_numerator),
            escalation_rule text NOT NULL CHECK (length(btrim(escalation_rule)) > 0),
            material_classes text[] NOT NULL CHECK (material_classes @> ARRAY[
                'operative_tables','amounts','dates','negations','cross_references','identity_citation']::text[]),
            PRIMARY KEY (collection_id,revision)
        );
        CREATE TABLE {p}.verification_material (
            collection_id text NOT NULL,
            revision bigint NOT NULL,
            material_class text NOT NULL CHECK (material_class IN
                ('operative_tables','amounts','dates','negations','cross_references','identity_citation')),
            method text NOT NULL CHECK (method IN ('human_source_comparison','validated_native_sampling')),
            sample_numerator integer NOT NULL CHECK (sample_numerator > 0),
            sample_denominator integer NOT NULL CHECK (sample_denominator >= sample_numerator),
            competence_evidence_id uuid NOT NULL,
            escalation_rule text NOT NULL CHECK (length(btrim(escalation_rule)) > 0),
            exclusion_rule text NOT NULL CHECK (length(btrim(exclusion_rule)) > 0),
            PRIMARY KEY (collection_id,revision,material_class),
            FOREIGN KEY (collection_id,revision) REFERENCES {p}.verification_policy(collection_id,revision),
            FOREIGN KEY (competence_evidence_id,collection_id) REFERENCES {p}.evidence(id,collection_id),
            CHECK (material_class<>'operative_tables' OR method='human_source_comparison')
        );
        ALTER TABLE {b}.identity_works ADD CONSTRAINT uq_work_collection UNIQUE (id,collection_id);
        ALTER TABLE {b}.identity_legacy_sources ADD CONSTRAINT uq_legacy_source_work UNIQUE (source_id,work_id);
        ALTER TABLE {b}.identity_legacy_versions ADD CONSTRAINT uq_legacy_version_owner_hash
            UNIQUE (version_id,source_id,content_hash);
        CREATE TABLE {p}.version_binding (
            version_id varchar(160) PRIMARY KEY,
            source_id varchar(160) NOT NULL,
            work_id text NOT NULL,
            collection_id text NOT NULL,
            content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{{64}}$'),
            FOREIGN KEY (version_id,source_id,content_hash)
                REFERENCES {b}.identity_legacy_versions(version_id,source_id,content_hash),
            FOREIGN KEY (source_id,work_id) REFERENCES {b}.identity_legacy_sources(source_id,work_id),
            FOREIGN KEY (work_id,collection_id) REFERENCES {b}.identity_works(id,collection_id),
            UNIQUE (version_id,collection_id)
        );
        CREATE TABLE {p}.version_override (
            version_id varchar(160) NOT NULL, {common}, {vector},
            PRIMARY KEY (version_id,revision),
            FOREIGN KEY (version_id,collection_id) REFERENCES {p}.version_binding(version_id,collection_id)
        );
        CREATE TABLE {s}.staged_artifact (
            id uuid PRIMARY KEY,
            collection_id text NOT NULL REFERENCES {b}.identity_collections(id),
            assessment_id uuid NOT NULL, assessment_revision bigint NOT NULL,
            artifact_hash text NOT NULL CHECK (artifact_hash ~ '^[a-f0-9]{{64}}$'),
            size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 10485760),
            private_bytes bytea,
            acquired_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            retention_deadline timestamptz NOT NULL,
            state text NOT NULL DEFAULT 'staged' CHECK (state IN
                ('staged','processing','validated','held_pending_reassessment','erasure_required','erased')),
            FOREIGN KEY (assessment_id,assessment_revision,collection_id)
                REFERENCES {p}.acquisition_assessment(id,revision,collection_id),
            CHECK (retention_deadline > acquired_at),
            CHECK ((state='erased' AND private_bytes IS NULL) OR
                (state<>'erased' AND private_bytes IS NOT NULL
                 AND octet_length(private_bytes)=size_bytes
                 AND encode(sha256(private_bytes),'hex')=artifact_hash))
        );
        CREATE TABLE {c}.metadata_projection (
            work_id text PRIMARY KEY,
            collection_id text NOT NULL,
            title text NOT NULL CHECK (length(btrim(title)) > 0),
            citation text NOT NULL CHECK (length(btrim(citation)) > 0),
            reference_url text NOT NULL CHECK (reference_url ~ '^https://[^/@]+(/|$)'),
            suspended boolean NOT NULL DEFAULT true,
            FOREIGN KEY (work_id,collection_id) REFERENCES {b}.identity_works(id,collection_id)
        );
    """)
    op.execute(f"""
        CREATE FUNCTION {p}.current_actor() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog AS $$
            SELECT a.id FROM {p}.actor a JOIN pg_catalog.pg_roles r ON r.oid=a.database_role
            WHERE a.active AND NOT r.rolsuper AND NOT r.rolbypassrls
                AND r.rolname=session_user::text
        $$;
        CREATE FUNCTION {p}.assigned(cid text, cap text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {p}.assignment WHERE actor_id={p}.current_actor()
                AND collection_id=cid AND capability=cap)
        $$;
        CREATE FUNCTION {p}.immutable_record() RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog AS $$ BEGIN
            RAISE EXCEPTION 'Policy history is append-only' USING ERRCODE='55000';
        END $$;
        CREATE FUNCTION {p}.record_revision() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$
        DECLARE k text := TG_ARGV[0]; scope text; previous bigint; cap text := TG_ARGV[1];
        BEGIN
            IF NEW.actor_id IS DISTINCT FROM {p}.current_actor() OR NOT {p}.assigned(NEW.collection_id,cap) THEN
                RAISE EXCEPTION 'Actor or collection authority mismatch' USING ERRCODE='42501';
            END IF;
            IF k IN ('collection','verification') THEN scope := NEW.collection_id;
            ELSIF k='override' THEN scope := NEW.version_id;
            ELSE scope := NEW.id::text; END IF;
            INSERT INTO {p}.revision_head VALUES(k,scope,0) ON CONFLICT DO NOTHING;
            SELECT revision INTO previous FROM {p}.revision_head
                WHERE kind=k AND scope_id=scope FOR UPDATE;
            IF NEW.revision <> previous+1 THEN
                RAISE EXCEPTION 'Stale policy revision' USING ERRCODE='40001';
            END IF;
            IF k NOT IN ('collection','verification','override') AND previous>0 THEN
                EXECUTE format('SELECT collection_id FROM %I.%I WHERE id=$1 AND revision=$2',
                    TG_TABLE_SCHEMA,TG_TABLE_NAME) INTO scope USING NEW.id,previous;
                IF scope IS DISTINCT FROM NEW.collection_id THEN
                    RAISE EXCEPTION 'Revision cannot change collection' USING ERRCODE='23514';
                END IF;
                scope := NEW.id::text;
            END IF;
            IF NEW.state='approved' AND NEW.valid_from>statement_timestamp() THEN
                RAISE EXCEPTION 'Future review cannot approve' USING ERRCODE='23514';
            END IF;
            UPDATE {p}.revision_head SET revision=NEW.revision WHERE kind=k AND scope_id=scope;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(NEW.actor_id,NEW.collection_id,NEW.evidence_id,k,scope,NEW.revision,NEW.reason);
            RETURN NEW;
        END $$;
        CREATE FUNCTION {p}.privacy_current(pid uuid, rev bigint, cid text, hash text DEFAULT NULL)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(
                SELECT 1 FROM {p}.privacy_review v
                JOIN {p}.revision_head vh ON vh.kind='privacy' AND vh.scope_id=v.id::text AND vh.revision=v.revision
                JOIN {p}.controller_record ctl ON ctl.id=v.controller_id AND ctl.revision=v.controller_revision
                JOIN {p}.revision_head ch ON ch.kind='controller' AND ch.scope_id=ctl.id::text AND ch.revision=ctl.revision
                JOIN {p}.actor va ON va.id=v.actor_id AND va.active
                JOIN {p}.actor ca ON ca.id=ctl.actor_id AND ca.active
                WHERE v.id=pid AND v.revision=rev AND v.collection_id=cid AND ctl.collection_id=cid
                    AND v.state='approved' AND ctl.state='approved'
                    AND v.valid_from<=statement_timestamp() AND v.expires_at>statement_timestamp()
                    AND ctl.valid_from<=statement_timestamp() AND ctl.expires_at>statement_timestamp()
                    AND (v.clearance='cleared' OR (v.clearance='redacted' AND v.cleared_hash=hash)))
        $$;
        CREATE FUNCTION {p}.collection_permission(cid text, operation text, vid text DEFAULT NULL)
        RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE d {p}.collection_decision; o {p}.version_override; value text;
        BEGIN
            IF operation IS NULL OR operation NOT IN ({",".join(repr(x) for x in PERMISSIONS)}) THEN RETURN false; END IF;
            SELECT r.* INTO d FROM {p}.collection_decision r JOIN {p}.revision_head h
                ON h.kind='collection' AND h.scope_id=r.collection_id AND h.revision=r.revision
                JOIN {p}.actor a ON a.id=r.actor_id AND a.active WHERE r.collection_id=cid;
            IF d.collection_id IS NULL OR d.state<>'approved' OR d.basis_type='unknown'
                OR NOT d.conditions_satisfied OR d.valid_from>statement_timestamp()
                OR d.expires_at<=statement_timestamp() THEN RETURN false; END IF;
            EXECUTE format('SELECT ($1).%I::text',operation) INTO value USING d;
            IF value IS DISTINCT FROM 'allow' THEN RETURN false; END IF;
            IF operation='redistribute_metadata' AND (d.metadata_privacy_class<>'no_personal_data' OR d.privacy_review_id IS NOT NULL)
                AND NOT {p}.privacy_current(d.privacy_review_id,d.privacy_review_revision,cid) THEN RETURN false; END IF;
            IF vid IS NOT NULL THEN
                IF NOT EXISTS(SELECT 1 FROM {p}.version_binding WHERE version_id=vid AND collection_id=cid) THEN RETURN false; END IF;
                SELECT r.* INTO o FROM {p}.version_override r JOIN {p}.revision_head h
                    ON h.kind='override' AND h.scope_id=r.version_id AND h.revision=r.revision
                    JOIN {p}.actor a ON a.id=r.actor_id AND a.active
                    WHERE r.version_id=vid AND r.collection_id=cid;
                IF o.version_id IS NOT NULL THEN
                    EXECUTE format('SELECT ($1).%I::text',operation) INTO value USING o;
                    IF o.state<>'approved' OR value IS DISTINCT FROM 'allow'
                        OR o.valid_from>statement_timestamp() OR o.expires_at<=statement_timestamp() THEN RETURN false; END IF;
                END IF;
            END IF;
            RETURN true;
        END $$;
        CREATE FUNCTION {p}.metadata_allowed(cid text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog AS $$ SELECT {p}.collection_permission(cid,'redistribute_metadata') $$;
        CREATE FUNCTION {p}.acquisition_current(aid uuid, rev bigint, cid text, purpose_value text, hash text DEFAULT NULL)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {p}.acquisition_assessment a JOIN {p}.revision_head h
                ON h.kind='acquisition' AND h.scope_id=a.id::text AND h.revision=a.revision
                JOIN {p}.actor actor ON actor.id=a.actor_id AND actor.active
                JOIN {b}.identity_collections col ON col.id=a.collection_id
                WHERE a.id=aid AND a.revision=rev AND a.collection_id=cid AND a.purpose=purpose_value
                    AND a.state='approved' AND a.acquire='allow' AND a.retain='allow'
                    AND a.valid_from<=statement_timestamp() AND a.expires_at>statement_timestamp()
                    AND a.retention_deadline>statement_timestamp()
                    AND ((a.privacy_class='no_personal_data' AND a.privacy_review_id IS NULL AND col.document_class NOT IN ('judgment','other'))
                        OR {p}.privacy_current(a.privacy_review_id,a.privacy_review_revision,cid,hash)))
        $$;
        CREATE FUNCTION {p}.staging_allowed(aid uuid, rev bigint, cid text, hash text)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT {p}.assigned(cid,'acquire') AND EXISTS(
                SELECT 1 FROM {p}.acquisition_assessment a WHERE a.id=aid AND a.revision=rev
                    AND {p}.acquisition_current(aid,rev,cid,a.purpose,hash))
        $$;
        CREATE FUNCTION {p}.validate_staging() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$
        DECLARE deadline timestamptz;
        BEGIN
            IF NOT {p}.assigned(NEW.collection_id,'acquire') THEN
                RAISE EXCEPTION 'Acquisition assignment required' USING ERRCODE='42501';
            END IF;
            IF TG_OP='UPDATE' THEN
                IF (NEW.id,NEW.collection_id,NEW.assessment_id,NEW.assessment_revision,NEW.artifact_hash,
                    NEW.size_bytes,NEW.acquired_at,NEW.retention_deadline)
                    IS DISTINCT FROM (OLD.id,OLD.collection_id,OLD.assessment_id,OLD.assessment_revision,
                    OLD.artifact_hash,OLD.size_bytes,OLD.acquired_at,OLD.retention_deadline)
                    OR (NEW.private_bytes IS DISTINCT FROM OLD.private_bytes AND NEW.state<>'erased') THEN
                    RAISE EXCEPTION 'Staging lineage is immutable' USING ERRCODE='55000';
                END IF;
                IF NEW.state='erased' THEN RETURN NEW; END IF;
            END IF;
            SELECT retention_deadline INTO deadline FROM {p}.acquisition_assessment
                WHERE id=NEW.assessment_id AND revision=NEW.assessment_revision AND collection_id=NEW.collection_id;
            IF NOT {p}.staging_allowed(NEW.assessment_id,NEW.assessment_revision,NEW.collection_id,NEW.artifact_hash)
                OR deadline IS NULL OR NEW.retention_deadline>deadline OR NEW.acquired_at>statement_timestamp()
                OR NEW.state NOT IN ('staged','processing','validated') THEN
                RAISE EXCEPTION 'Current acquisition authority required' USING ERRCODE='42501';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER validate_staging BEFORE INSERT OR UPDATE ON {s}.staged_artifact
            FOR EACH ROW EXECUTE FUNCTION {p}.validate_staging();
        CREATE FUNCTION {p}.lock_collection(cid text, expected bigint) RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE actual bigint;
        BEGIN
            IF NOT ({p}.assigned(cid,'rights') OR {p}.assigned(cid,'release')) THEN
                RAISE EXCEPTION 'Collection authority required' USING ERRCODE='42501';
            END IF;
            SELECT revision INTO actual FROM {p}.revision_head
                WHERE kind='collection' AND scope_id=cid FOR UPDATE;
            IF actual IS DISTINCT FROM expected THEN
                RAISE EXCEPTION 'Stale policy revision' USING ERRCODE='40001';
            END IF;
            RETURN actual;
        END $$;
    """)
    op.execute(f"""
        CREATE FUNCTION {p}.lock_approval_inputs(cid text, rights_expected bigint, content_expected bigint)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE d {p}.collection_decision; v {p}.verification_policy;
        BEGIN
            IF NOT {p}.assigned(cid,'release') THEN
                RAISE EXCEPTION 'Release assignment required' USING ERRCODE='42501';
            END IF;
            PERFORM 1 FROM {p}.revision_head WHERE scope_id=cid AND kind IN ('collection','verification')
                ORDER BY kind FOR UPDATE;
            SELECT r.* INTO d FROM {p}.collection_decision r JOIN {p}.revision_head h
                ON h.kind='collection' AND h.scope_id=r.collection_id AND h.revision=r.revision WHERE r.collection_id=cid;
            SELECT r.* INTO v FROM {p}.verification_policy r JOIN {p}.revision_head h
                ON h.kind='verification' AND h.scope_id=r.collection_id AND h.revision=r.revision WHERE r.collection_id=cid;
            IF d.revision IS DISTINCT FROM rights_expected OR v.revision IS DISTINCT FROM content_expected THEN
                RAISE EXCEPTION 'Stale approval inputs' USING ERRCODE='40001';
            END IF;
            IF d.state<>'approved' OR v.state<>'approved' OR d.actor_id=v.actor_id
                OR d.valid_from>statement_timestamp() OR d.expires_at<=statement_timestamp()
                OR v.valid_from>statement_timestamp() OR v.expires_at<=statement_timestamp()
                OR NOT EXISTS(SELECT 1 FROM {p}.actor WHERE id=d.actor_id AND active)
                OR NOT EXISTS(SELECT 1 FROM {p}.actor WHERE id=v.actor_id AND active) THEN
                RAISE EXCEPTION 'Independent current reviews required' USING ERRCODE='42501';
            END IF;
            IF (SELECT count(*) FROM {p}.verification_material WHERE collection_id=cid AND revision=v.revision)<>6 THEN
                RAISE EXCEPTION 'Material coverage incomplete' USING ERRCODE='23514';
            END IF;
            RETURN true;
        END $$;
    """)
    op.execute(f"""
        CREATE FUNCTION {p}.erase_staged(artifact_id uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE artifact {s}.staged_artifact; evidence uuid;
        BEGIN
            SELECT * INTO artifact FROM {s}.staged_artifact WHERE id=artifact_id FOR UPDATE;
            IF artifact.id IS NULL OR NOT {p}.assigned(artifact.collection_id,'acquire') THEN
                RAISE EXCEPTION 'Acquisition assignment required' USING ERRCODE='42501';
            END IF;
            IF artifact.state='erased' THEN RETURN true; END IF;
            SELECT evidence_id INTO evidence FROM {p}.acquisition_assessment
                WHERE id=artifact.assessment_id AND revision=artifact.assessment_revision;
            UPDATE {s}.staged_artifact SET state='erased',private_bytes=NULL WHERE id=artifact_id;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES({p}.current_actor(),artifact.collection_id,evidence,'staging_erasure',artifact_id::text,1,'Scoped staging erasure');
            RETURN true;
        END $$;
    """)
    op.execute(f"""
        CREATE FUNCTION {p}.record_material() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$
        DECLARE v {p}.verification_policy;
        BEGIN
            SELECT * INTO v FROM {p}.verification_policy
                WHERE collection_id=NEW.collection_id AND revision=NEW.revision;
            IF v.actor_id IS DISTINCT FROM {p}.current_actor() OR NOT {p}.assigned(NEW.collection_id,'content') THEN
                RAISE EXCEPTION 'Content reviewer authority required' USING ERRCODE='42501';
            END IF;
            PERFORM 1 FROM {p}.revision_head WHERE kind='verification' AND scope_id=NEW.collection_id
                AND revision=NEW.revision FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Stale verification policy' USING ERRCODE='40001'; END IF;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(v.actor_id,NEW.collection_id,NEW.competence_evidence_id,'verification_material',
                    NEW.collection_id||':'||NEW.material_class,NEW.revision,'Material coverage recorded');
            RETURN NEW;
        END $$;
        CREATE TRIGGER record_material BEFORE INSERT ON {p}.verification_material
            FOR EACH ROW EXECUTE FUNCTION {p}.record_material();
        CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {p}.verification_material
            FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        CREATE FUNCTION {p}.review_permission(cid text, operation text, vid text DEFAULT NULL)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT ({p}.assigned(cid,'rights') OR {p}.assigned(cid,'content'))
                AND {p}.collection_permission(cid,operation,vid)
        $$;
    """)
    reviewed = {
        "controller_record": ("controller", "rights"),
        "privacy_review": ("privacy", "rights"),
        "acquisition_assessment": ("acquisition", "rights"),
        "collection_decision": ("collection", "rights"),
        "verification_policy": ("verification", "content"),
        "version_override": ("override", "rights"),
    }
    for table, (kind, cap) in reviewed.items():
        op.execute(f"""
            CREATE TRIGGER record_revision BEFORE INSERT ON {p}.{table}
                FOR EACH ROW EXECUTE FUNCTION {p}.record_revision('{kind}','{cap}');
            CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {p}.{table}
                FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        """)
    for table in ("evidence", "audit_event", "version_binding"):
        op.execute(f"""CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {p}.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record()""")
    for table in (*reviewed, "verification_material", "evidence", "version_binding", "audit_event"):
        op.execute(f"""
            ALTER TABLE {p}.{table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {p}.{table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY collection_scope ON {p}.{table} USING (
                current_user='{guard}' OR {p}.assigned(collection_id,'rights')
                OR {p}.assigned(collection_id,'content')) WITH CHECK (
                current_user='{guard}' OR {p}.assigned(collection_id,'rights')
                OR {p}.assigned(collection_id,'content'));
        """)
    op.execute(f"""
        ALTER TABLE {s}.staged_artifact ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {s}.staged_artifact FORCE ROW LEVEL SECURITY;
        CREATE POLICY staged_read ON {s}.staged_artifact FOR SELECT USING (
            current_user='{guard}' OR (retention_deadline>statement_timestamp() AND state IN ('staged','processing','validated')
                AND {p}.staging_allowed(assessment_id,assessment_revision,collection_id,artifact_hash)));
        CREATE POLICY staged_insert ON {s}.staged_artifact FOR INSERT WITH CHECK ({p}.assigned(collection_id,'acquire'));
        CREATE POLICY staged_update ON {s}.staged_artifact FOR UPDATE USING ({p}.assigned(collection_id,'acquire'))
            WITH CHECK ({p}.assigned(collection_id,'acquire'));
        ALTER TABLE {c}.metadata_projection ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {c}.metadata_projection FORCE ROW LEVEL SECURITY;
        CREATE POLICY metadata_scope ON {c}.metadata_projection USING (current_user='{guard}'
            OR {p}.assigned(collection_id,'release')) WITH CHECK ({p}.assigned(collection_id,'release'));
        CREATE POLICY metadata_eligible ON {c}.metadata_projection FOR SELECT
            USING (NOT suspended AND {p}.metadata_allowed(collection_id));
        CREATE VIEW {c}.eligible_metadata WITH (security_barrier=true) AS
            SELECT work_id,title,citation,reference_url FROM {c}.metadata_projection
            WHERE NOT suspended AND {p}.metadata_allowed(collection_id);
        REVOKE ALL ON ALL TABLES IN SCHEMA {p},{s},{c} FROM PUBLIC;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {p} FROM PUBLIC;
    """)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _, b, names = _names()
    for schema in names.values():
        # Do not erase policy history through a routine downgrade.
        relations = (
            op.get_bind()
            .exec_driver_sql(
                """
            SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname) FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','p')
        """,
                (schema[1:-1],),
            )
            .scalars()
            .all()
        )
        for relation in relations:
            if op.get_bind().exec_driver_sql(f"SELECT EXISTS(SELECT 1 FROM {relation})").scalar_one():
                raise RuntimeError("Nonempty policy foundation requires forward repair")
    for schema in names.values():
        op.execute(f"DROP SCHEMA {schema} CASCADE")
    op.execute(f"ALTER TABLE {b}.identity_legacy_versions DROP CONSTRAINT uq_legacy_version_owner_hash")
    op.execute(f"ALTER TABLE {b}.identity_legacy_sources DROP CONSTRAINT uq_legacy_source_work")
    op.execute(f"ALTER TABLE {b}.identity_works DROP CONSTRAINT uq_work_collection")
