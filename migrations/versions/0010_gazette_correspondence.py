"""Private identified gazette items project existing issue-owned notice bytes."""

from hashlib import sha256

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def names():
    conn = op.get_bind()
    base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
    quote = conn.dialect.identifier_preparer.quote_identifier
    return (
        quote(base),
        *(
            quote(f"{base}_{kind}" if base.startswith("test_") else kind)
            for kind in ("policy", "staging", "corpus")
        ),
        "oa_" + sha256(base.encode()).hexdigest()[:12],
        quote,
    )


def patched():
    return (
        "approved_version_current(text,text)",
        "read_approved_synthetic_version(text,text)",
        "hold_stale_approval(uuid)",
        "bound_identity_immutable()",
        "apply_synthetic_approval(jsonb)",
        "validate_approval_commit()",
        "staging_live_bytes(text)",
        "reserve_assembly_capacity(uuid,text,bigint,integer)",
        "transition_assembly(uuid,text,text)",
        "apply_assembly_command(jsonb)",
    )


def functions():
    return (
        "gazette_binding_hash(uuid)",
        "gazette_identity_current(uuid)",
        "validate_gazette_binding()",
        "gazette_item_policy_current(text,bigint,bigint)",
        "gazette_correspondence_current(uuid,text)",
        "gazette_dependencies_current(text,text)",
        "lock_gazette_dependencies(text)",
        "gazette_inputs(uuid,text,text,text,boolean)",
        "apply_gazette_command(jsonb)",
        "read_gazette_projection(uuid,text)",
        "validate_gazette_commit()",
        "hold_stale_gazette(uuid)",
        "validate_gazette_correspondence()",
    )


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    b, p, s, c, prefix, quote = names()
    guard_oid = conn.exec_driver_sql(
        "SELECT oid FROM pg_roles WHERE rolname=%s", (prefix + "_guard",)
    ).scalar_one_or_none()
    if guard_oid is not None:
        for kind in ("guard", "serving", "acquisition", "review", "release"):
            role = conn.exec_driver_sql(
                "SELECT oid,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=%s",
                (prefix + "_" + kind,),
            ).one_or_none()
            if (
                role is None
                or any(role[1:])
                or (
                    kind != "guard"
                    and conn.exec_driver_sql(
                        "SELECT pg_has_role(%s,%s,'MEMBER')", (role[0], guard_oid)
                    ).scalar_one()
                )
            ):
                raise RuntimeError("Existing capability roles require boundary review")
        original_acl = conn.exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM pg_proc f CROSS JOIN LATERAL aclexplode(coalesce(f.proacl,acldefault('f',f.proowner))) a WHERE f.oid=to_regprocedure(%s) AND a.privilege_type='EXECUTE' AND a.grantee<>f.proowner AND (a.grantee<>(SELECT oid FROM pg_roles WHERE rolname=%s) OR a.is_grantable))",
            (p + ".read_approved_synthetic_version(text,text)", prefix + "_acquisition"),
        ).scalar_one()
        if original_acl:
            raise RuntimeError("Existing private reader grants require boundary review")
        for fn in patched():
            if (
                conn.exec_driver_sql(
                    "SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(%s)", (p + "." + fn,)
                ).scalar_one_or_none()
                != guard_oid
            ):
                raise RuntimeError("Existing guard ownership requires boundary review")
        if conn.exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema' AND has_schema_privilege(%s,oid,'CREATE')) OR EXISTS(SELECT 1 FROM pg_class WHERE relowner=%s AND relkind IN ('r','p'))",
            (guard_oid, guard_oid),
        ).scalar_one():
            raise RuntimeError("Existing guard ownership requires boundary review")
    op.execute(f"""
        CREATE TABLE {s}.gazette_item_binding (
            id uuid PRIMARY KEY,item_work_id text NOT NULL,item_expression_id text NOT NULL,item_collection_id text NOT NULL,
            issue_version_id text NOT NULL,issue_expression_id text NOT NULL,issue_collection_id text NOT NULL,artifact_id uuid NOT NULL,
            manifestation_id text NOT NULL,notice_node_id text NOT NULL,anchor_id text NOT NULL REFERENCES {c}.approved_anchor(id),
            identity_hash text NOT NULL,evidence_id uuid NOT NULL REFERENCES {p}.evidence(id),reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 2048),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),payload jsonb,
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            UNIQUE(issue_version_id,notice_node_id),UNIQUE(item_expression_id,issue_version_id),
            FOREIGN KEY(item_expression_id,item_work_id) REFERENCES {b}.identity_expressions(id,work_id),
            FOREIGN KEY(item_work_id,item_collection_id) REFERENCES {b}.identity_works(id,collection_id),
            FOREIGN KEY(item_work_id,manifestation_id) REFERENCES {b}.identity_work_manifestations(work_id,manifestation_id),
            FOREIGN KEY(issue_version_id,artifact_id,issue_expression_id) REFERENCES {c}.approved_version(id,artifact_id,expression_id),
            FOREIGN KEY(notice_node_id,issue_version_id) REFERENCES {c}.version_node(id,version_id),
            CHECK(item_collection_id<>issue_collection_id),CHECK(item_expression_id<>issue_expression_id),
            CHECK((state='erased')=(payload IS NULL)),CHECK(state='erased' OR octet_length(payload::text)<=8192)
        );
        CREATE INDEX gazette_binding_artifact ON {s}.gazette_item_binding(artifact_id);
        CREATE TABLE {s}.gazette_item_review (
            id uuid PRIMARY KEY,command_id uuid NOT NULL UNIQUE,binding_id uuid NOT NULL REFERENCES {s}.gazette_item_binding(id),
            artifact_id uuid NOT NULL REFERENCES {s}.staged_artifact(id),issue_collection_id text NOT NULL,item_collection_id text NOT NULL,
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),review_hash text NOT NULL,
            representation_id text NOT NULL REFERENCES {c}.representation_revision(id),record_set_hash text NOT NULL,
            item_rights_revision bigint NOT NULL,item_verification_revision bigint NOT NULL,expected_revision bigint NOT NULL CHECK(expected_revision BETWEEN 0 AND 31),
            payload jsonb,state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            FOREIGN KEY(item_collection_id,item_rights_revision) REFERENCES {p}.collection_decision(collection_id,revision),
            FOREIGN KEY(item_collection_id,item_verification_revision) REFERENCES {p}.verification_policy(collection_id,revision),
            CHECK((state='erased')=(payload IS NULL)),CHECK(state='erased' OR coalesce(octet_length(payload::text)<=65536 AND encode(sha256(convert_to(payload::text,'UTF8')),'hex')=review_hash,false))
        );
        CREATE TABLE {c}.gazette_correspondence (
            id uuid PRIMARY KEY,binding_id uuid NOT NULL REFERENCES {s}.gazette_item_binding(id),
            artifact_id uuid NOT NULL REFERENCES {s}.staged_artifact(id),review_id uuid NOT NULL REFERENCES {s}.gazette_item_review(id),
            issue_collection_id text NOT NULL,item_collection_id text NOT NULL,revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 32),
            parent_id uuid,representation_id text NOT NULL REFERENCES {c}.representation_revision(id),
            start_byte bigint NOT NULL,end_byte bigint NOT NULL,span_hash text NOT NULL,payload jsonb,payload_hash text NOT NULL,
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            UNIQUE(binding_id,revision),UNIQUE(id,binding_id),
            FOREIGN KEY(parent_id,binding_id) REFERENCES {c}.gazette_correspondence(id,binding_id),
            CHECK((revision=1)=(parent_id IS NULL)),CHECK(start_byte>=0 AND end_byte>start_byte AND end_byte<=131072),
            CHECK((state='erased')=(payload IS NULL)),CHECK(state='erased' OR coalesce(octet_length(payload::text)<=8192 AND encode(sha256(convert_to(payload::text,'UTF8')),'hex')=payload_hash,false))
        );
        CREATE TABLE {c}.gazette_correspondence_head (
            binding_id uuid PRIMARY KEY REFERENCES {s}.gazette_item_binding(id),correspondence_id uuid NOT NULL,revision bigint NOT NULL,
            FOREIGN KEY(correspondence_id,binding_id) REFERENCES {c}.gazette_correspondence(id,binding_id)
        );
        CREATE TABLE {p}.gazette_receipt (
            command_id uuid PRIMARY KEY,actor_id uuid NOT NULL REFERENCES {p}.actor(id),fingerprint text NOT NULL,
            binding_id uuid NOT NULL REFERENCES {s}.gazette_item_binding(id),result jsonb NOT NULL
        );
    """)
    # Preserve authority functions first so new predicates cannot recursively consult themselves.
    for fn in patched():
        base = fn.split("(")[0]
        op.execute(f"ALTER FUNCTION {p}.{fn} RENAME TO {base}_m33")
    install_functions(b, p, s, c, prefix)
    extend(p, s, c)
    op.execute(
        f"DROP TRIGGER approval_commit_recheck ON {p}.approval_receipt; CREATE CONSTRAINT TRIGGER approval_commit_recheck AFTER INSERT ON {p}.approval_receipt DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {p}.validate_approval_commit()"
    )
    for table in ("identity_works", "identity_expressions", "identity_manifestations"):
        op.execute(
            f"DROP TRIGGER bound_identity ON {b}.{table}; CREATE TRIGGER bound_identity BEFORE UPDATE OR DELETE ON {b}.{table} FOR EACH ROW EXECUTE FUNCTION {p}.bound_identity_immutable()"
        )
    for schema, table in (
        (s, "gazette_item_binding"),
        (s, "gazette_item_review"),
        (c, "gazette_correspondence"),
        (c, "gazette_correspondence_head"),
        (p, "gazette_receipt"),
    ):
        op.execute(
            f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY; CREATE POLICY private_guard ON {schema}.{table} USING(current_user='{prefix}_guard'); REVOKE ALL ON {schema}.{table} FROM PUBLIC"
        )
    for fn in functions() + patched():
        op.execute(f"REVOKE ALL ON FUNCTION {p}.{fn} FROM PUBLIC")
    if guard_oid is not None:
        guard = quote(prefix + "_guard")
        op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {s},{c},{p} TO {guard}")
        op.execute(
            f"GRANT UPDATE(state,payload) ON {s}.gazette_item_binding TO {guard}; GRANT INSERT,UPDATE(state,payload) ON {s}.gazette_item_review,{c}.gazette_correspondence TO {guard}; GRANT INSERT,UPDATE ON {c}.gazette_correspondence_head TO {guard}; GRANT INSERT ON {p}.gazette_receipt TO {guard}"
        )
        op.execute(f"GRANT CREATE ON SCHEMA {p} TO {guard}")
        for fn in functions() + patched():
            op.execute(f"ALTER FUNCTION {p}.{fn} OWNER TO {guard}")
        op.execute(f"REVOKE CREATE ON SCHEMA {p} FROM {guard}")
        op.execute(
            f"REVOKE EXECUTE ON FUNCTION {p}.read_approved_synthetic_version_m33(text,text) FROM {quote(prefix + '_acquisition')}"
        )
        for fn, roles in (
            ("apply_gazette_command(jsonb)", ("review", "release")),
            ("read_gazette_projection(uuid,text)", ("acquisition",)),
            ("read_approved_synthetic_version(text,text)", ("acquisition",)),
            ("apply_assembly_command(jsonb)", ("acquisition",)),
            ("apply_synthetic_approval(jsonb)", ("review", "release")),
        ):
            op.execute(
                f"GRANT EXECUTE ON FUNCTION {p}.{fn} TO {','.join(quote(prefix + '_' + kind) for kind in roles)}"
            )


