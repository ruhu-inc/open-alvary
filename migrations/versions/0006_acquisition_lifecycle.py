"""Bounded native conservative staging reconciliation; no activation or retargeting."""

from hashlib import sha256

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def names():
    conn = op.get_bind()
    base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
    quote = conn.dialect.identifier_preparer.quote_identifier
    p, s = (quote(f"{base}_{kind}" if base.startswith("test_") else kind) for kind in ("policy", "staging"))
    return p, s, "oa_" + sha256(base.encode()).hexdigest()[:12], quote


def check_roles(conn, p, prefix):
    guard_oid = conn.exec_driver_sql(
        "SELECT oid FROM pg_roles WHERE rolname=%s", (prefix + "_guard",)
    ).scalar_one_or_none()
    if guard_oid is None:
        return None
    for kind in ("guard", "serving", "review", "acquisition", "release"):
        role = conn.exec_driver_sql(
            "SELECT oid,rolcanlogin,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication FROM pg_roles WHERE rolname=%s",
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
    for signature in ("validate_staging()", "validate_synthetic_staging()", "erase_staged(uuid)"):
        if (
            conn.exec_driver_sql(
                "SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(%s)", (p + "." + signature,)
            ).scalar_one_or_none()
            != guard_oid
        ):
            raise RuntimeError("Existing guard ownership requires boundary review")
    if conn.exec_driver_sql(
        "SELECT has_schema_privilege(%s,to_regnamespace(%s)::oid,'CREATE') OR EXISTS(SELECT 1 FROM pg_class WHERE relowner=%s AND relkind IN ('r','p'))",
        (guard_oid, p, guard_oid),
    ).scalar_one():
        raise RuntimeError("Existing guard ownership requires boundary review")
    return guard_oid


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    p, s, prefix, quote = names()
    guard_oid = check_roles(conn, p, prefix)
    op.execute(f"""
        CREATE TABLE {p}.lifecycle_receipt (
            command_id uuid PRIMARY KEY,
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            collection_id text NOT NULL REFERENCES {p}.synthetic_staging_limit(collection_id),
            payload_hash text NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{{64}}$'),
            reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2048),
            result jsonb NOT NULL CHECK (jsonb_typeof(result)='object'),
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp()
        );
        CREATE TABLE {p}.staging_lifecycle_event (
            artifact_id uuid NOT NULL REFERENCES {s}.staged_artifact(id),
            revision bigint NOT NULL CHECK (revision>0),
            command_id uuid NOT NULL REFERENCES {p}.lifecycle_receipt(command_id) DEFERRABLE INITIALLY DEFERRED,
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            collection_id text NOT NULL,
            assessment_id uuid NOT NULL,
            assessment_revision bigint NOT NULL,
            from_state text NOT NULL,
            to_state text NOT NULL CHECK (to_state IN ('held_pending_reassessment','erasure_required','erased')),
            cause text NOT NULL CHECK (cause IN ('authority_not_current','privacy_rejected','retention_due','erasure_pending','due_erasure')),
            acquisition_head bigint,
            privacy_head bigint,
            controller_head bigint,
            audit_id uuid NOT NULL UNIQUE REFERENCES {p}.audit_event(id),
            transaction_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            PRIMARY KEY (artifact_id,revision),
            UNIQUE (artifact_id,to_state),
            FOREIGN KEY (assessment_id,assessment_revision,collection_id)
                REFERENCES {p}.acquisition_assessment(id,revision,collection_id),
            CHECK ((to_state='held_pending_reassessment' AND from_state IN ('staged','processing','validated'))
                OR (to_state='erasure_required' AND from_state IN ('staged','processing','validated','held_pending_reassessment'))
                OR (to_state='erased' AND from_state='erasure_required'))
        );
        CREATE INDEX staged_collection_cursor ON {s}.staged_artifact(collection_id,id);
    """)
    for table in ("lifecycle_receipt", "staging_lifecycle_event"):
        op.execute(f"""
            CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {p}.{table}
                FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
            ALTER TABLE {p}.{table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {p}.{table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY lifecycle_guard ON {p}.{table} USING (current_user='{prefix}_guard');
            REVOKE ALL ON {p}.{table} FROM PUBLIC;
        """)
    op.execute(f"""
        CREATE FUNCTION {p}.lifecycle_transition_allowed(artifact uuid, previous text, following text)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {p}.staging_lifecycle_event e WHERE e.artifact_id=artifact
                AND e.from_state=previous AND e.to_state=following AND e.actor_id={p}.current_actor()
                AND e.transaction_id=pg_current_xact_id())
        $$;
        CREATE FUNCTION {p}.record_staging_transition(artifact uuid, following text, cause_value text,
            cmd uuid, ah bigint, ph bigint, ch bigint) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE item record; evidence uuid; next_revision bigint; audit uuid; operator_id uuid;
        BEGIN
            operator_id:={p}.current_actor();
            SELECT id,collection_id,assessment_id,assessment_revision,state INTO item
                FROM {s}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT a.evidence_id INTO evidence FROM {p}.acquisition_assessment a
                WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision AND a.collection_id=item.collection_id;
            SELECT coalesce(max(e.revision),0)+1 INTO next_revision FROM {p}.staging_lifecycle_event e WHERE e.artifact_id=artifact;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(operator_id,item.collection_id,evidence,'staging_lifecycle',artifact::text,next_revision,cause_value)
                RETURNING id INTO audit;
            INSERT INTO {p}.staging_lifecycle_event(artifact_id,revision,command_id,actor_id,collection_id,
                assessment_id,assessment_revision,from_state,to_state,cause,acquisition_head,privacy_head,controller_head,audit_id)
                VALUES(artifact,next_revision,cmd,operator_id,item.collection_id,item.assessment_id,item.assessment_revision,
                    item.state,following,cause_value,ah,ph,ch,audit);
            IF following='erased' THEN
                PERFORM {p}.erase_staged(artifact);
            ELSE
                UPDATE {s}.staged_artifact SET state=following WHERE id=artifact;
            END IF;
        END $$;
        CREATE FUNCTION {p}.reconcile_staging(envelope jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE operator_id uuid; cid text; cmd uuid; cursor_id uuid; batch_limit integer; erase_due boolean;
            fingerprint text; previous {p}.lifecycle_receipt; item record; assessment {p}.acquisition_assessment;
            privacy {p}.privacy_review; ah bigint; ph bigint; ch bigint; target text; cause_value text;
            scanned integer:=0; held integer:=0; required integer:=0; erased integer:=0; last_id uuid; result jsonb;
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR NOT {p}.operator_keys(envelope,
                ARRAY['command_id','collection_id','reason'],ARRAY['after','limit','erase_due'])
                OR jsonb_typeof(envelope->'reason') IS DISTINCT FROM 'string'
                OR length(btrim(envelope->>'reason')) NOT BETWEEN 1 AND 2048
                OR (envelope ? 'limit' AND jsonb_typeof(envelope->'limit') IS DISTINCT FROM 'number')
                OR (envelope ? 'erase_due' AND jsonb_typeof(envelope->'erase_due') IS DISTINCT FROM 'boolean') THEN
                RAISE EXCEPTION 'Invalid lifecycle request' USING ERRCODE='22023'; END IF;
            cmd:=(envelope->>'command_id')::uuid; cid:=envelope->>'collection_id'; cursor_id:=(envelope->>'after')::uuid;
            batch_limit:=coalesce((envelope->>'limit')::integer,50); erase_due:=coalesce((envelope->>'erase_due')::boolean,false);
            IF cmd IS NULL OR cid IS NULL OR batch_limit NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION 'Bounded lifecycle request required' USING ERRCODE='22023'; END IF;
            operator_id:={p}.current_actor();
            PERFORM 1 FROM {p}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {p}.is_native_operator() OR NOT {p}.assigned(cid,'acquire') THEN
                RAISE EXCEPTION 'Native acquisition assignment required' USING ERRCODE='42501'; END IF;
            -- Conservative holds and erasure do not acquire or serve data: permitted while migration is fenced.
            IF NOT EXISTS(SELECT 1 FROM {p}.synthetic_staging_limit WHERE collection_id=cid) THEN
                RAISE EXCEPTION 'Explicit synthetic scope required' USING ERRCODE='42501'; END IF;
            fingerprint:=encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:lifecycle:'||cmd::text,0));
            SELECT * INTO previous FROM {p}.lifecycle_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>operator_id THEN RAISE EXCEPTION 'Request belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Lifecycle retry changed' USING ERRCODE='40001'; END IF;
                RETURN previous.result || jsonb_build_object('replayed',true);
            END IF;
            FOR item IN SELECT id,collection_id,assessment_id,assessment_revision,purpose,artifact_hash,retention_deadline,state
                FROM {s}.staged_artifact WHERE collection_id=cid AND (cursor_id IS NULL OR id>cursor_id)
                ORDER BY id LIMIT batch_limit FOR UPDATE
            LOOP
                scanned:=scanned+1; last_id:=item.id;
                IF item.state='erased' THEN CONTINUE; END IF;
                PERFORM {p}.lock_acquisition_authority(item.assessment_id,item.assessment_revision,cid);
                SELECT a.* INTO assessment FROM {p}.acquisition_assessment a
                    WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision AND a.collection_id=cid;
                SELECT v.* INTO privacy FROM {p}.privacy_review v WHERE v.id=assessment.privacy_review_id
                    AND v.revision=assessment.privacy_review_revision AND v.collection_id=cid;
                SELECT h.revision INTO ah FROM {p}.revision_head h WHERE h.kind='acquisition' AND h.scope_id=assessment.id::text;
                SELECT h.revision INTO ph FROM {p}.revision_head h WHERE h.kind='privacy' AND h.scope_id=privacy.id::text;
                SELECT h.revision INTO ch FROM {p}.revision_head h WHERE h.kind='controller' AND h.scope_id=privacy.controller_id::text;
                target:=NULL; cause_value:=NULL;
                IF item.state='erasure_required' THEN target:='erasure_required'; cause_value:='erasure_pending';
                ELSIF item.retention_deadline<=statement_timestamp() THEN target:='erasure_required'; cause_value:='retention_due';
                ELSIF EXISTS(SELECT 1 FROM {p}.privacy_review v WHERE v.id=assessment.privacy_review_id
                    AND v.collection_id=cid AND v.revision>=assessment.privacy_review_revision
                    AND v.clearance='rejected' AND v.valid_from<=statement_timestamp()) THEN
                    target:='erasure_required'; cause_value:='privacy_rejected';
                ELSIF item.state IN ('staged','processing','validated') AND NOT
                    {p}.private_operation_allowed(item.assessment_id,item.assessment_revision,cid,item.purpose,item.artifact_hash,'retain') THEN
                    target:='held_pending_reassessment'; cause_value:='authority_not_current';
                END IF;
                IF target IS NOT NULL AND target<>item.state THEN
                    PERFORM {p}.record_staging_transition(item.id,target,cause_value,cmd,ah,ph,ch);
                    IF target='held_pending_reassessment' THEN held:=held+1; ELSE required:=required+1; END IF;
                END IF;
                IF target='erasure_required' AND erase_due THEN
                    PERFORM {p}.record_staging_transition(item.id,'erased','due_erasure',cmd,ah,ph,ch);
                    erased:=erased+1;
                END IF;
            END LOOP;
            result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'scanned',scanned,
                'held',held,'erasure_required',required,'erased',erased,'next_after',CASE WHEN scanned=batch_limit THEN last_id ELSE NULL END);
            INSERT INTO {p}.lifecycle_receipt(command_id,actor_id,collection_id,payload_hash,reason,result)
                VALUES(cmd,operator_id,cid,fingerprint,envelope->>'reason',result);
            RETURN result;
        END $$;
    """)
    # Preserve the installed 0005 trigger functions as exact rollback copies. Patch only
    # a checked insertion point; immutable lineage/qualification checks still run first.
    anchors = {
        "validate_staging": "IF NEW.state='erased' THEN RETURN NEW; END IF;",
        "validate_synthetic_staging": "IF TG_OP='UPDATE' THEN",
    }
    for fn, anchor in anchors.items():
        definition = conn.exec_driver_sql(
            "SELECT pg_get_functiondef(to_regprocedure(%s))", (p + "." + fn + "()",)
        ).scalar_one()
        if definition.count(anchor) != 1:
            raise RuntimeError("Installed staging validator requires boundary review")
        addition = (
            f"\n IF {p}.lifecycle_transition_allowed(NEW.id,OLD.state,NEW.state) THEN RETURN NEW; END IF;\n"
        )
        op.execute(f"ALTER FUNCTION {p}.{fn}() RENAME TO {fn}_m22")
        op.execute(definition.replace(anchor, anchor + addition, 1))
        trigger = "synthetic_staging" if fn == "validate_synthetic_staging" else "validate_staging"
        op.execute(f"DROP TRIGGER {trigger} ON {s}.staged_artifact")
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {s}.staged_artifact FOR EACH ROW EXECUTE FUNCTION {p}.{fn}()"
        )
    functions = (
        "lifecycle_transition_allowed(uuid,text,text)",
        "record_staging_transition(uuid,text,text,uuid,bigint,bigint,bigint)",
        "reconcile_staging(jsonb)",
        "validate_staging()",
        "validate_synthetic_staging()",
    )
    for fn in functions:
        op.execute(f"REVOKE ALL ON FUNCTION {p}.{fn} FROM PUBLIC")
    if guard_oid is not None:
        guard = quote(prefix + "_guard")
        op.execute(f"GRANT SELECT,INSERT ON {p}.lifecycle_receipt,{p}.staging_lifecycle_event TO {guard}")
        op.execute(f"GRANT CREATE ON SCHEMA {p} TO {guard}")
        for fn in functions:
            op.execute(f"ALTER FUNCTION {p}.{fn} OWNER TO {guard}")
        op.execute(f"REVOKE CREATE ON SCHEMA {p} FROM {guard}")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {p}.reconcile_staging(jsonb) TO {quote(prefix + '_acquisition')}"
        )


def downgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    p, s, _, _ = names()
    if conn.exec_driver_sql(
        f"SELECT EXISTS(SELECT 1 FROM {p}.lifecycle_receipt) OR EXISTS(SELECT 1 FROM {p}.staging_lifecycle_event)"
    ).scalar_one():
        raise RuntimeError("Acquisition lifecycle requires forward repair")
    for fn, trigger in (
        ("validate_staging", "validate_staging"),
        ("validate_synthetic_staging", "synthetic_staging"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {s}.staged_artifact")
        op.execute(f"DROP FUNCTION {p}.{fn}()")
        op.execute(f"ALTER FUNCTION {p}.{fn}_m22() RENAME TO {fn}")
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {s}.staged_artifact FOR EACH ROW EXECUTE FUNCTION {p}.{fn}()"
        )
    op.execute(f"""
        DROP FUNCTION {p}.reconcile_staging(jsonb);
        DROP FUNCTION {p}.record_staging_transition(uuid,text,text,uuid,bigint,bigint,bigint);
        DROP FUNCTION {p}.lifecycle_transition_allowed(uuid,text,text);
        DROP TABLE {p}.staging_lifecycle_event;
        DROP TABLE {p}.lifecycle_receipt;
        DROP INDEX {s}.staged_collection_cursor;
    """)
