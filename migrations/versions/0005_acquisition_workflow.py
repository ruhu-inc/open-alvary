"""Synthetic native-session acquisition review, exact bytes and bounded private staging.

Applied revisions stay immutable. No network/observation writer or publication exists.
"""

from hashlib import sha256

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def names():
    conn = op.get_bind()
    base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
    quote = conn.dialect.identifier_preparer.quote_identifier
    schemas = {
        kind: quote(f"{base}_{kind}" if base.startswith("test_") else kind)
        for kind in ("policy", "staging", "corpus")
    }
    prefix = "oa_" + sha256(base.encode()).hexdigest()[:12]
    return quote(base), schemas["policy"], schemas["staging"], prefix, quote


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    b, p, s, prefix, quote = names()
    guard_oid = conn.exec_driver_sql(
        "SELECT oid FROM pg_roles WHERE rolname=%s", (prefix + "_guard",)
    ).scalar_one_or_none()
    if guard_oid is not None:
        for kind in ("guard", "serving", "review", "acquisition", "release"):
            profile = (
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
            if profile is None or any(
                profile[key]
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
                    "SELECT pg_has_role(%s,%s,'MEMBER')", (profile["oid"], guard_oid)
                ).scalar_one()
            ):
                raise RuntimeError("Runtime capability cannot inherit guard")
        owner = conn.exec_driver_sql(
            "SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(%s)",
            (p + ".apply_operator_command(jsonb)",),
        ).scalar_one_or_none()
        if (
            owner != guard_oid
            or conn.exec_driver_sql(
                "SELECT has_schema_privilege(%s,to_regnamespace(%s)::oid,'CREATE')", (guard_oid, p)
            ).scalar_one()
            or conn.exec_driver_sql(
                "SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=%s AND relkind IN ('r','p'))",
                (guard_oid,),
            ).scalar_one()
        ):
            raise RuntimeError("Existing guard ownership requires boundary review")
    op.execute(f"""
        ALTER TABLE {p}.acquisition_assessment ADD COLUMN artifact_hash text
            CHECK (artifact_hash ~ '^[a-f0-9]{{64}}$');
        ALTER TABLE {p}.command_receipt DROP CONSTRAINT command_receipt_action_check,
            ADD CONSTRAINT command_receipt_action_check CHECK (action IN ('record_evidence',
                'record_collection_decision','record_verification_review','record_controller',
                'record_privacy_review','record_acquisition_assessment'));
        ALTER TABLE {s}.staged_artifact ADD COLUMN purpose text,
            ADD COLUMN acquired_by uuid REFERENCES {p}.actor(id),
            ADD CONSTRAINT qualified_staging_pair CHECK (
                (purpose IS NULL AND acquired_by IS NULL) OR
                (purpose IS NOT NULL AND length(btrim(purpose)) BETWEEN 1 AND 2048 AND acquired_by IS NOT NULL));
        CREATE TABLE {p}.synthetic_staging_limit (
            collection_id text PRIMARY KEY REFERENCES {b}.identity_collections(id),
            profile text NOT NULL DEFAULT 'synthetic_public_min' CHECK (profile='synthetic_public_min'),
            maximum_bytes bigint NOT NULL CHECK (maximum_bytes BETWEEN 1 AND 104857600),
            evidence_id uuid NOT NULL,
            reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2048),
            declared_by text NOT NULL DEFAULT session_user,
            declared_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            FOREIGN KEY (evidence_id,collection_id) REFERENCES {p}.evidence(id,collection_id)
        );
        CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {p}.synthetic_staging_limit
            FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        ALTER TABLE {p}.synthetic_staging_limit ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {p}.synthetic_staging_limit FORCE ROW LEVEL SECURITY;
        CREATE POLICY limit_guard ON {p}.synthetic_staging_limit USING (current_user='{prefix}_guard');
        CREATE POLICY limit_declaration ON {p}.synthetic_staging_limit FOR INSERT WITH CHECK (
            declared_by=session_user::text AND current_user=pg_get_userbyid(
                (SELECT relowner FROM pg_class WHERE oid='{p}.synthetic_staging_limit'::regclass)));
        REVOKE ALL ON {p}.synthetic_staging_limit FROM PUBLIC;

        CREATE FUNCTION {p}.lock_acquisition_authority(aid uuid, rev bigint, cid text)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE assessment {p}.acquisition_assessment; privacy {p}.privacy_review; ctl {p}.controller_record;
        BEGIN
            SELECT * INTO assessment FROM {p}.acquisition_assessment WHERE id=aid AND revision=rev AND collection_id=cid;
            SELECT * INTO privacy FROM {p}.privacy_review WHERE id=assessment.privacy_review_id
                AND revision=assessment.privacy_review_revision AND collection_id=cid;
            SELECT * INTO ctl FROM {p}.controller_record WHERE id=privacy.controller_id
                AND revision=privacy.controller_revision AND collection_id=cid;
            PERFORM 1 FROM {p}.actor WHERE id IN (assessment.actor_id,privacy.actor_id,ctl.actor_id)
                ORDER BY id FOR SHARE;
            PERFORM 1 FROM {p}.revision_head WHERE (kind='acquisition' AND scope_id=aid::text)
                OR (kind='privacy' AND scope_id=privacy.id::text)
                OR (kind='controller' AND scope_id=ctl.id::text) ORDER BY kind,scope_id FOR SHARE;
        END $$;
        CREATE FUNCTION {p}.private_operation_allowed(aid uuid, rev bigint, cid text,
            requested_purpose text, hash text, operation text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT coalesce(operation IN ('discover','acquire','retain','derive') AND
                {p}.assigned(cid,'acquire') AND EXISTS (
                    SELECT 1 FROM {p}.actor WHERE id={p}.current_actor() AND database_role_name IS NOT NULL)
                AND {p}.acquisition_current(aid,rev,cid,requested_purpose,hash) AND EXISTS (
                    SELECT 1 FROM {p}.acquisition_assessment a WHERE a.id=aid AND a.revision=rev
                        AND a.collection_id=cid AND a.artifact_hash=hash AND a.purpose=requested_purpose
                        AND CASE operation WHEN 'discover' THEN a.discover WHEN 'acquire' THEN a.acquire
                            WHEN 'retain' THEN a.retain WHEN 'derive' THEN a.derive ELSE NULL END='allow'
                        AND (a.privacy_review_id IS NULL OR EXISTS (
                            SELECT 1 FROM {p}.privacy_review v WHERE v.id=a.privacy_review_id
                                AND v.revision=a.privacy_review_revision AND v.collection_id=cid
                                AND v.purpose=requested_purpose))),false)
        $$;
        CREATE FUNCTION {p}.apply_acquisition_command(envelope jsonb)
        RETURNS TABLE(revision bigint,replayed boolean) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$
        DECLARE aid uuid; cid text; cmd uuid; action_name text; expected bigint; reason_value text;
            body jsonb; fingerprint text; previous {p}.command_receipt; evidence uuid;
            ctl {p}.controller_record; privacy {p}.privacy_review; related uuid;
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR NOT {p}.operator_keys(envelope,
                ARRAY['command_id','collection_id','action','expected_revision','reason','payload']) THEN
                RAISE EXCEPTION 'Invalid operator command' USING ERRCODE='22023'; END IF;
            SELECT a.id INTO aid FROM {p}.actor a JOIN pg_roles r ON r.oid=a.database_role
                WHERE r.rolname=session_user::text AND a.database_role_name=r.rolname AND a.active FOR SHARE OF a;
            IF aid IS NULL OR aid IS DISTINCT FROM {p}.current_actor() THEN
                RAISE EXCEPTION 'Individual operator session required' USING ERRCODE='42501'; END IF;
            PERFORM {b}.identity_check_legacy_authority(true);
            cid:=envelope->>'collection_id'; cmd:=(envelope->>'command_id')::uuid;
            action_name:=envelope->>'action'; expected:=(envelope->>'expected_revision')::bigint;
            reason_value:=envelope->>'reason'; body:=envelope->'payload';
            IF cmd IS NULL OR cid IS NULL OR expected IS NULL OR expected<0 OR reason_value IS NULL
                OR length(btrim(reason_value)) NOT BETWEEN 1 AND 2048 OR action_name IS NULL OR
                action_name NOT IN ('record_controller','record_privacy_review','record_acquisition_assessment') THEN
                RAISE EXCEPTION 'Invalid acquisition command' USING ERRCODE='22023'; END IF;
            IF NOT {p}.assigned(cid,'rights') THEN
                RAISE EXCEPTION 'Rights assessment authority required' USING ERRCODE='42501'; END IF;
            fingerprint:=encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:'||cmd::text,0));
            SELECT * INTO previous FROM {p}.command_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>aid THEN RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Command retry changed' USING ERRCODE='40001'; END IF;
                RETURN QUERY SELECT previous.revision,true; RETURN;
            END IF;
            IF NOT {p}.operator_keys(body,ARRAY['id','evidence_id','valid_from','expires_at','state'],
                CASE action_name WHEN 'record_controller' THEN ARRAY['legal_name','postal_address',
                    'jurisdiction_id','privacy_contact','accountable_role']
                WHEN 'record_privacy_review' THEN ARRAY['controller_id','controller_revision','purpose',
                    'lawful_basis','assessment_reference','clearance','cleared_hash']
                ELSE ARRAY['discover','acquire','retain','derive','privacy_class','classification_reason',
                    'privacy_review_id','privacy_review_revision','purpose','retention_deadline','artifact_hash'] END)
                OR NOT isfinite((body->>'valid_from')::timestamptz) OR NOT isfinite((body->>'expires_at')::timestamptz)
                OR (body->>'state'='approved' AND (body->>'expires_at')::timestamptz<=statement_timestamp()) THEN
                RAISE EXCEPTION 'Invalid review interval or fields' USING ERRCODE='22023'; END IF;
            evidence:=(body->>'evidence_id')::uuid;
            IF action_name='record_controller' THEN
                INSERT INTO {p}.controller_record(id,revision,collection_id,actor_id,evidence_id,
                    valid_from,expires_at,state,reason,legal_name,postal_address,jurisdiction_id,privacy_contact,accountable_role)
                VALUES((body->>'id')::uuid,expected+1,cid,aid,evidence,(body->>'valid_from')::timestamptz,
                    (body->>'expires_at')::timestamptz,body->>'state',reason_value,body->>'legal_name',
                    body->>'postal_address',body->>'jurisdiction_id',body->>'privacy_contact',body->>'accountable_role');
            ELSIF action_name='record_privacy_review' THEN
                related:=(body->>'controller_id')::uuid;
                SELECT c.* INTO ctl FROM {p}.controller_record c WHERE c.id=related
                    AND c.revision=(body->>'controller_revision')::bigint AND c.collection_id=cid;
                PERFORM 1 FROM {p}.actor WHERE id=ctl.actor_id FOR SHARE;
                PERFORM 1 FROM {p}.revision_head WHERE kind='controller' AND scope_id=related::text FOR SHARE;
                IF body->>'state'='approved' AND body->>'clearance' IN ('cleared','redacted') AND
                    (ctl.id IS NULL OR ctl.state<>'approved' OR ctl.valid_from>statement_timestamp()
                    OR ctl.expires_at<=statement_timestamp() OR NOT EXISTS(SELECT 1 FROM {p}.actor WHERE id=ctl.actor_id AND active)
                    OR NOT EXISTS(SELECT 1 FROM {p}.revision_head h WHERE h.kind='controller' AND h.scope_id=related::text AND h.revision=ctl.revision)) THEN
                    RAISE EXCEPTION 'Current controller revision required' USING ERRCODE='42501'; END IF;
                INSERT INTO {p}.privacy_review(id,revision,collection_id,actor_id,evidence_id,valid_from,expires_at,
                    state,reason,controller_id,controller_revision,purpose,lawful_basis,assessment_reference,clearance,cleared_hash)
                VALUES((body->>'id')::uuid,expected+1,cid,aid,evidence,(body->>'valid_from')::timestamptz,
                    (body->>'expires_at')::timestamptz,body->>'state',reason_value,related,(body->>'controller_revision')::bigint,
                    body->>'purpose',body->>'lawful_basis',body->>'assessment_reference',body->>'clearance',body->>'cleared_hash');
            ELSE
                SELECT v.* INTO privacy FROM {p}.privacy_review v WHERE v.id=(body->>'privacy_review_id')::uuid
                    AND v.revision=(body->>'privacy_review_revision')::bigint AND v.collection_id=cid;
                SELECT c.* INTO ctl FROM {p}.controller_record c WHERE c.id=privacy.controller_id
                    AND c.revision=privacy.controller_revision AND c.collection_id=cid;
                PERFORM 1 FROM {p}.actor WHERE id IN (privacy.actor_id,ctl.actor_id) ORDER BY id FOR SHARE;
                PERFORM 1 FROM {p}.revision_head WHERE (kind='privacy' AND scope_id=privacy.id::text)
                    OR (kind='controller' AND scope_id=ctl.id::text) ORDER BY kind,scope_id FOR SHARE;
                IF body->>'state'='approved' AND (body->>'acquire'='allow' OR body->>'retain'='allow'
                    OR body->>'derive'='allow' OR body->>'discover'='allow') AND
                    (((body->>'privacy_class'<>'no_personal_data' OR EXISTS(SELECT 1 FROM {b}.identity_collections
                        WHERE id=cid AND document_class IN ('judgment','other'))) AND privacy.id IS NULL)
                    OR ((body->>'privacy_review_id') IS NOT NULL AND
                        (privacy.purpose IS DISTINCT FROM body->>'purpose'
                        OR NOT {p}.privacy_current(privacy.id,privacy.revision,cid,body->>'artifact_hash')))) THEN
                    RAISE EXCEPTION 'Current purpose and privacy authority required' USING ERRCODE='42501'; END IF;
                IF NOT isfinite((body->>'retention_deadline')::timestamptz) THEN
                    RAISE EXCEPTION 'Finite retention deadline required' USING ERRCODE='22023'; END IF;
                INSERT INTO {p}.acquisition_assessment(id,revision,collection_id,actor_id,evidence_id,valid_from,expires_at,
                    state,reason,discover,acquire,retain,derive,privacy_class,classification_reason,privacy_review_id,
                    privacy_review_revision,purpose,retention_deadline,artifact_hash)
                VALUES((body->>'id')::uuid,expected+1,cid,aid,evidence,(body->>'valid_from')::timestamptz,
                    (body->>'expires_at')::timestamptz,body->>'state',reason_value,
                    coalesce(body->>'discover','unknown')::{p}.permission,coalesce(body->>'acquire','unknown')::{p}.permission,
                    coalesce(body->>'retain','unknown')::{p}.permission,coalesce(body->>'derive','unknown')::{p}.permission,
                    body->>'privacy_class',body->>'classification_reason',(body->>'privacy_review_id')::uuid,
                    (body->>'privacy_review_revision')::bigint,body->>'purpose',(body->>'retention_deadline')::timestamptz,
                    body->>'artifact_hash');
            END IF;
            INSERT INTO {p}.command_receipt(command_id,actor_id,collection_id,action,payload_hash,evidence_id,revision)
                VALUES(cmd,aid,cid,action_name,fingerprint,evidence,expected+1);
            RETURN QUERY SELECT expected+1,false;
        END $$;
    """)
    install_staging(b, p, s, prefix)
    functions = (
        "lock_acquisition_authority(uuid,bigint,text)",
        "private_operation_allowed(uuid,bigint,text,text,text,text)",
        "apply_acquisition_command(jsonb)",
        "validate_synthetic_staging()",
        "stage_synthetic_artifact(uuid,text,uuid,bigint,text,bytea,timestamptz)",
        "read_synthetic_artifact(uuid,text,text)",
        "is_native_operator()",
    )
    for fn in functions:
        op.execute(f"REVOKE ALL ON FUNCTION {p}.{fn} FROM PUBLIC")
    if guard_oid is not None:
        guard, review, acquisition = (
            quote(prefix + "_" + kind) for kind in ("guard", "review", "acquisition")
        )
        op.execute(f"""
            GRANT CREATE ON SCHEMA {p} TO {guard};
            GRANT SELECT,UPDATE(maximum_bytes) ON {p}.synthetic_staging_limit TO {guard};
            GRANT INSERT ON {p}.controller_record,{p}.privacy_review,{p}.acquisition_assessment TO {guard};
            GRANT INSERT ON {s}.staged_artifact TO {guard};
        """)
        for fn in functions:
            op.execute(f"ALTER FUNCTION {p}.{fn} OWNER TO {guard}")
        op.execute(f"REVOKE CREATE ON SCHEMA {p} FROM {guard}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {p}.apply_acquisition_command(jsonb) TO {review}")
        for fn in functions[1:2] + functions[4:]:
            op.execute(f"GRANT EXECUTE ON FUNCTION {p}.{fn} TO {acquisition}")


