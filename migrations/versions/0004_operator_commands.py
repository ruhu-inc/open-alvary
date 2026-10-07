"""Native-login synthetic operator bindings and atomic private policy commands.

No real appointments, authentication provider, public route or acquisition is enabled.
Existing NOLOGIN synthetic subjects remain foundation fixtures, never CLI identities.
"""

from hashlib import sha256

from alembic import op

revision = "0004"
down_revision = "0003"
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
MATERIALS = (
    "operative_tables",
    "amounts",
    "dates",
    "negations",
    "cross_references",
    "identity_citation",
)


def names():
    conn = op.get_bind()
    base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
    quote = conn.dialect.identifier_preparer.quote_identifier
    policy = f"{base}_policy" if base.startswith("test_") else "policy"
    role_prefix = "oa_" + sha256(base.encode()).hexdigest()[:12]
    return quote(policy), role_prefix, quote


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    p, prefix, quote = names()
    conn = op.get_bind()
    existing_guard = conn.exec_driver_sql(
        "SELECT oid FROM pg_roles WHERE rolname=%s", (prefix + "_guard",)
    ).scalar_one_or_none()
    if existing_guard is not None:
        for kind in ("guard", "serving", "review", "acquisition", "release"):
            role = (
                conn.exec_driver_sql(
                    """
                SELECT oid,rolcanlogin,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication
                FROM pg_roles WHERE rolname=%s
            """,
                    (prefix + "_" + kind,),
                )
                .mappings()
                .one_or_none()
            )
            if role is None or any(
                role[key]
                for key in (
                    "rolcanlogin",
                    "rolsuper",
                    "rolbypassrls",
                    "rolcreaterole",
                    "rolcreatedb",
                    "rolreplication",
                )
            ):
                raise RuntimeError("Existing capability roles require boundary review")
            if (
                kind != "guard"
                and conn.exec_driver_sql(
                    "SELECT pg_has_role(%s,%s,'MEMBER')", (role["oid"], existing_guard)
                ).scalar_one()
            ):
                raise RuntimeError("Runtime capability cannot inherit guard")
        owner = conn.exec_driver_sql(
            "SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(%s)", (p + ".current_actor()",)
        ).scalar_one_or_none()
        if (
            owner != existing_guard
            or conn.exec_driver_sql(
                "SELECT has_schema_privilege(%s,to_regnamespace(%s)::oid,'CREATE')", (existing_guard, p)
            ).scalar_one()
        ):
            raise RuntimeError("Existing guard ownership requires boundary review")
        if conn.exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=%s AND relkind IN ('r','p'))",
            (existing_guard,),
        ).scalar_one():
            raise RuntimeError("Guard must not own private tables")
    base = quote(op.get_bind().exec_driver_sql("SELECT current_schema()").scalar_one())
    base_name = op.get_bind().exec_driver_sql("SELECT current_schema()").scalar_one()
    schemas = [base_name] + [
        f"{base_name}_{kind}" if base_name.startswith("test_") else kind
        for kind in ("policy", "staging", "corpus")
    ]
    schema_literals = ",".join(
        op.get_bind().exec_driver_sql("SELECT quote_literal(%s)", (name,)).scalar_one() for name in schemas
    )
    base_literal = op.get_bind().exec_driver_sql("SELECT quote_literal(%s)", (base_name,)).scalar_one()
    allowed_roles = ",".join(
        repr(prefix + "_" + kind) for kind in ("serving", "review", "acquisition", "release")
    )
    op.execute(f"""
        ALTER TABLE {p}.actor
            ADD COLUMN database_role_name text UNIQUE,
            ADD COLUMN subject_issuer text,
            ADD COLUMN subject_key text,
            ADD COLUMN authenticated_until timestamptz,
            ADD COLUMN binding_evidence_reference text,
            ADD COLUMN binding_evidence_hash text,
            ADD COLUMN registration_reason text,
            ADD CONSTRAINT uq_operator_subject UNIQUE (subject_issuer,subject_key),
            ADD CONSTRAINT operator_binding_complete CHECK (
                (database_role_name IS NULL AND subject_issuer IS NULL AND subject_key IS NULL
                 AND authenticated_until IS NULL AND binding_evidence_reference IS NULL
                 AND binding_evidence_hash IS NULL AND registration_reason IS NULL)
                OR (database_role_name IS NOT NULL AND length(database_role_name) BETWEEN 1 AND 63
                 AND subject_issuer IS NOT NULL AND length(btrim(subject_issuer)) BETWEEN 1 AND 160
                 AND subject_key IS NOT NULL AND length(btrim(subject_key)) BETWEEN 1 AND 256
                 AND authenticated_until IS NOT NULL AND isfinite(authenticated_until)
                 AND binding_evidence_reference IS NOT NULL
                 AND length(btrim(binding_evidence_reference)) BETWEEN 1 AND 2048
                 AND binding_evidence_hash IS NOT NULL AND binding_evidence_hash ~ '^[a-f0-9]{{64}}$'
                 AND registration_reason IS NOT NULL AND length(btrim(registration_reason)) BETWEEN 1 AND 2048));
        CREATE TABLE {p}.operator_event (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            administrator text NOT NULL,
            event_kind text NOT NULL CHECK (event_kind IN
                ('registered','binding_updated','assignment_added','assignment_removed')),
            collection_id text REFERENCES {base}.identity_collections(id),
            capability text CHECK (capability IN ('rights','content','acquire','release')),
            active boolean NOT NULL,
            database_role oid NOT NULL,
            database_role_name text NOT NULL,
            subject_issuer text NOT NULL,
            subject_key text NOT NULL,
            authenticated_until timestamptz NOT NULL,
            evidence_reference text NOT NULL,
            evidence_hash text NOT NULL,
            reason text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            CHECK ((event_kind IN ('registered','binding_updated') AND collection_id IS NULL AND capability IS NULL)
                OR (event_kind IN ('assignment_added','assignment_removed') AND collection_id IS NOT NULL AND capability IS NOT NULL))
        );
        CREATE TABLE {p}.command_receipt (
            command_id uuid PRIMARY KEY,
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            collection_id text NOT NULL,
            action text NOT NULL CHECK (action IN
                ('record_evidence','record_collection_decision','record_verification_review')),
            payload_hash text NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{{64}}$'),
            evidence_id uuid NOT NULL,
            revision bigint NOT NULL CHECK (revision > 0),
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            FOREIGN KEY (evidence_id,collection_id) REFERENCES {p}.evidence(id,collection_id)
        );
        CREATE FUNCTION {p}.record_operator_binding() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$ BEGIN
            IF TG_OP='UPDATE' AND OLD.database_role_name IS NOT NULL AND
                (NEW.id,NEW.database_role,NEW.database_role_name,NEW.subject_issuer,NEW.subject_key)
                IS DISTINCT FROM
                (OLD.id,OLD.database_role,OLD.database_role_name,OLD.subject_issuer,OLD.subject_key) THEN
                RAISE EXCEPTION 'Operator identity cannot be rebound' USING ERRCODE='23514';
            END IF;
            IF NEW.database_role_name IS NOT NULL THEN
                INSERT INTO {p}.operator_event(actor_id,administrator,event_kind,active,database_role,database_role_name,
                    subject_issuer,subject_key,authenticated_until,evidence_reference,evidence_hash,reason)
                VALUES(NEW.id,session_user,CASE WHEN TG_OP='INSERT' THEN 'registered' ELSE 'binding_updated' END,
                    NEW.active,NEW.database_role,NEW.database_role_name,
                    NEW.subject_issuer,NEW.subject_key,NEW.authenticated_until,NEW.binding_evidence_reference,
                    NEW.binding_evidence_hash,NEW.registration_reason);
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER operator_binding BEFORE UPDATE ON {p}.actor
            FOR EACH ROW EXECUTE FUNCTION {p}.record_operator_binding();
        CREATE TRIGGER operator_registration AFTER INSERT ON {p}.actor
            FOR EACH ROW EXECUTE FUNCTION {p}.record_operator_binding();
        CREATE FUNCTION {p}.record_operator_assignment() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$
        DECLARE subject {p}.actor; assignment {p}.assignment;
        BEGIN
            IF TG_OP='INSERT' THEN assignment:=NEW; ELSE assignment:=OLD; END IF;
            SELECT * INTO subject FROM {p}.actor WHERE id=assignment.actor_id
                AND database_role_name IS NOT NULL FOR UPDATE;
            IF subject.id IS NOT NULL THEN
                INSERT INTO {p}.operator_event(actor_id,administrator,event_kind,collection_id,capability,
                    active,database_role,database_role_name,subject_issuer,subject_key,authenticated_until,
                    evidence_reference,evidence_hash,reason)
                VALUES(subject.id,session_user,CASE WHEN TG_OP='INSERT' THEN 'assignment_added' ELSE 'assignment_removed' END,
                    assignment.collection_id,assignment.capability,subject.active,subject.database_role,
                    subject.database_role_name,subject.subject_issuer,subject.subject_key,subject.authenticated_until,
                    subject.binding_evidence_reference,subject.binding_evidence_hash,'Collection assignment changed by maintenance');
            END IF;
            RETURN NULL;
        END $$;
        CREATE TRIGGER operator_assignment AFTER INSERT OR DELETE ON {p}.assignment
            FOR EACH ROW EXECUTE FUNCTION {p}.record_operator_assignment();
        CREATE TRIGGER assignment_append_only BEFORE UPDATE OR TRUNCATE ON {p}.assignment
            FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        CREATE FUNCTION {p}.operator_role_safe(subject oid) RETURNS boolean LANGUAGE sql STABLE
        SECURITY DEFINER SET search_path=pg_catalog AS $$
            WITH RECURSIVE inherited(oid) AS (
                SELECT subject UNION
                SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN inherited i ON i.oid=m.member
            ) SELECT NOT EXISTS (
                SELECT 1 FROM inherited i JOIN pg_catalog.pg_roles r ON r.oid=i.oid
                WHERE r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
                    OR (r.oid<>subject AND r.rolname NOT IN ({allowed_roles})))
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid=subject
                    AND r.rolname IN ({allowed_roles},'{prefix}_guard'))
                AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname IN ({schema_literals})
                    AND has_schema_privilege(subject,n.oid,'CREATE'))
                AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname IN ({schema_literals}) AND (c.relowner=subject OR
                        (n.nspname={base_literal} AND c.relkind IN ('r','p') AND
                            has_table_privilege(subject,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE'))))
        $$;
        CREATE OR REPLACE FUNCTION {p}.current_actor() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog AS $$
            SELECT a.id FROM {p}.actor a JOIN pg_catalog.pg_roles r ON r.oid=a.database_role
            WHERE a.active AND NOT r.rolsuper AND NOT r.rolbypassrls AND r.rolname=session_user::text
                AND a.identity_kind='synthetic' AND (
                    (a.database_role_name IS NULL AND NOT r.rolcanlogin)
                    OR (r.rolcanlogin AND a.database_role_name=r.rolname
                        AND a.authenticated_until>statement_timestamp()
                        AND (r.rolvaliduntil IS NULL OR r.rolvaliduntil>statement_timestamp())
                        AND {p}.operator_role_safe(r.oid)))
        $$;
        CREATE FUNCTION {p}.operator_keys(data jsonb, required text[], optional text[] DEFAULT ARRAY[]::text[])
        RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
            SELECT CASE WHEN jsonb_typeof(data)='object' THEN
                data ?& required AND NOT EXISTS (
                    SELECT 1 FROM jsonb_object_keys(data) k WHERE NOT k=ANY(required||optional))
                ELSE false END
        $$;
    """)
    common_keys = "'evidence_id','valid_from','expires_at','state'"
    decision_keys = (
        common_keys + ", 'basis_type','conditions_satisfied','metadata_privacy_class','privacy_reason'"
    )
    optional_decision = ",".join(
        repr(name) for name in (*PERMISSIONS, "privacy_review_id", "privacy_review_revision")
    )
    vectors = ",".join(PERMISSIONS)
    vector_values = ",".join(f"coalesce(body->>'{name}','unknown')::{p}.permission" for name in PERMISSIONS)
    required_materials = ",".join(repr(name) for name in MATERIALS)
    op.execute(f"""
        CREATE FUNCTION {p}.apply_operator_command(envelope jsonb)
        RETURNS TABLE(revision bigint,replayed boolean) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$
        DECLARE aid uuid; cid text; cmd uuid; action_name text; expected bigint; reason_value text;
            body jsonb; fingerprint text; previous {p}.command_receipt; evidence uuid;
            material jsonb; classes text[];
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR NOT {p}.operator_keys(envelope,
                ARRAY['command_id','collection_id','action','expected_revision','reason','payload']) THEN
                RAISE EXCEPTION 'Invalid operator command' USING ERRCODE='22023'; END IF;
            -- Hold authorization until the caller commits; administrator revocation waits.
            SELECT a.id INTO aid FROM {p}.actor a JOIN pg_catalog.pg_roles r ON r.oid=a.database_role
                WHERE r.rolname=session_user::text AND a.database_role_name=r.rolname AND a.active
                FOR SHARE OF a;
            IF aid IS NULL OR aid IS DISTINCT FROM {p}.current_actor() THEN
                RAISE EXCEPTION 'Individual operator session required' USING ERRCODE='42501'; END IF;
            -- Synthetic shadow commands never become a live target-authority writer.
            PERFORM {base}.identity_check_legacy_authority(true);
            cid := envelope->>'collection_id'; cmd := (envelope->>'command_id')::uuid;
            action_name := envelope->>'action'; expected := (envelope->>'expected_revision')::bigint;
            reason_value := envelope->>'reason'; body := envelope->'payload';
            IF cmd IS NULL OR cid IS NULL OR expected IS NULL OR expected<0
                OR reason_value IS NULL OR length(btrim(reason_value)) NOT BETWEEN 1 AND 2048
                OR action_name IS NULL OR action_name NOT IN
                    ('record_evidence','record_collection_decision','record_verification_review') THEN
                RAISE EXCEPTION 'Invalid operator command' USING ERRCODE='22023'; END IF;
            IF (action_name='record_verification_review' AND NOT {p}.assigned(cid,'content'))
                OR (action_name='record_collection_decision' AND NOT {p}.assigned(cid,'rights'))
                OR (action_name='record_evidence' AND NOT
                    ({p}.assigned(cid,'rights') OR {p}.assigned(cid,'content'))) THEN
                RAISE EXCEPTION 'Collection authority required' USING ERRCODE='42501'; END IF;
            fingerprint := encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            -- One transaction-scoped retry lock, shared by both individual sessions.
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:'||cmd::text,0));
            SELECT * INTO previous FROM {p}.command_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>aid THEN
                    RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN
                    RAISE EXCEPTION 'Command retry changed' USING ERRCODE='40001'; END IF;
                RETURN QUERY SELECT previous.revision,true; RETURN;
            END IF;
            IF action_name='record_evidence' THEN
                IF expected<>0 OR NOT {p}.operator_keys(body,
                    ARRAY['id','private_reference','evidence_hash','observed_at']) THEN
                    RAISE EXCEPTION 'Invalid evidence command' USING ERRCODE='22023'; END IF;
                evidence := (body->>'id')::uuid;
                IF (body->>'observed_at')::timestamptz>statement_timestamp() THEN
                    RAISE EXCEPTION 'Future evidence observation' USING ERRCODE='22023'; END IF;
                INSERT INTO {p}.evidence(id,collection_id,private_reference,evidence_hash,observed_at)
                    VALUES(evidence,cid,body->>'private_reference',body->>'evidence_hash',
                        (body->>'observed_at')::timestamptz);
                INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                    VALUES(aid,cid,evidence,'evidence',evidence::text,1,reason_value);
            ELSE
                evidence := (body->>'evidence_id')::uuid;
                IF NOT isfinite((body->>'valid_from')::timestamptz)
                    OR NOT isfinite((body->>'expires_at')::timestamptz) THEN
                    RAISE EXCEPTION 'Finite review interval required' USING ERRCODE='22023'; END IF;
                IF body->>'state'='approved' AND (body->>'expires_at')::timestamptz<=statement_timestamp() THEN
                    RAISE EXCEPTION 'Expired review cannot approve' USING ERRCODE='22023'; END IF;
                IF action_name='record_collection_decision' THEN
                    IF NOT {p}.operator_keys(body,ARRAY[{decision_keys}],ARRAY[{optional_decision}]) THEN
                        RAISE EXCEPTION 'Invalid collection decision' USING ERRCODE='22023'; END IF;
                    INSERT INTO {p}.collection_decision(collection_id,actor_id,revision,evidence_id,
                        valid_from,expires_at,state,reason,basis_type,conditions_satisfied,metadata_privacy_class,
                        privacy_reason,privacy_review_id,privacy_review_revision,{vectors})
                    VALUES(cid,aid,expected+1,evidence,(body->>'valid_from')::timestamptz,
                        (body->>'expires_at')::timestamptz,body->>'state',reason_value,body->>'basis_type',
                        (body->>'conditions_satisfied')::boolean,body->>'metadata_privacy_class',
                        body->>'privacy_reason',(body->>'privacy_review_id')::uuid,
                        (body->>'privacy_review_revision')::bigint,{vector_values});
                ELSE
                    IF NOT {p}.operator_keys(body,ARRAY[{common_keys},'sample_numerator',
                        'sample_denominator','escalation_rule','materials'])
                        OR jsonb_typeof(body->'materials') IS DISTINCT FROM 'array' THEN
                        RAISE EXCEPTION 'Invalid verification review' USING ERRCODE='22023'; END IF;
                    SELECT array_agg(m->>'material_class') INTO classes FROM jsonb_array_elements(body->'materials') m;
                    IF cardinality(classes)<>6 OR NOT classes @> ARRAY[{required_materials}]::text[] THEN
                        RAISE EXCEPTION 'Complete material coverage required' USING ERRCODE='22023'; END IF;
                    INSERT INTO {p}.verification_policy(collection_id,actor_id,revision,evidence_id,
                        valid_from,expires_at,state,reason,sample_numerator,sample_denominator,
                        escalation_rule,material_classes)
                    VALUES(cid,aid,expected+1,evidence,(body->>'valid_from')::timestamptz,
                        (body->>'expires_at')::timestamptz,body->>'state',reason_value,
                        (body->>'sample_numerator')::integer,(body->>'sample_denominator')::integer,
                        body->>'escalation_rule',classes);
                    FOR material IN SELECT value FROM jsonb_array_elements(body->'materials') LOOP
                        IF NOT {p}.operator_keys(material,ARRAY['material_class','method','sample_numerator',
                            'sample_denominator','competence_evidence_id','escalation_rule','exclusion_rule']) THEN
                            RAISE EXCEPTION 'Invalid material review' USING ERRCODE='22023'; END IF;
                        INSERT INTO {p}.verification_material(collection_id,revision,material_class,method,
                            sample_numerator,sample_denominator,competence_evidence_id,escalation_rule,exclusion_rule)
                        VALUES(cid,expected+1,material->>'material_class',material->>'method',
                            (material->>'sample_numerator')::integer,(material->>'sample_denominator')::integer,
                            (material->>'competence_evidence_id')::uuid,material->>'escalation_rule',
                            material->>'exclusion_rule');
                    END LOOP;
                END IF;
            END IF;
            INSERT INTO {p}.command_receipt(command_id,actor_id,collection_id,action,payload_hash,evidence_id,revision)
                VALUES(cmd,aid,cid,action_name,fingerprint,evidence,expected+1);
            RETURN QUERY SELECT expected+1,false;
        END $$;
        REVOKE ALL ON FUNCTION {p}.operator_role_safe(oid), {p}.operator_keys(jsonb,text[],text[]),
            {p}.apply_operator_command(jsonb), {p}.record_operator_binding(),{p}.record_operator_assignment() FROM PUBLIC;
    """)
    for table in ("operator_event", "command_receipt"):
        op.execute(f"""
            CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {p}.{table}
                FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
            ALTER TABLE {p}.{table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {p}.{table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY private_operator ON {p}.{table} USING (current_user='{prefix}_guard');
            REVOKE ALL ON {p}.{table} FROM PUBLIC;
        """)
    if (
        op.get_bind()
        .exec_driver_sql("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)", (prefix + "_guard",))
        .scalar_one()
    ):
        guard, review = quote(prefix + "_guard"), quote(prefix + "_review")
        op.execute(f"""
            GRANT CREATE ON SCHEMA {p} TO {guard};
            GRANT SELECT,INSERT ON {p}.command_receipt,{p}.operator_event TO {guard};
            GRANT INSERT ON {p}.evidence TO {guard};
            GRANT INSERT ON {p}.collection_decision,{p}.verification_policy,{p}.verification_material TO {guard};
            GRANT UPDATE(active) ON {p}.actor TO {guard};
            ALTER FUNCTION {p}.operator_role_safe(oid) OWNER TO {guard};
            ALTER FUNCTION {p}.operator_keys(jsonb,text[],text[]) OWNER TO {guard};
            ALTER FUNCTION {p}.apply_operator_command(jsonb) OWNER TO {guard};
            ALTER FUNCTION {p}.record_operator_binding() OWNER TO {guard};
            ALTER FUNCTION {p}.record_operator_assignment() OWNER TO {guard};
            REVOKE CREATE ON SCHEMA {p} FROM {guard};
            GRANT EXECUTE ON FUNCTION {p}.apply_operator_command(jsonb) TO {review};
        """)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    p, prefix, quote = names()
    conn = op.get_bind()
    if (
        conn.exec_driver_sql(
            f"SELECT EXISTS(SELECT 1 FROM {p}.actor WHERE database_role_name IS NOT NULL)"
        ).scalar_one()
        or conn.exec_driver_sql(f"SELECT EXISTS(SELECT 1 FROM {p}.command_receipt)").scalar_one()
    ):
        raise RuntimeError("Registered operator foundation requires forward repair")
    if conn.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)", (prefix + "_guard",)
    ).scalar_one():
        guard = quote(prefix + "_guard")
        op.execute(
            f"REVOKE INSERT ON {p}.evidence,{p}.collection_decision,{p}.verification_policy,{p}.verification_material FROM {guard}"
        )
        op.execute(f"REVOKE UPDATE(active) ON {p}.actor FROM {guard}")
    op.execute(f"""
        DROP FUNCTION {p}.apply_operator_command(jsonb);
        CREATE OR REPLACE FUNCTION {p}.current_actor() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog AS $$
            SELECT a.id FROM {p}.actor a JOIN pg_catalog.pg_roles r ON r.oid=a.database_role
            WHERE a.active AND NOT r.rolsuper AND NOT r.rolbypassrls AND r.rolname=session_user::text
        $$;
        DROP FUNCTION {p}.operator_role_safe(oid);
        DROP FUNCTION {p}.operator_keys(jsonb,text[],text[]);
        DROP TRIGGER operator_binding ON {p}.actor;
        DROP TRIGGER operator_registration ON {p}.actor;
        DROP FUNCTION {p}.record_operator_binding();
        DROP TRIGGER operator_assignment ON {p}.assignment;
        DROP TRIGGER assignment_append_only ON {p}.assignment;
        DROP FUNCTION {p}.record_operator_assignment();
        DROP TABLE {p}.command_receipt,{p}.operator_event;
        ALTER TABLE {p}.actor DROP CONSTRAINT operator_binding_complete,
            DROP CONSTRAINT uq_operator_subject,
            DROP COLUMN database_role_name,DROP COLUMN subject_issuer,DROP COLUMN subject_key,
            DROP COLUMN authenticated_until,DROP COLUMN binding_evidence_reference,
            DROP COLUMN binding_evidence_hash,DROP COLUMN registration_reason;
    """)