def install_functions(b, p, s, c, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.gazette_binding_hash(bid uuid) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT encode(sha256(convert_to((to_jsonb(g)-ARRAY['payload','state'])::text,'UTF8')),'hex') FROM {s}.gazette_item_binding g WHERE id=bid
        $$;
        CREATE FUNCTION {p}.gazette_identity_current(bid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {s}.gazette_item_binding g JOIN {b}.identity_expressions x ON x.id=g.item_expression_id
                JOIN {b}.identity_works w ON w.id=x.work_id WHERE g.id=bid AND g.state='active' AND w.identity_status='identified'
                AND g.identity_hash=encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(x))::text,'UTF8')),'hex'))
        $$;
        CREATE FUNCTION {p}.validate_gazette_binding() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
        DECLARE v {c}.approved_version;source_binding {s}.synthetic_expression_binding;n {c}.version_node;x record;
        BEGIN
            PERFORM {b}.identity_check_legacy_authority(true);
            SELECT * INTO v FROM {c}.approved_version WHERE id=NEW.issue_version_id;
            PERFORM 1 FROM {s}.staged_artifact WHERE id=v.artifact_id FOR UPDATE;
            SELECT * INTO source_binding FROM {s}.synthetic_expression_binding WHERE id=v.binding_id FOR SHARE;
            IF v.id IS NULL OR v.state<>'active' OR NOT {p}.binding_current(source_binding.id)
                OR NOT {p}.approval_policy_current(v.collection_id,v.rights_revision,v.verification_revision)
                OR NOT EXISTS(SELECT 1 FROM {c}.representation_head h JOIN {c}.representation_revision r ON r.id=h.rep_id WHERE h.version_id=v.id AND r.state='active')
                OR NOT EXISTS(SELECT 1 FROM {s}.staged_artifact a JOIN {p}.acquisition_assessment d ON d.id=a.assessment_id AND d.revision=a.assessment_revision
                    WHERE a.id=v.artifact_id AND a.state IN ('staged','processing','validated') AND a.retention_deadline>statement_timestamp() AND d.derive='allow' AND d.retain='allow'
                    AND {p}.acquisition_current(a.assessment_id,a.assessment_revision,a.collection_id,a.purpose,a.artifact_hash))
                OR NOT EXISTS(SELECT 1 FROM {b}.identity_works WHERE id=source_binding.work_id AND document_class='gazette')
                THEN RAISE EXCEPTION 'Current identified synthetic gazette issue required' USING ERRCODE='23514'; END IF;
            SELECT d.* INTO n FROM {c}.version_node d WHERE d.id=NEW.notice_node_id AND d.version_id=v.id;
            IF n.id IS NULL OR n.node_kind<>'notice' OR n.parent_id IS NOT NULL OR n.start_byte IS NULL THEN RAISE EXCEPTION 'Complete issue-owned root notice required' USING ERRCODE='23514'; END IF;
            PERFORM 1 FROM {b}.identity_expressions e JOIN {b}.identity_works w ON w.id=e.work_id WHERE e.id=NEW.item_expression_id FOR SHARE OF e,w;
            SELECT e.work_id,w.collection_id,e.language_id,e.edition_kind,w.document_class,w.identity_status,w.jurisdiction_id,
                encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(e))::text,'UTF8')),'hex') AS digest INTO x
                FROM {b}.identity_expressions e JOIN {b}.identity_works w ON w.id=e.work_id WHERE e.id=NEW.item_expression_id;
            IF x.work_id IS NULL OR x.identity_status<>'identified' OR x.document_class IN ('gazette','judgment','other') OR x.edition_kind<>'original'
                OR x.language_id IS DISTINCT FROM (SELECT language_id FROM {b}.identity_expressions WHERE id=v.expression_id)
                OR x.jurisdiction_id IS DISTINCT FROM (SELECT jurisdiction_id FROM {b}.identity_works WHERE id=source_binding.work_id)
                OR x.collection_id=v.collection_id OR NOT EXISTS(SELECT 1 FROM {p}.evidence WHERE id=NEW.evidence_id AND collection_id=x.collection_id)
                OR NOT EXISTS(SELECT 1 FROM {b}.identity_work_manifestations WHERE work_id=x.work_id AND manifestation_id=source_binding.manifestation_id)
                OR (SELECT count(*) FROM {s}.gazette_item_binding WHERE issue_version_id=v.id)>=50 OR NEW.state<>'active'
                THEN RAISE EXCEPTION 'Explicit independently scoped item identity required' USING ERRCODE='23514'; END IF;
            NEW.item_work_id:=x.work_id;NEW.item_collection_id:=x.collection_id;NEW.identity_hash:=x.digest;
            NEW.issue_expression_id:=v.expression_id;NEW.issue_collection_id:=v.collection_id;NEW.artifact_id:=v.artifact_id;NEW.manifestation_id:=source_binding.manifestation_id;
            SELECT id INTO NEW.anchor_id FROM {c}.approved_anchor WHERE version_id=v.id AND start_byte=n.start_byte AND end_byte=n.end_byte AND profile_hash=v.profile_hash;
            IF NEW.anchor_id IS NULL THEN RAISE EXCEPTION 'Persisted issue-owned notice anchor required' USING ERRCODE='23514'; END IF;
            NEW.payload:=jsonb_build_object('item_expression_id',NEW.item_expression_id,'item_work_id',NEW.item_work_id,'identity_hash',NEW.identity_hash,
                'notice_node_id',NEW.notice_node_id,'issue_version_id',v.id,'anchor_id',NEW.anchor_id);
            PERFORM {p}.reserve_assembly_capacity(v.artifact_id,v.collection_id,octet_length(NEW.payload::text),1);
            RETURN NEW;
        END $$;
        CREATE TRIGGER declare_binding BEFORE INSERT ON {s}.gazette_item_binding FOR EACH ROW EXECUTE FUNCTION {p}.validate_gazette_binding();
        CREATE FUNCTION {p}.gazette_item_policy_current(cid text,rights_rev bigint,verification_rev bigint) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT {p}.approval_policy_current(cid,rights_rev,verification_rev) AND EXISTS(SELECT 1 FROM {p}.collection_decision WHERE collection_id=cid AND revision=rights_rev AND retain='allow')
        $$;
        CREATE FUNCTION {p}.gazette_correspondence_current(gid uuid,purpose_value text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {c}.gazette_correspondence g JOIN {c}.gazette_correspondence_head h ON h.correspondence_id=g.id AND h.binding_id=g.binding_id AND h.revision=g.revision
                JOIN {s}.gazette_item_binding b ON b.id=g.binding_id JOIN {s}.gazette_item_review r ON r.id=g.review_id
                JOIN {c}.representation_head rh ON rh.version_id=b.issue_version_id AND rh.rep_id=g.representation_id
                JOIN {c}.representation_revision rep ON rep.id=rh.rep_id AND rep.record_set_hash=r.record_set_hash
                WHERE g.id=gid AND g.state='active' AND b.state='active' AND r.state='active' AND rep.state='active'
                AND {p}.gazette_identity_current(b.id) AND {p}.approved_version_current_m33(b.issue_version_id,purpose_value)
                AND {p}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision))
        $$;
        CREATE FUNCTION {p}.gazette_dependencies_current(vid text,purpose_value text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT NOT EXISTS(SELECT 1 FROM {s}.gazette_item_binding b LEFT JOIN {c}.gazette_correspondence_head h ON h.binding_id=b.id
                WHERE b.issue_version_id=vid AND (h.correspondence_id IS NULL OR NOT {p}.gazette_correspondence_current(h.correspondence_id,purpose_value)))
        $$;
        CREATE FUNCTION {p}.approved_version_current(vid text,purpose_value text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT {p}.approved_version_current_m33(vid,purpose_value) AND {p}.gazette_dependencies_current(vid,purpose_value)
        $$;
        CREATE FUNCTION {p}.lock_gazette_dependencies(vid text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE item record;rights_rev bigint;verification_rev bigint;
        BEGIN
            FOR item IN SELECT b.id,b.item_collection_id FROM {s}.gazette_item_binding b WHERE b.issue_version_id=vid ORDER BY b.item_collection_id,b.id FOR SHARE LOOP
                IF NOT {p}.assigned(item.item_collection_id,'acquire') THEN RAISE EXCEPTION 'Independent item acquisition assignment required' USING ERRCODE='42501'; END IF;
                PERFORM 1 FROM {c}.gazette_correspondence_head WHERE binding_id=item.id FOR SHARE;
                SELECT r.item_rights_revision,r.item_verification_revision INTO rights_rev,verification_rev FROM {s}.gazette_item_review r
                    JOIN {c}.gazette_correspondence g ON g.review_id=r.id JOIN {c}.gazette_correspondence_head h ON h.correspondence_id=g.id WHERE h.binding_id=item.id;
                PERFORM {p}.lock_synthetic_approval_policy(item.item_collection_id,rights_rev,verification_rev);
            END LOOP;
        END $$;
        CREATE FUNCTION {p}.gazette_inputs(bid uuid,issue_cid text,item_cid text,purpose_value text,write boolean) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE binding {s}.gazette_item_binding;v record;n {c}.version_node;rep record;
        BEGIN
            SELECT id,artifact_id,issue_version_id,issue_collection_id,item_collection_id INTO binding.id,binding.artifact_id,binding.issue_version_id,binding.issue_collection_id,binding.item_collection_id FROM {s}.gazette_item_binding WHERE id=bid;
            IF binding.id IS NULL OR binding.issue_collection_id IS DISTINCT FROM issue_cid OR binding.item_collection_id IS DISTINCT FROM item_cid
                OR NOT {p}.assigned(item_cid,'acquire') THEN RAISE EXCEPTION 'Exact scoped item binding required' USING ERRCODE='42501'; END IF;
            PERFORM {p}.lock_assembly_input(binding.artifact_id,issue_cid,purpose_value,write);
            SELECT * INTO binding FROM {s}.gazette_item_binding WHERE id=bid FOR SHARE;
            PERFORM 1 FROM {c}.representation_head WHERE version_id=binding.issue_version_id FOR SHARE;
            IF NOT {p}.gazette_identity_current(bid) OR NOT {p}.approved_version_current_m33(binding.issue_version_id,purpose_value) THEN RAISE EXCEPTION 'Current issue and identified item required' USING ERRCODE='42501'; END IF;
            SELECT id,expression_id,artifact_id,collection_id,rights_revision,verification_revision,content_hash,profile_hash,snapshot_hash,serialization_profile_id,state INTO v FROM {c}.approved_version WHERE id=binding.issue_version_id;
            PERFORM {p}.lock_synthetic_approval_policy(issue_cid,v.rights_revision,v.verification_revision);
            SELECT * INTO n FROM {c}.version_node WHERE id=binding.notice_node_id;
            SELECT r.id,r.version_id,r.revision,r.record_set_hash,r.state INTO rep FROM {c}.representation_revision r JOIN {c}.representation_head h ON h.rep_id=r.id WHERE h.version_id=v.id;
            RETURN jsonb_build_object('binding',to_jsonb(binding)-'payload','binding_hash',{p}.gazette_binding_hash(bid),'version',to_jsonb(v)-ARRAY['payload','canonical_bytes'],
                'notice',to_jsonb(n),'representation',to_jsonb(rep)-'payload');
        END $$;
    """)
    install_commands(p, s, c, prefix)
    install_lifecycle(p, s, c)


def install_commands(p, s, c, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.apply_gazette_command(command jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE operator_id uuid;cmd uuid;bid uuid;issue_cid text;item_cid text;inputs jsonb;fingerprint text;previous {p}.gazette_receipt;
            r {s}.gazette_item_review;h {c}.gazette_correspondence_head;range_value jsonb;payload jsonb;result jsonb;rev bigint;gid uuid;digest text;
        BEGIN
            operator_id:={p}.current_actor();PERFORM 1 FROM {p}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {p}.is_native_operator() THEN RAISE EXCEPTION 'Native scoped session required' USING ERRCODE='42501'; END IF;
            IF command IS NULL OR octet_length(command::text)>65536 OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                OR length(btrim(command->>'reason')) NOT BETWEEN 1 AND 2048 OR jsonb_typeof(command->'purpose') IS DISTINCT FROM 'string'
                OR length(btrim(command->>'purpose')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Bounded explicit gazette command required' USING ERRCODE='22023'; END IF;
            cmd:=(command->>'command_id')::uuid;bid:=(command->>'binding_id')::uuid;issue_cid:=command->>'issue_collection_id';item_cid:=command->>'item_collection_id';
            IF command->>'action'='record_gazette_item_review' THEN
                IF NOT {p}.operator_keys(command,ARRAY['command_id','issue_collection_id','item_collection_id','binding_id','purpose','reason','action','review_id','binding_hash','issue_version_id','representation_id','record_set_hash','content_hash','profile_hash','ranges','item_rights_revision','item_verification_revision','expected_revision','evidence_id','identification_method','review_scope','requires_exception_review'],ARRAY[]::text[])
                    OR command->>'identification_method' IS DISTINCT FROM 'human_source_identification' OR command->>'review_scope' IS DISTINCT FROM 'collection_policy'
                    OR command->'requires_exception_review' IS DISTINCT FROM 'false'::jsonb THEN RAISE EXCEPTION 'Explicit human item identification required' USING ERRCODE='22023'; END IF;
                IF NOT {p}.assigned(item_cid,'content') THEN RAISE EXCEPTION 'Current item content reviewer required' USING ERRCODE='42501'; END IF;
            ELSIF command->>'action'='approve_gazette_item_correspondence' THEN
                IF NOT {p}.operator_keys(command,ARRAY['command_id','issue_collection_id','item_collection_id','binding_id','purpose','reason','action','review_id','review_hash','correspondence_id','expected_revision'],ARRAY[]::text[]) THEN RAISE EXCEPTION 'Invalid correspondence approval' USING ERRCODE='22023'; END IF;
                IF NOT {p}.assigned(item_cid,'release') OR NOT {p}.assigned(issue_cid,'release') THEN RAISE EXCEPTION 'Independent scoped release authority required' USING ERRCODE='42501'; END IF;
            ELSE RAISE EXCEPTION 'Unsupported gazette command' USING ERRCODE='22023'; END IF;
            inputs:={p}.gazette_inputs(bid,issue_cid,item_cid,command->>'purpose',true);
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:gazette-binding:'||bid::text,0));
            SELECT * INTO h FROM {c}.gazette_correspondence_head WHERE binding_id=bid FOR UPDATE;
            IF jsonb_typeof(command->'expected_revision') IS DISTINCT FROM 'number' OR command->>'expected_revision' !~ '^[0-9]{{1,2}}$'
                OR (command->>'expected_revision')::bigint NOT BETWEEN 0 AND 31 THEN RAISE EXCEPTION 'Bounded exact revision required' USING ERRCODE='22023'; END IF;
            IF command->>'action'='record_gazette_item_review' THEN
                IF inputs->>'binding_hash' IS DISTINCT FROM command->>'binding_hash' OR inputs->'version'->>'id' IS DISTINCT FROM command->>'issue_version_id'
                    OR inputs->'version'->>'content_hash' IS DISTINCT FROM command->>'content_hash' OR inputs->'version'->>'profile_hash' IS DISTINCT FROM command->>'profile_hash'
                    OR inputs->'representation'->>'id' IS DISTINCT FROM command->>'representation_id' OR inputs->'representation'->>'record_set_hash' IS DISTINCT FROM command->>'record_set_hash'
                    THEN RAISE EXCEPTION 'Exact identity/version/representation/profile required' USING ERRCODE='42501'; END IF;
                PERFORM {p}.lock_synthetic_approval_policy(item_cid,(command->>'item_rights_revision')::bigint,(command->>'item_verification_revision')::bigint);
                IF NOT {p}.gazette_item_policy_current(item_cid,(command->>'item_rights_revision')::bigint,(command->>'item_verification_revision')::bigint)
                    OR operator_id IS DISTINCT FROM (SELECT actor_id FROM {p}.verification_policy WHERE collection_id=item_cid AND revision=(command->>'item_verification_revision')::bigint)
                    OR NOT EXISTS(SELECT 1 FROM {p}.evidence WHERE id=(command->>'evidence_id')::uuid AND collection_id=item_cid) THEN RAISE EXCEPTION 'Exact current independent item reviews/evidence required' USING ERRCODE='42501'; END IF;
                IF jsonb_typeof(command->'ranges') IS DISTINCT FROM 'array' OR jsonb_array_length(command->'ranges')<>1 THEN RAISE EXCEPTION 'One complete notice range required' USING ERRCODE='22023'; END IF;
                range_value:=command->'ranges'->0;
                IF NOT {p}.operator_keys(range_value,ARRAY['start_byte','end_byte','span_hash'],ARRAY[]::text[])
                    OR jsonb_typeof(range_value->'start_byte') IS DISTINCT FROM 'number' OR jsonb_typeof(range_value->'end_byte') IS DISTINCT FROM 'number'
                    OR range_value->>'start_byte' IS DISTINCT FROM inputs->'notice'->>'start_byte' OR range_value->>'end_byte' IS DISTINCT FROM inputs->'notice'->>'end_byte'
                    OR range_value->>'span_hash' IS DISTINCT FROM inputs->'notice'->>'span_hash' THEN RAISE EXCEPTION 'Exact complete issue-owned notice interval required' USING ERRCODE='23514'; END IF;
            ELSE
                SELECT * INTO r FROM {s}.gazette_item_review WHERE id=(command->>'review_id')::uuid FOR SHARE;
                IF r.id IS NULL OR r.binding_id<>bid OR r.state<>'active' OR r.review_hash IS DISTINCT FROM command->>'review_hash'
                    OR r.representation_id IS DISTINCT FROM inputs->'representation'->>'id' OR r.record_set_hash IS DISTINCT FROM inputs->'representation'->>'record_set_hash'
                    OR r.expected_revision<>(command->>'expected_revision')::bigint OR r.payload->>'binding_hash' IS DISTINCT FROM inputs->>'binding_hash'
                    OR r.actor_id IS DISTINCT FROM (SELECT actor_id FROM {p}.verification_policy WHERE collection_id=item_cid AND revision=r.item_verification_revision)
                    THEN RAISE EXCEPTION 'Exact current reviewed correspondence required' USING ERRCODE='42501'; END IF;
                PERFORM {p}.lock_synthetic_approval_policy(item_cid,r.item_rights_revision,r.item_verification_revision);
                IF NOT {p}.gazette_item_policy_current(item_cid,r.item_rights_revision,r.item_verification_revision) THEN RAISE EXCEPTION 'Current item retention and approval required' USING ERRCODE='42501'; END IF;
                range_value:=r.payload->'ranges'->0;
            END IF;
            fingerprint:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:gazette-command:'||cmd::text,0));
            SELECT * INTO previous FROM {p}.gazette_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>operator_id THEN RAISE EXCEPTION 'Receipt belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.fingerprint<>fingerprint THEN RAISE EXCEPTION 'Changed correspondence intent' USING ERRCODE='40001'; END IF;
                IF command->>'action'='record_gazette_item_review' THEN
                    IF NOT EXISTS(SELECT 1 FROM {s}.gazette_item_review WHERE command_id=cmd AND state='active') THEN RAISE EXCEPTION 'Held review cannot resume' USING ERRCODE='42501'; END IF;
                ELSIF NOT {p}.gazette_correspondence_current((previous.result->>'correspondence_id')::uuid,command->>'purpose') THEN RAISE EXCEPTION 'Held correspondence cannot resume' USING ERRCODE='42501'; END IF;
                RETURN previous.result||jsonb_build_object('replayed',true);
            END IF;
            IF coalesce(h.revision,0)<>(command->>'expected_revision')::bigint THEN RAISE EXCEPTION 'Stale correspondence head' USING ERRCODE='40001'; END IF;
            IF command->>'action'='record_gazette_item_review' THEN
                digest:=fingerprint;
                PERFORM {p}.reserve_assembly_capacity((inputs->'binding'->>'artifact_id')::uuid,issue_cid,octet_length(command::text),1);
                INSERT INTO {s}.gazette_item_review(id,command_id,binding_id,artifact_id,issue_collection_id,item_collection_id,actor_id,review_hash,representation_id,record_set_hash,item_rights_revision,item_verification_revision,expected_revision,payload)
                    VALUES((command->>'review_id')::uuid,cmd,bid,(inputs->'binding'->>'artifact_id')::uuid,issue_cid,item_cid,operator_id,digest,command->>'representation_id',command->>'record_set_hash',(command->>'item_rights_revision')::bigint,(command->>'item_verification_revision')::bigint,(command->>'expected_revision')::bigint,command);
                result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'review_hash',digest);
            ELSE
                rev:=coalesce(h.revision,0)+1;gid:=(command->>'correspondence_id')::uuid;
                payload:=jsonb_build_object('binding_hash',inputs->>'binding_hash','review_hash',r.review_hash,'ranges',r.payload->'ranges','profile_hash',inputs->'version'->>'profile_hash','content_hash',inputs->'version'->>'content_hash');
                PERFORM {p}.reserve_assembly_capacity((inputs->'binding'->>'artifact_id')::uuid,issue_cid,octet_length(payload::text),1);
                INSERT INTO {c}.gazette_correspondence(id,binding_id,artifact_id,review_id,issue_collection_id,item_collection_id,revision,parent_id,representation_id,start_byte,end_byte,span_hash,payload,payload_hash,actor_id)
                    VALUES(gid,bid,(inputs->'binding'->>'artifact_id')::uuid,r.id,issue_cid,item_cid,rev,h.correspondence_id,r.representation_id,(range_value->>'start_byte')::bigint,(range_value->>'end_byte')::bigint,range_value->>'span_hash',payload,encode(sha256(convert_to(payload::text,'UTF8')),'hex'),operator_id);
                INSERT INTO {c}.gazette_correspondence_head(binding_id,correspondence_id,revision) VALUES(bid,gid,rev) ON CONFLICT(binding_id) DO UPDATE SET correspondence_id=excluded.correspondence_id,revision=excluded.revision;
                result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'correspondence_id',gid,'revision',rev);
            END IF;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(operator_id,item_cid,CASE WHEN r.id IS NULL THEN (command->>'evidence_id')::uuid ELSE (r.payload->>'evidence_id')::uuid END,CASE WHEN rev IS NULL THEN 'gazette_item_identification' ELSE 'gazette_item_correspondence' END,CASE WHEN rev IS NULL THEN command->>'review_id' ELSE bid::text END,coalesce(rev,1),command->>'reason');
            INSERT INTO {p}.gazette_receipt(command_id,actor_id,fingerprint,binding_id,result) VALUES(cmd,operator_id,fingerprint,bid,result);
            RETURN result;
        END $$;
        CREATE FUNCTION {p}.read_gazette_projection(gid uuid,purpose_value text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE meta record;inputs jsonb;result jsonb;g {c}.gazette_correspondence;
        BEGIN
            SELECT b.id,b.issue_collection_id,b.item_collection_id,b.issue_version_id INTO meta FROM {c}.gazette_correspondence d JOIN {s}.gazette_item_binding b ON b.id=d.binding_id WHERE d.id=gid;
            IF meta.id IS NULL THEN RETURN NULL; END IF;
            inputs:={p}.gazette_inputs(meta.id,meta.issue_collection_id,meta.item_collection_id,purpose_value,false);
            PERFORM {p}.lock_gazette_dependencies(meta.issue_version_id);
            IF NOT {p}.approved_version_current(meta.issue_version_id,purpose_value) OR NOT {p}.gazette_correspondence_current(gid,purpose_value) THEN RETURN NULL; END IF;
            SELECT * INTO g FROM {c}.gazette_correspondence WHERE id=gid FOR SHARE;
            SELECT jsonb_build_object('correspondence_id',g.id,'revision',g.revision,'item_work_id',b.item_work_id,'item_expression_id',b.item_expression_id,
                'issue_version_id',v.id,'representation_id',g.representation_id,'notice_node_id',b.notice_node_id,'anchor_id',b.anchor_id,
                'content_hash',v.content_hash,'profile_hash',v.profile_hash,'ranges',g.payload->'ranges','projected_text',convert_from(substring(v.canonical_bytes FROM g.start_byte::integer+1 FOR (g.end_byte-g.start_byte)::integer),'UTF8'),'publication_eligible',false)
                INTO result FROM {s}.gazette_item_binding b JOIN {c}.approved_version v ON v.id=b.issue_version_id WHERE b.id=g.binding_id;
            RETURN result;
        END $$;
        CREATE FUNCTION {p}.validate_gazette_commit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE b {s}.gazette_item_binding;r {s}.gazette_item_review;purpose_value text;gid uuid;
        BEGIN
            SELECT * INTO b FROM {s}.gazette_item_binding WHERE id=NEW.binding_id;
            SELECT purpose INTO purpose_value FROM {s}.staged_artifact WHERE id=b.artifact_id;
            IF NEW.actor_id IS DISTINCT FROM {p}.current_actor() OR NOT {p}.is_native_operator() OR NOT {p}.assigned(b.item_collection_id,'acquire') OR NOT {p}.assigned(b.issue_collection_id,'acquire')
                OR NOT {p}.gazette_identity_current(b.id) OR NOT {p}.approved_version_current_m33(b.issue_version_id,purpose_value) THEN RAISE EXCEPTION 'Gazette authority expired before commit' USING ERRCODE='42501'; END IF;
            gid:=(NEW.result->>'correspondence_id')::uuid;
            IF gid IS NOT NULL THEN
                IF NOT {p}.assigned(b.item_collection_id,'release') OR NOT {p}.assigned(b.issue_collection_id,'release') OR NOT {p}.gazette_correspondence_current(gid,purpose_value) THEN RAISE EXCEPTION 'Correspondence changed before commit' USING ERRCODE='42501'; END IF;
            ELSE
                SELECT * INTO r FROM {s}.gazette_item_review WHERE command_id=NEW.command_id;
                IF r.state IS DISTINCT FROM 'active' OR NOT {p}.assigned(b.item_collection_id,'content') OR NOT {p}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision)
                    OR NOT EXISTS(SELECT 1 FROM {c}.representation_head h JOIN {c}.representation_revision rep ON rep.id=h.rep_id WHERE h.version_id=b.issue_version_id AND h.rep_id=r.representation_id AND rep.record_set_hash=r.record_set_hash) THEN RAISE EXCEPTION 'Identification changed before commit' USING ERRCODE='42501'; END IF;
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER commit_recheck AFTER INSERT ON {p}.gazette_receipt DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {p}.validate_gazette_commit();
    """)


def install_lifecycle(p, s, c):
    for schema, table in (
        (s, "gazette_item_binding"),
        (s, "gazette_item_review"),
        (c, "gazette_correspondence"),
    ):
        op.execute(
            f"CREATE TRIGGER lifecycle_update BEFORE UPDATE OR DELETE ON {schema}.{table} FOR EACH ROW EXECUTE FUNCTION {p}.approval_lifecycle(); CREATE TRIGGER no_truncate BEFORE TRUNCATE ON {schema}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record()"
        )
    op.execute(
        f"CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {p}.gazette_receipt FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record()"
    )
    op.execute(f"""
        CREATE FUNCTION {p}.validate_gazette_correspondence() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM {s}.gazette_item_binding b JOIN {s}.gazette_item_review r ON r.binding_id=b.id
                JOIN {c}.version_node n ON n.id=b.notice_node_id AND n.version_id=b.issue_version_id
                JOIN {c}.representation_revision rep ON rep.id=r.representation_id AND rep.version_id=b.issue_version_id
                WHERE b.id=NEW.binding_id AND r.id=NEW.review_id AND NEW.artifact_id=b.artifact_id AND r.artifact_id=b.artifact_id
                    AND NEW.issue_collection_id=b.issue_collection_id AND NEW.item_collection_id=b.item_collection_id
                    AND NEW.representation_id=r.representation_id AND NEW.start_byte=n.start_byte AND NEW.end_byte=n.end_byte AND NEW.span_hash=n.span_hash)
                THEN RAISE EXCEPTION 'Exact issue-owned reviewed notice correspondence required' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER exact_notice BEFORE INSERT ON {c}.gazette_correspondence FOR EACH ROW EXECUTE FUNCTION {p}.validate_gazette_correspondence();
        CREATE FUNCTION {p}.hold_stale_gazette(artifact uuid) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE meta record;targets jsonb;changed integer;next_rev bigint;audit uuid;
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision,purpose INTO meta FROM {s}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT jsonb_build_object('review',coalesce((SELECT jsonb_agg(r.id) FROM {s}.gazette_item_review r JOIN {s}.gazette_item_binding b ON b.id=r.binding_id WHERE r.artifact_id=artifact AND r.state='active' AND
                (NOT {p}.approved_version_current_m33(b.issue_version_id,meta.purpose) OR NOT {p}.gazette_identity_current(b.id) OR NOT {p}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision)
                OR NOT EXISTS(SELECT 1 FROM {c}.representation_head WHERE version_id=b.issue_version_id AND rep_id=r.representation_id))),'[]'),
                'correspondence',coalesce((SELECT jsonb_agg(g.id) FROM {c}.gazette_correspondence g JOIN {s}.gazette_item_binding b ON b.id=g.binding_id JOIN {s}.gazette_item_review r ON r.id=g.review_id WHERE g.artifact_id=artifact AND g.state='active' AND
                    (NOT {p}.approved_version_current_m33(b.issue_version_id,meta.purpose) OR NOT {p}.gazette_identity_current(b.id) OR NOT {p}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision)
                    OR NOT EXISTS(SELECT 1 FROM {c}.representation_head WHERE version_id=b.issue_version_id AND rep_id=g.representation_id))),'[]')) INTO targets;
            changed:=jsonb_array_length(targets->'review')+jsonb_array_length(targets->'correspondence');
            IF changed=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(revision),0)+1 INTO next_rev FROM {p}.assembly_lifecycle_event WHERE artifact_id=artifact;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {p}.current_actor(),meta.collection_id,evidence_id,'assembly_lifecycle',artifact::text,next_rev,'Gazette item authority no longer current' FROM {p}.acquisition_assessment WHERE id=meta.assessment_id AND revision=meta.assessment_revision RETURNING id INTO audit;
            INSERT INTO {p}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id) VALUES(artifact,next_rev,{p}.current_actor(),'held','derivation_not_current',changed,audit);
            UPDATE {s}.gazette_item_review SET state='held' WHERE targets->'review' ? id::text;
            UPDATE {c}.gazette_correspondence SET state='held' WHERE targets->'correspondence' ? id::text;
            RETURN changed;
        END $$;
    """)


def extend(p, s, c):
    conn = op.get_bind()
    for fn in patched():
        if fn.startswith("approved_version_current"):
            continue
        name = fn.split("(")[0]
        args = fn[fn.index("(") :]
        definition = conn.exec_driver_sql(
            "SELECT pg_get_functiondef(to_regprocedure(%s))", (p + "." + name + "_m33" + args,)
        ).scalar_one()
        definition = definition.replace(f".{name}_m33(", f".{name}(", 1)
        if name == "read_approved_synthetic_version":
            anchor = f"IF NOT {p}.approved_version_current(vid,purpose_value) THEN RETURN NULL; END IF;"
            replacement = f"PERFORM {p}.lock_gazette_dependencies(vid);\n            " + anchor
        elif name in {"hold_stale_approval", "apply_synthetic_approval", "validate_approval_commit"}:
            # Owner holds remain distinct from missing/withheld item correspondence.
            definition = definition.replace(
                f"{p}.approved_version_current(", f"{p}.approved_version_current_m33("
            )
            op.execute(definition)
            continue
        elif name == "bound_identity_immutable":
            anchor = "(TG_TABLE_NAME='identity_manifestations' AND manifestation_id=OLD.id)) THEN"
            replacement = (
                anchor[:-5]
                + f" OR EXISTS(SELECT 1 FROM {s}.gazette_item_binding WHERE (TG_TABLE_NAME='identity_works' AND item_work_id=OLD.id) OR (TG_TABLE_NAME='identity_expressions' AND item_expression_id=OLD.id) OR (TG_TABLE_NAME='identity_manifestations' AND manifestation_id=OLD.id)) THEN"
            )
        elif name == "staging_live_bytes":
            anchor = f"+coalesce((SELECT sum(octet_length(r.payload::text)) FROM {c}.representation_revision r JOIN {c}.approved_version v ON v.id=r.version_id WHERE v.collection_id=cid),0)"
            replacement = anchor
            for schema, table in (
                (s, "gazette_item_binding"),
                (s, "gazette_item_review"),
                (c, "gazette_correspondence"),
            ):
                replacement += f"\n                +coalesce((SELECT sum(octet_length(payload::text)) FROM {schema}.{table} WHERE issue_collection_id=cid),0)"
        elif name == "reserve_assembly_capacity":
            anchor = f"UNION ALL SELECT octet_length(payload::text) FROM {c}.representation_revision WHERE artifact_id=artifact)"
            replacement = anchor[:-1]
            for schema, table in (
                (s, "gazette_item_binding"),
                (s, "gazette_item_review"),
                (c, "gazette_correspondence"),
            ):
                replacement += f"\n                UNION ALL SELECT octet_length(payload::text) FROM {schema}.{table} WHERE artifact_id=artifact"
            replacement += ")"
        elif name == "transition_assembly":
            anchor = f"UNION ALL SELECT 1 FROM {c}.representation_revision WHERE artifact_id=artifact AND state=ANY(eligible))"
            replacement = anchor[:-1]
            for schema, table in (
                (s, "gazette_item_binding"),
                (s, "gazette_item_review"),
                (c, "gazette_correspondence"),
            ):
                replacement += f"\n                UNION ALL SELECT 1 FROM {schema}.{table} WHERE artifact_id=artifact AND state=ANY(eligible)"
            replacement += ")"
            tail = "            RETURN changed;"
            if definition.count(tail) != 1:
                raise RuntimeError("Installed gazette cleanup requires boundary review")
            updates = "".join(
                f"            UPDATE {schema}.{table} SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END WHERE artifact_id=artifact AND state=ANY(eligible);\n"
                for schema, table in (
                    (s, "gazette_item_binding"),
                    (s, "gazette_item_review"),
                    (c, "gazette_correspondence"),
                )
            )
            definition = definition.replace(tail, updates + tail, 1)
        else:
            anchor = f"held:=held+{p}.hold_stale_approval(row_item.id);"
            replacement = anchor + f"\n                    held:=held+{p}.hold_stale_gazette(row_item.id);"
            erased = f"OR EXISTS(SELECT 1 FROM {c}.representation_revision WHERE artifact_id=row_item.id AND state='erasure_required'))"
            if definition.count(erased) != 1:
                raise RuntimeError("Installed gazette materializer requires boundary review")
            added = (
                erased[:-1]
                + "".join(
                    f"\n                        OR EXISTS(SELECT 1 FROM {schema}.{table} WHERE artifact_id=row_item.id AND state='erasure_required')"
                    for schema, table in (
                        (s, "gazette_item_binding"),
                        (s, "gazette_item_review"),
                        (c, "gazette_correspondence"),
                    )
                )
                + ")"
            )
            definition = definition.replace(erased, added, 1)
        if definition.count(anchor) != 1:
            raise RuntimeError("Installed authority requires boundary review")
        op.execute(definition.replace(anchor, replacement, 1))


def downgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    b, p, s, c, prefix, quote = names()
    if conn.exec_driver_sql(
        f"SELECT EXISTS(SELECT 1 FROM {s}.gazette_item_binding) OR EXISTS(SELECT 1 FROM {p}.gazette_receipt)"
    ).scalar_one():
        raise RuntimeError("Private gazette correspondence requires forward repair")
    for schema, table in (
        (s, "gazette_item_binding"),
        (s, "gazette_item_review"),
        (c, "gazette_correspondence"),
    ):
        op.execute(
            f"DROP TRIGGER lifecycle_update ON {schema}.{table}; DROP TRIGGER no_truncate ON {schema}.{table}"
        )
    op.execute(
        f"DROP TRIGGER declare_binding ON {s}.gazette_item_binding; DROP TRIGGER commit_recheck ON {p}.gazette_receipt; DROP TRIGGER exact_notice ON {c}.gazette_correspondence"
    )
    op.execute(f"DROP TRIGGER approval_commit_recheck ON {p}.approval_receipt")
    for table in ("identity_works", "identity_expressions", "identity_manifestations"):
        op.execute(f"DROP TRIGGER bound_identity ON {b}.{table}")
    for fn in patched():
        name = fn.split("(")[0]
        args = fn[fn.index("(") :]
        op.execute(f"DROP FUNCTION {p}.{fn}; ALTER FUNCTION {p}.{name}_m33{args} RENAME TO {name}")
    for table in ("identity_works", "identity_expressions", "identity_manifestations"):
        op.execute(
            f"CREATE TRIGGER bound_identity BEFORE UPDATE OR DELETE ON {b}.{table} FOR EACH ROW EXECUTE FUNCTION {p}.bound_identity_immutable()"
        )
    op.execute(
        f"CREATE CONSTRAINT TRIGGER approval_commit_recheck AFTER INSERT ON {p}.approval_receipt DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {p}.validate_approval_commit()"
    )
    for fn in functions():
        op.execute(f"DROP FUNCTION {p}.{fn}")
    op.execute(
        f"DROP TABLE {p}.gazette_receipt; DROP TABLE {c}.gazette_correspondence_head; DROP TABLE {c}.gazette_correspondence; DROP TABLE {s}.gazette_item_review; DROP TABLE {s}.gazette_item_binding"
    )
    if conn.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)", (prefix + "_guard",)
    ).scalar_one():
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {p}.read_approved_synthetic_version(text,text) TO {quote(prefix + '_acquisition')}"
        )