def install_staging(b, p, s, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.is_native_operator() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {p}.actor WHERE id={p}.current_actor() AND database_role_name IS NOT NULL)
        $$;
        CREATE FUNCTION {p}.validate_synthetic_staging() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog AS $$
        DECLARE actor uuid; native boolean; capacity bigint; occupied bigint; evidence uuid;
        BEGIN
            actor:={p}.current_actor();
            SELECT a.database_role_name IS NOT NULL INTO native FROM {p}.actor a WHERE a.id=actor;
            IF TG_OP='UPDATE' AND (NEW.purpose,NEW.acquired_by) IS DISTINCT FROM (OLD.purpose,OLD.acquired_by) THEN
                RAISE EXCEPTION 'Staging qualification is immutable' USING ERRCODE='55000'; END IF;
            IF NOT coalesce(native,false) THEN
                IF TG_OP='INSERT' AND (NEW.purpose IS NOT NULL OR NEW.acquired_by IS NOT NULL) THEN
                    RAISE EXCEPTION 'Native acquisition subject required' USING ERRCODE='42501'; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP='UPDATE' THEN
                IF NEW.state='erased' AND OLD.state<>'erased' THEN
                    IF NOT EXISTS(SELECT 1 FROM {p}.audit_event WHERE kind='staging_erasure'
                        AND scope_id=NEW.id::text AND revision=1 AND actor_id=actor) THEN
                        RAISE EXCEPTION 'Audited erasure required' USING ERRCODE='42501'; END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Native staging lifecycle writer unavailable' USING ERRCODE='42501';
            END IF;
            PERFORM 1 FROM {p}.actor a WHERE a.id=actor FOR SHARE;
            PERFORM {b}.identity_check_legacy_authority(true);
            PERFORM {p}.lock_acquisition_authority(NEW.assessment_id,NEW.assessment_revision,NEW.collection_id);
            IF NEW.acquired_by IS DISTINCT FROM actor OR NEW.purpose IS NULL OR NEW.state<>'staged'
                OR NOT isfinite(NEW.retention_deadline) OR NOT
                {p}.private_operation_allowed(NEW.assessment_id,NEW.assessment_revision,NEW.collection_id,
                    NEW.purpose,NEW.artifact_hash,'acquire') THEN
                RAISE EXCEPTION 'Exact synthetic acquisition authority required' USING ERRCODE='42501'; END IF;
            SELECT maximum_bytes INTO capacity FROM {p}.synthetic_staging_limit WHERE collection_id=NEW.collection_id FOR UPDATE;
            IF capacity IS NULL THEN RAISE EXCEPTION 'Explicit synthetic staging limit required' USING ERRCODE='42501'; END IF;
            SELECT coalesce(sum(octet_length(private_bytes)),0) INTO occupied FROM {s}.staged_artifact WHERE collection_id=NEW.collection_id;
            IF occupied+NEW.size_bytes>capacity THEN RAISE EXCEPTION 'Synthetic staging capacity exceeded' USING ERRCODE='54000'; END IF;
            SELECT evidence_id INTO evidence FROM {p}.acquisition_assessment WHERE id=NEW.assessment_id
                AND revision=NEW.assessment_revision AND collection_id=NEW.collection_id;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(actor,NEW.collection_id,evidence,'synthetic_staging',NEW.id::text,1,'Original synthetic artifact staged');
            RETURN NEW;
        END $$;
        CREATE TRIGGER synthetic_staging BEFORE INSERT OR UPDATE ON {s}.staged_artifact
            FOR EACH ROW EXECUTE FUNCTION {p}.validate_synthetic_staging();
        ALTER POLICY staged_read ON {s}.staged_artifact USING (
            current_user='{prefix}_guard' OR (retention_deadline>statement_timestamp()
                AND state IN ('staged','processing','validated') AND {p}.staging_allowed(assessment_id,assessment_revision,collection_id,artifact_hash)
                AND (NOT {p}.is_native_operator()
                    OR {p}.private_operation_allowed(assessment_id,assessment_revision,collection_id,purpose,artifact_hash,'retain'))));
        CREATE FUNCTION {p}.stage_synthetic_artifact(artifact_id uuid,cid text,assessment uuid,rev bigint,
            requested_purpose text,bytes bytea,deadline timestamptz) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE existing {s}.staged_artifact; actor uuid; hash text;
        BEGIN
            actor:={p}.current_actor();
            IF actor IS NULL OR NOT EXISTS(SELECT 1 FROM {p}.actor a WHERE a.id=actor AND a.database_role_name IS NOT NULL)
                OR NOT {p}.assigned(cid,'acquire') THEN
                RAISE EXCEPTION 'Native acquisition subject required' USING ERRCODE='42501'; END IF;
            PERFORM 1 FROM {p}.actor a WHERE a.id=actor FOR SHARE;
            PERFORM {b}.identity_check_legacy_authority(true);
            IF bytes IS NULL OR octet_length(bytes)>10485760 THEN RAISE EXCEPTION 'Artifact bound exceeded' USING ERRCODE='22023'; END IF;
            hash:=encode(sha256(bytes),'hex');
            PERFORM {p}.lock_acquisition_authority(assessment,rev,cid);
            IF NOT {p}.private_operation_allowed(assessment,rev,cid,requested_purpose,hash,'acquire') THEN
                RAISE EXCEPTION 'Exact current acquisition authority required' USING ERRCODE='42501'; END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:stage:'||artifact_id::text,0));
            SELECT * INTO existing FROM {s}.staged_artifact WHERE id=artifact_id FOR UPDATE;
            IF existing.id IS NOT NULL THEN
                IF (existing.collection_id,existing.assessment_id,existing.assessment_revision,existing.purpose,
                    existing.artifact_hash,existing.retention_deadline,existing.acquired_by) IS DISTINCT FROM
                    (cid,assessment,rev,requested_purpose,hash,deadline,actor) THEN
                    RAISE EXCEPTION 'Staging retry changed' USING ERRCODE='40001'; END IF;
                IF existing.private_bytes IS NULL OR existing.retention_deadline<=statement_timestamp() THEN
                    RAISE EXCEPTION 'Retry cannot restore erased or expired bytes' USING ERRCODE='42501'; END IF;
                RETURN true;
            END IF;
            INSERT INTO {s}.staged_artifact(id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,
                private_bytes,retention_deadline,purpose,acquired_by)
            VALUES(artifact_id,cid,assessment,rev,hash,octet_length(bytes),bytes,deadline,requested_purpose,actor);
            RETURN false;
        END $$;
        CREATE FUNCTION {p}.read_synthetic_artifact(artifact_id uuid,requested_purpose text,operation text) RETURNS bytea
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE artifact {s}.staged_artifact; actor uuid; bytes bytea;
        BEGIN
            actor:={p}.current_actor();
            IF actor IS NULL THEN RETURN NULL; END IF;
            PERFORM 1 FROM {p}.actor a WHERE a.id=actor FOR SHARE;
            PERFORM {b}.identity_check_legacy_authority(true);
            SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                acquired_at,retention_deadline,state,purpose,acquired_by INTO artifact
                FROM {s}.staged_artifact WHERE id=artifact_id FOR SHARE;
            IF artifact.id IS NULL OR artifact.retention_deadline<=statement_timestamp()
                OR artifact.state NOT IN ('staged','processing','validated') OR operation NOT IN ('retain','derive') THEN RETURN NULL; END IF;
            PERFORM {p}.lock_acquisition_authority(artifact.assessment_id,artifact.assessment_revision,artifact.collection_id);
            IF NOT {p}.private_operation_allowed(artifact.assessment_id,artifact.assessment_revision,artifact.collection_id,
                requested_purpose,artifact.artifact_hash,operation) OR artifact.purpose IS DISTINCT FROM requested_purpose THEN RETURN NULL; END IF;
            SELECT private_bytes INTO bytes FROM {s}.staged_artifact WHERE id=artifact_id;
            RETURN bytes;
        END $$;
        CREATE OR REPLACE FUNCTION {p}.erase_staged(artifact_id uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE artifact {s}.staged_artifact; evidence uuid; operator_id uuid;
        BEGIN
            operator_id:={p}.current_actor();
            IF operator_id IS NULL THEN RAISE EXCEPTION 'Acquisition subject required' USING ERRCODE='42501'; END IF;
            PERFORM 1 FROM {p}.actor a WHERE a.id=operator_id FOR SHARE;
            SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                acquired_at,retention_deadline,state,purpose,acquired_by INTO artifact
                FROM {s}.staged_artifact WHERE id=artifact_id FOR UPDATE;
            IF artifact.id IS NULL OR NOT {p}.assigned(artifact.collection_id,'acquire') THEN
                RAISE EXCEPTION 'Acquisition assignment required' USING ERRCODE='42501'; END IF;
            IF artifact.state='erased' THEN RETURN true; END IF;
            SELECT evidence_id INTO evidence FROM {p}.acquisition_assessment
                WHERE id=artifact.assessment_id AND revision=artifact.assessment_revision;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(operator_id,artifact.collection_id,evidence,'staging_erasure',artifact_id::text,1,'Scoped staging erasure');
            UPDATE {s}.staged_artifact SET state='erased',private_bytes=NULL WHERE id=artifact_id;
            RETURN true;
        END $$;
    """)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    _, p, s, prefix, quote = names()
    conn = op.get_bind()
    if conn.exec_driver_sql(f"""SELECT EXISTS(SELECT 1 FROM {p}.synthetic_staging_limit)
        OR EXISTS(SELECT 1 FROM {p}.acquisition_assessment WHERE artifact_hash IS NOT NULL)
        OR EXISTS(SELECT 1 FROM {s}.staged_artifact WHERE purpose IS NOT NULL)
        OR EXISTS(SELECT 1 FROM {p}.command_receipt WHERE action IN
            ('record_controller','record_privacy_review','record_acquisition_assessment'))""").scalar_one():
        raise RuntimeError("Acquisition workflow requires forward repair")
    if conn.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)", (prefix + "_guard",)
    ).scalar_one():
        guard = quote(prefix + "_guard")
        op.execute(
            f"REVOKE INSERT ON {p}.controller_record,{p}.privacy_review,{p}.acquisition_assessment,{s}.staged_artifact FROM {guard}"
        )
    op.execute(f"""
        ALTER POLICY staged_read ON {s}.staged_artifact USING (current_user='{prefix}_guard' OR
            (retention_deadline>statement_timestamp() AND state IN ('staged','processing','validated')
                AND {p}.staging_allowed(assessment_id,assessment_revision,collection_id,artifact_hash)));
        DROP TRIGGER synthetic_staging ON {s}.staged_artifact;
        DROP FUNCTION {p}.validate_synthetic_staging();
        DROP FUNCTION {p}.stage_synthetic_artifact(uuid,text,uuid,bigint,text,bytea,timestamptz);
        DROP FUNCTION {p}.read_synthetic_artifact(uuid,text,text);
        DROP FUNCTION {p}.private_operation_allowed(uuid,bigint,text,text,text,text);
        DROP FUNCTION {p}.is_native_operator();
        DROP FUNCTION {p}.lock_acquisition_authority(uuid,bigint,text);
        DROP FUNCTION {p}.apply_acquisition_command(jsonb);
        DROP TABLE {p}.synthetic_staging_limit;
        ALTER TABLE {s}.staged_artifact DROP CONSTRAINT qualified_staging_pair,
            DROP COLUMN purpose,DROP COLUMN acquired_by;
        ALTER TABLE {p}.acquisition_assessment DROP COLUMN artifact_hash;
        ALTER TABLE {p}.command_receipt DROP CONSTRAINT command_receipt_action_check,
            ADD CONSTRAINT command_receipt_action_check CHECK (action IN
                ('record_evidence','record_collection_decision','record_verification_review'));
        CREATE OR REPLACE FUNCTION {p}.erase_staged(artifact_id uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE artifact {s}.staged_artifact; evidence uuid;
        BEGIN
            SELECT * INTO artifact FROM {s}.staged_artifact WHERE id=artifact_id FOR UPDATE;
            IF artifact.id IS NULL OR NOT {p}.assigned(artifact.collection_id,'acquire') THEN
                RAISE EXCEPTION 'Acquisition assignment required' USING ERRCODE='42501'; END IF;
            IF artifact.state='erased' THEN RETURN true; END IF;
            SELECT evidence_id INTO evidence FROM {p}.acquisition_assessment
                WHERE id=artifact.assessment_id AND revision=artifact.assessment_revision;
            UPDATE {s}.staged_artifact SET state='erased',private_bytes=NULL WHERE id=artifact_id;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES({p}.current_actor(),artifact.collection_id,evidence,'staging_erasure',artifact_id::text,1,'Scoped staging erasure');
            RETURN true;
        END $$;
    """)
