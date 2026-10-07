"""Private bounded original-synthetic candidates/snapshots and inherited lifecycle."""

from hashlib import sha256

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def names():
    conn = op.get_bind()
    base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
    quote = conn.dialect.identifier_preparer.quote_identifier
    p, s = (quote(f"{base}_{kind}" if base.startswith("test_") else kind) for kind in ("policy", "staging"))
    return quote(base), p, s, "oa_" + sha256(base.encode()).hexdigest()[:12], quote


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    b, p, s, prefix, quote = names()
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
        for fn in ("validate_synthetic_staging()", "erase_staged(uuid)", "reconcile_staging(jsonb)"):
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
        ALTER TABLE {s}.staged_artifact ADD CONSTRAINT assembly_source_lineage UNIQUE
            (id,collection_id,assessment_id,assessment_revision,artifact_hash,retention_deadline);
        CREATE TABLE {s}.synthetic_run (
            id uuid PRIMARY KEY,
            artifact_id uuid NOT NULL,
            collection_id text NOT NULL,
            assessment_id uuid NOT NULL,
            assessment_revision bigint NOT NULL,
            artifact_hash text NOT NULL,
            purpose text NOT NULL CHECK(length(btrim(purpose)) BETWEEN 1 AND 2048),
            retention_deadline timestamptz NOT NULL,
            producer text NOT NULL DEFAULT 'original_synthetic' CHECK(producer='original_synthetic'),
            profile_key text NOT NULL CHECK(profile_key ~ '^[a-zA-Z0-9:_-]{{1,64}}$'),
            profile_hash text NOT NULL CHECK(profile_hash ~ '^[a-f0-9]{{64}}$'),
            output_hash text NOT NULL CHECK(output_hash ~ '^[a-f0-9]{{64}}$'),
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            UNIQUE(id,artifact_id), UNIQUE(artifact_id,profile_hash),
            FOREIGN KEY(artifact_id,collection_id,assessment_id,assessment_revision,artifact_hash,retention_deadline)
                REFERENCES {s}.staged_artifact(id,collection_id,assessment_id,assessment_revision,artifact_hash,retention_deadline)
        );
        CREATE TABLE {s}.adapter_candidate (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL,
            artifact_id uuid NOT NULL,
            adapter_local_id text NOT NULL CHECK(adapter_local_id ~ '^[a-zA-Z0-9:_-]{{1,64}}$'),
            region_key text NOT NULL CHECK(region_key ~ '^[a-zA-Z0-9:_-]{{1,64}}$'),
            parent_local_id text,
            artifact_class text NOT NULL CHECK(artifact_class IN ('native_text','ocr_transcription')),
            block_type text NOT NULL CHECK(block_type IN ('heading','paragraph','list_item','table_cell','footnote')),
            payload jsonb,
            payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{{64}}$'),
            text_hash text NOT NULL CHECK(text_hash ~ '^[a-f0-9]{{64}}$'),
            payload_bytes bigint NOT NULL CHECK(payload_bytes BETWEEN 1 AND 65536),
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            UNIQUE(run_id,adapter_local_id), UNIQUE(id,run_id,region_key),
            FOREIGN KEY(run_id,artifact_id) REFERENCES {s}.synthetic_run(id,artifact_id),
            FOREIGN KEY(run_id,parent_local_id) REFERENCES {s}.adapter_candidate(run_id,adapter_local_id) DEFERRABLE INITIALLY DEFERRED,
            CHECK((state='erased')=(payload IS NULL))
        );
        CREATE INDEX candidate_family ON {s}.adapter_candidate(artifact_id);
        CREATE INDEX candidate_page ON {s}.adapter_candidate(run_id,id);
        CREATE TABLE {s}.staging_snapshot (
            id uuid NOT NULL,
            revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 32),
            run_id uuid NOT NULL,
            artifact_id uuid NOT NULL,
            parent_hash text CHECK(parent_hash ~ '^[a-f0-9]{{64}}$'),
            parent_revision bigint GENERATED ALWAYS AS (nullif(revision-1,0)) STORED,
            payload jsonb,
            payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{{64}}$'),
            snapshot_hash text NOT NULL CHECK(snapshot_hash ~ '^[a-f0-9]{{64}}$'),
            payload_bytes bigint NOT NULL CHECK(payload_bytes BETWEEN 1 AND 65536),
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            PRIMARY KEY(id,revision), UNIQUE(id,revision,run_id), UNIQUE(id,revision,snapshot_hash),
            FOREIGN KEY(id,parent_revision,parent_hash) REFERENCES {s}.staging_snapshot(id,revision,snapshot_hash),
            FOREIGN KEY(run_id,artifact_id) REFERENCES {s}.synthetic_run(id,artifact_id),
            CHECK((revision=1)=(parent_hash IS NULL)), CHECK((state='erased')=(payload IS NULL))
        );
        CREATE INDEX snapshot_family ON {s}.staging_snapshot(artifact_id);
        CREATE TABLE {s}.snapshot_head (
            id uuid PRIMARY KEY,
            revision bigint NOT NULL,
            run_id uuid NOT NULL,
            FOREIGN KEY(id,revision,run_id) REFERENCES {s}.staging_snapshot(id,revision,run_id) DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE {s}.snapshot_selection (
            snapshot_id uuid NOT NULL, revision bigint NOT NULL, run_id uuid NOT NULL,
            region_key text NOT NULL, candidate_id uuid NOT NULL, position integer NOT NULL CHECK(position BETWEEN 1 AND 50),
            PRIMARY KEY(snapshot_id,revision,region_key), UNIQUE(snapshot_id,revision,position),
            UNIQUE(snapshot_id,revision,candidate_id), UNIQUE(snapshot_id,revision,region_key,candidate_id),
            FOREIGN KEY(snapshot_id,revision,run_id) REFERENCES {s}.staging_snapshot(id,revision,run_id),
            FOREIGN KEY(candidate_id,run_id,region_key) REFERENCES {s}.adapter_candidate(id,run_id,region_key)
        );
        CREATE TABLE {s}.snapshot_resolution (
            snapshot_id uuid NOT NULL, revision bigint NOT NULL, run_id uuid NOT NULL,
            region_key text NOT NULL, selected_candidate_id uuid NOT NULL, rejected_candidate_id uuid NOT NULL,
            PRIMARY KEY(snapshot_id,revision,rejected_candidate_id), CHECK(selected_candidate_id<>rejected_candidate_id),
            FOREIGN KEY(snapshot_id,revision,region_key,selected_candidate_id)
                REFERENCES {s}.snapshot_selection(snapshot_id,revision,region_key,candidate_id),
            FOREIGN KEY(rejected_candidate_id,run_id,region_key) REFERENCES {s}.adapter_candidate(id,run_id,region_key)
        );
        CREATE TABLE {p}.assembly_receipt (
            command_id uuid PRIMARY KEY, actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            collection_id text NOT NULL REFERENCES {p}.synthetic_staging_limit(collection_id),
            action text NOT NULL CHECK(action IN ('record_synthetic_run','record_snapshot','reconcile_assembly')),
            payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{{64}}$'),
            result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp()
        );
        CREATE TABLE {p}.assembly_lifecycle_event (
            artifact_id uuid NOT NULL REFERENCES {s}.staged_artifact(id), revision bigint NOT NULL CHECK(revision>0),
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            target text NOT NULL CHECK(target IN ('held','erasure_required','erased')),
            cause text NOT NULL CHECK(cause IN ('raw_parent_state','derivation_not_current','privacy_rejected','retention_due','due_erasure')),
            changed_count integer NOT NULL CHECK(changed_count BETWEEN 1 AND 128),
            audit_id uuid NOT NULL UNIQUE REFERENCES {p}.audit_event(id),
            transaction_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(), PRIMARY KEY(artifact_id,revision)
        );
    """)
    tables = (
        (s, "synthetic_run"),
        (s, "adapter_candidate"),
        (s, "staging_snapshot"),
        (s, "snapshot_head"),
        (s, "snapshot_selection"),
        (s, "snapshot_resolution"),
        (p, "assembly_receipt"),
        (p, "assembly_lifecycle_event"),
    )
    for schema, table in tables:
        op.execute(f"""
            ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY assembly_guard ON {schema}.{table} USING(current_user='{prefix}_guard');
            REVOKE ALL ON {schema}.{table} FROM PUBLIC;
        """)
        if table not in ("adapter_candidate", "staging_snapshot", "snapshot_head"):
            op.execute(
                f"CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {schema}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record()"
            )
    install_helpers(b, p, s, prefix)
    op.execute(f"""
        ALTER TABLE {s}.adapter_candidate ADD CONSTRAINT candidate_payload_integrity CHECK(payload IS NULL OR coalesce(
            {p}.assembly_payload_valid(payload) AND octet_length(payload::text)=payload_bytes
            AND encode(sha256(convert_to(payload::text,'UTF8')),'hex')=payload_hash
            AND encode(sha256(convert_to(payload->>'text','UTF8')),'hex')=text_hash
            AND (payload->>'id')::uuid=id AND payload->>'adapter_local_id'=adapter_local_id
            AND payload->>'region_key'=region_key AND payload->>'artifact_class'=artifact_class
            AND payload->>'block_type'=block_type AND (payload->>'parent_local_id') IS NOT DISTINCT FROM parent_local_id,false));
        ALTER TABLE {s}.staging_snapshot ADD CONSTRAINT snapshot_payload_integrity CHECK(payload IS NULL OR coalesce(
            jsonb_typeof(payload)='object' AND octet_length(payload::text)=payload_bytes
            AND encode(sha256(convert_to(payload::text,'UTF8')),'hex')=payload_hash,false));
    """)
    install_commands(p, s, prefix)
    install_reads(p, s)
    definition = conn.exec_driver_sql(
        "SELECT pg_get_functiondef(to_regprocedure(%s))", (p + ".validate_synthetic_staging()",)
    ).scalar_one()
    anchor = f"SELECT coalesce(sum(octet_length(private_bytes)),0) INTO occupied FROM {s}.staged_artifact WHERE collection_id=NEW.collection_id;"
    if definition.count(anchor) != 1:
        raise RuntimeError("Installed staging capacity requires boundary review")
    op.execute(f"ALTER FUNCTION {p}.validate_synthetic_staging() RENAME TO validate_synthetic_staging_m23")
    op.execute(
        definition.replace(anchor, f"SELECT {p}.staging_live_bytes(NEW.collection_id) INTO occupied;", 1)
    )
    op.execute(f"DROP TRIGGER synthetic_staging ON {s}.staged_artifact")
    op.execute(
        f"CREATE TRIGGER synthetic_staging BEFORE INSERT OR UPDATE ON {s}.staged_artifact FOR EACH ROW EXECUTE FUNCTION {p}.validate_synthetic_staging()"
    )
    functions = function_signatures()
    for fn in functions:
        op.execute(f"REVOKE ALL ON FUNCTION {p}.{fn} FROM PUBLIC")
    if guard_oid is not None:
        guard = quote(prefix + "_guard")
        op.execute(f"GRANT CREATE ON SCHEMA {p} TO {guard}")
        for schema, table in tables:
            op.execute(f"GRANT SELECT,INSERT ON {schema}.{table} TO {guard}")
        op.execute(f"GRANT UPDATE(state,payload) ON {s}.adapter_candidate,{s}.staging_snapshot TO {guard}")
        op.execute(f"GRANT UPDATE(revision) ON {s}.snapshot_head TO {guard}")
        for fn in functions:
            op.execute(f"ALTER FUNCTION {p}.{fn} OWNER TO {guard}")
        op.execute(f"REVOKE CREATE ON SCHEMA {p} FROM {guard}")
        for fn in public_functions():
            op.execute(f"GRANT EXECUTE ON FUNCTION {p}.{fn} TO {quote(prefix + '_acquisition')}")


def function_signatures():
    return (
        "assembly_local_key(jsonb,boolean)",
        "assembly_payload_valid(jsonb)",
        "assembly_input_current(uuid,text)",
        "lock_assembly_input(uuid,text,text,boolean)",
        "staging_live_bytes(text)",
        "reserve_assembly_capacity(uuid,text,bigint,integer)",
        "validate_assembly_update()",
        "validate_snapshot_head()",
        "transition_assembly(uuid,text,text)",
        "sync_assembly_parent()",
        "apply_assembly_command(jsonb)",
        "read_candidates(uuid,text,uuid,integer)",
        "read_snapshot(uuid,bigint,text)",
        "validate_synthetic_staging()",
    )


def public_functions():
    return (
        "apply_assembly_command(jsonb)",
        "read_candidates(uuid,text,uuid,integer)",
        "read_snapshot(uuid,bigint,text)",
    )


def install_helpers(b, p, s, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.assembly_local_key(value jsonb, nullable boolean) RETURNS boolean
        LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
            SELECT coalesce((nullable AND (value IS NULL OR value='null'::jsonb)) OR
                (jsonb_typeof(value)='string' AND value#>>'{{}}' ~ '^[a-zA-Z0-9:_-]{{1,64}}$'),false)
        $$;
        CREATE FUNCTION {p}.assembly_payload_valid(body jsonb) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE cell jsonb; warning jsonb; key text; value text;
        BEGIN
            IF body IS NULL OR NOT {p}.operator_keys(body,
                ARRAY['id','adapter_local_id','region_key','artifact_class','block_type','text','unavailable_page_reason','unavailable_geometry_reason'],
                ARRAY['parent_local_id','content_state','content_reason','language','direction','quality_state','warnings','cell'])
                OR NOT {p}.assembly_local_key(body->'adapter_local_id',false)
                OR NOT {p}.assembly_local_key(body->'region_key',false)
                OR NOT {p}.assembly_local_key(body->'parent_local_id',true)
                OR NOT {p}.assembly_local_key(body->'language',true)
                OR jsonb_typeof(body->'text') IS DISTINCT FROM 'string' OR length(body->>'text')>8192
                OR coalesce(body->>'artifact_class','') NOT IN ('native_text','ocr_transcription')
                OR coalesce(body->>'block_type','') NOT IN ('heading','paragraph','list_item','table_cell','footnote')
                OR (body ? 'direction' AND jsonb_typeof(body->'direction') IS DISTINCT FROM 'string')
                OR (body ? 'content_state' AND jsonb_typeof(body->'content_state') IS DISTINCT FROM 'string')
                OR (body ? 'quality_state' AND (jsonb_typeof(body->'quality_state') IS DISTINCT FROM 'string' OR body->>'quality_state'<>'unassessed'))
                OR coalesce(body->>'direction','unknown') NOT IN ('ltr','rtl','unknown')
                OR coalesce(body->>'content_state','present') NOT IN ('present','empty','illegible','unsupported')
                OR (coalesce(body->>'content_state','present')='present')<>(length(body->>'text')>0) THEN RETURN false; END IF;
            FOREACH key IN ARRAY ARRAY['unavailable_page_reason','unavailable_geometry_reason'] LOOP
                IF jsonb_typeof(body->key) IS DISTINCT FROM 'string' OR length(btrim(body->>key)) NOT BETWEEN 1 AND 2048 THEN RETURN false; END IF;
            END LOOP;
            IF coalesce(body->>'content_state','present') IN ('illegible','unsupported') AND
                (jsonb_typeof(body->'content_reason') IS DISTINCT FROM 'string' OR length(btrim(body->>'content_reason')) NOT BETWEEN 1 AND 2048) THEN RETURN false; END IF;
            IF body->'content_reason' IS NOT NULL AND body->'content_reason'<>'null'::jsonb AND
                (jsonb_typeof(body->'content_reason')<>'string' OR length(btrim(body->>'content_reason')) NOT BETWEEN 1 AND 2048) THEN RETURN false; END IF;
            IF jsonb_typeof(coalesce(body->'warnings','[]'::jsonb)) IS DISTINCT FROM 'array' OR jsonb_array_length(coalesce(body->'warnings','[]'::jsonb))>10 THEN RETURN false; END IF;
            FOR warning IN SELECT * FROM jsonb_array_elements(coalesce(body->'warnings','[]'::jsonb)) LOOP
                IF jsonb_typeof(warning)<>'string' OR length(warning#>>'{{}}') NOT BETWEEN 1 AND 256 THEN RETURN false; END IF;
            END LOOP;
            cell:=body->'cell';
            IF body->>'block_type'='table_cell' THEN
                IF NOT {p}.operator_keys(cell,ARRAY['table_local_id','row','column'],ARRAY['row_span','column_span'])
                    OR NOT {p}.assembly_local_key(cell->'table_local_id',false) THEN RETURN false; END IF;
                FOREACH key IN ARRAY ARRAY['row','column','row_span','column_span'] LOOP
                    value:=coalesce(cell->>key,CASE WHEN key IN ('row_span','column_span') THEN '1' ELSE NULL END);
                    IF value IS NULL OR (cell ? key AND jsonb_typeof(cell->key)<>'number') OR value !~ '^[0-9]{{1,3}}$'
                        OR value::integer NOT BETWEEN (CASE WHEN key IN ('row','column') THEN 0 ELSE 1 END) AND (CASE WHEN key IN ('row','column') THEN 99 ELSE 100 END) THEN RETURN false; END IF;
                END LOOP;
                IF (cell->>'row')::integer+coalesce((cell->>'row_span')::integer,1)>100
                    OR (cell->>'column')::integer+coalesce((cell->>'column_span')::integer,1)>100 THEN RETURN false; END IF;
            ELSIF cell IS NOT NULL AND cell<>'null'::jsonb THEN RETURN false; END IF;
            RETURN true;
        END $$;
        CREATE FUNCTION {p}.assembly_input_current(artifact uuid,requested_purpose text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {s}.staged_artifact a WHERE a.id=artifact AND a.purpose=requested_purpose
                AND a.acquired_by IS NOT NULL AND a.state IN ('staged','processing','validated') AND a.retention_deadline>statement_timestamp()
                AND {p}.private_operation_allowed(a.assessment_id,a.assessment_revision,a.collection_id,a.purpose,a.artifact_hash,'derive')
                AND {p}.private_operation_allowed(a.assessment_id,a.assessment_revision,a.collection_id,a.purpose,a.artifact_hash,'retain'))
        $$;
        CREATE FUNCTION {p}.lock_assembly_input(artifact uuid,cid text,requested_purpose text,write boolean)
        RETURNS {s}.staged_artifact LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE item {s}.staged_artifact; operator_id uuid;
        BEGIN
            operator_id:={p}.current_actor();
            PERFORM 1 FROM {p}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {p}.is_native_operator() THEN RAISE EXCEPTION 'Native session required' USING ERRCODE='42501'; END IF;
            PERFORM {b}.identity_check_legacy_authority(true);
            IF write THEN
                SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                    acquired_at,retention_deadline,state,purpose,acquired_by INTO item FROM {s}.staged_artifact WHERE id=artifact FOR UPDATE;
            ELSE
                SELECT id,collection_id,assessment_id,assessment_revision,artifact_hash,size_bytes,NULL::bytea,
                    acquired_at,retention_deadline,state,purpose,acquired_by INTO item FROM {s}.staged_artifact WHERE id=artifact FOR SHARE;
            END IF;
            IF item.id IS NULL OR item.collection_id IS DISTINCT FROM cid OR NOT {p}.assigned(cid,'acquire') THEN
                RAISE EXCEPTION 'Scoped artifact required' USING ERRCODE='42501'; END IF;
            PERFORM {p}.lock_acquisition_authority(item.assessment_id,item.assessment_revision,cid);
            IF NOT {p}.assembly_input_current(artifact,requested_purpose) THEN
                RAISE EXCEPTION 'Current exact derive and retain authority required' USING ERRCODE='42501'; END IF;
            RETURN item;
        END $$;
        CREATE FUNCTION {p}.staging_live_bytes(cid text) RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT coalesce((SELECT sum(octet_length(private_bytes)) FROM {s}.staged_artifact WHERE collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(c.payload::text)) FROM {s}.adapter_candidate c JOIN {s}.synthetic_run r ON r.id=c.run_id WHERE r.collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(t.payload::text)) FROM {s}.staging_snapshot t JOIN {s}.synthetic_run r ON r.id=t.run_id WHERE r.collection_id=cid),0)
        $$;
        CREATE FUNCTION {p}.reserve_assembly_capacity(artifact uuid,cid text,extra bigint,added_rows integer) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE capacity bigint; family_bytes bigint; family_rows bigint;
        BEGIN
            SELECT maximum_bytes INTO capacity FROM {p}.synthetic_staging_limit WHERE collection_id=cid FOR UPDATE;
            SELECT coalesce(sum(bytes),0),count(*) INTO family_bytes,family_rows FROM (
                SELECT octet_length(payload::text) bytes FROM {s}.adapter_candidate WHERE artifact_id=artifact
                UNION ALL SELECT octet_length(payload::text) FROM {s}.staging_snapshot WHERE artifact_id=artifact) q;
            IF capacity IS NULL OR extra<0 OR added_rows<1 OR family_rows+added_rows>128 OR family_bytes+extra>2097152
                OR {p}.staging_live_bytes(cid)+extra>capacity THEN RAISE EXCEPTION 'Synthetic assembly capacity exceeded' USING ERRCODE='54000'; END IF;
        END $$;
        CREATE FUNCTION {p}.validate_assembly_update() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF TG_OP<>'UPDATE' OR (to_jsonb(NEW)-ARRAY['state','payload','parent_revision']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','payload','parent_revision'])
                OR (NEW.payload IS DISTINCT FROM OLD.payload AND NOT(NEW.state='erased' AND NEW.payload IS NULL))
                OR NOT ((NEW.state='held' AND OLD.state='active') OR (NEW.state='erasure_required' AND OLD.state IN ('active','held'))
                    OR (NEW.state='erased' AND OLD.state IN ('active','held','erasure_required')))
                OR NOT EXISTS(SELECT 1 FROM {p}.assembly_lifecycle_event e WHERE e.artifact_id=NEW.artifact_id AND e.target=NEW.state
                    AND e.actor_id={p}.current_actor() AND e.transaction_id=pg_current_xact_id()) THEN
                RAISE EXCEPTION 'Immutable audited assembly lifecycle required' USING ERRCODE='55000'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER candidate_update BEFORE UPDATE OR DELETE ON {s}.adapter_candidate FOR EACH ROW EXECUTE FUNCTION {p}.validate_assembly_update();
        CREATE TRIGGER snapshot_update BEFORE UPDATE OR DELETE ON {s}.staging_snapshot FOR EACH ROW EXECUTE FUNCTION {p}.validate_assembly_update();
        CREATE TRIGGER no_truncate BEFORE TRUNCATE ON {s}.adapter_candidate FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        CREATE TRIGGER no_truncate BEFORE TRUNCATE ON {s}.staging_snapshot FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        CREATE FUNCTION {p}.validate_snapshot_head() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF (NEW.id,NEW.run_id) IS DISTINCT FROM (OLD.id,OLD.run_id) OR NEW.revision<>OLD.revision+1 THEN
                RAISE EXCEPTION 'Snapshot head cannot retarget or rewind' USING ERRCODE='55000'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER head_update BEFORE UPDATE ON {s}.snapshot_head FOR EACH ROW EXECUTE FUNCTION {p}.validate_snapshot_head();
        CREATE TRIGGER head_no_delete BEFORE DELETE OR TRUNCATE ON {s}.snapshot_head FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        CREATE FUNCTION {p}.transition_assembly(artifact uuid,following text,cause_value text) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE next_revision bigint; audit uuid; changed integer; item record; eligible text[];
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision INTO item FROM {s}.staged_artifact WHERE id=artifact FOR UPDATE;
            eligible:=CASE following WHEN 'held' THEN ARRAY['active'] WHEN 'erasure_required' THEN ARRAY['active','held']
                WHEN 'erased' THEN ARRAY['active','held','erasure_required'] ELSE ARRAY[]::text[] END;
            SELECT count(*) INTO changed FROM (SELECT 1 FROM {s}.adapter_candidate WHERE artifact_id=artifact AND state=ANY(eligible)
                UNION ALL SELECT 1 FROM {s}.staging_snapshot WHERE artifact_id=artifact AND state=ANY(eligible)) q;
            IF changed=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(e.revision),0)+1 INTO next_revision FROM {p}.assembly_lifecycle_event e WHERE e.artifact_id=artifact;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {p}.current_actor(),item.collection_id,a.evidence_id,'assembly_lifecycle',artifact::text,next_revision,cause_value
                    FROM {p}.acquisition_assessment a WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision
                RETURNING id INTO audit;
            INSERT INTO {p}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id)
                VALUES(artifact,next_revision,{p}.current_actor(),following,cause_value,changed,audit);
            UPDATE {s}.adapter_candidate SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END
                WHERE artifact_id=artifact AND state=ANY(eligible);
            UPDATE {s}.staging_snapshot SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END
                WHERE artifact_id=artifact AND state=ANY(eligible);
            RETURN changed;
        END $$;
        CREATE FUNCTION {p}.sync_assembly_parent() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF NEW.state IN ('held_pending_reassessment','erasure_required','erased') AND NEW.state IS DISTINCT FROM OLD.state THEN
                PERFORM {p}.transition_assembly(NEW.id,CASE NEW.state WHEN 'held_pending_reassessment' THEN 'held' ELSE NEW.state END,'raw_parent_state');
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER assembly_parent AFTER UPDATE OF state ON {s}.staged_artifact FOR EACH ROW EXECUTE FUNCTION {p}.sync_assembly_parent();
    """)


def install_commands(p, s, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.apply_assembly_command(envelope jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE operator_id uuid; cid text; cmd uuid; action_name text; fingerprint text; previous {p}.assembly_receipt;
            artifact {s}.staged_artifact; run {s}.synthetic_run; head {s}.snapshot_head;
            item jsonb; selection jsonb; resolution jsonb; body jsonb; normalized jsonb:='[]'; rejected jsonb;
            profile_hash text; output_hash text; body_hash text; selected_hashes jsonb; bytes bigint:=0; expected bigint; object_id uuid;
            region_count integer; candidate_count integer; count_value integer; rev bigint; aid uuid;
            scanned integer:=0; held integer:=0; required integer:=0; erased integer:=0; last_id uuid;
            target text; cause_value text; row_item record; assessment {p}.acquisition_assessment; cursor_id uuid;
            batch_limit integer; erase_due boolean; result jsonb;
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR jsonb_typeof(envelope)<>'object' THEN
                RAISE EXCEPTION 'Bounded assembly command required' USING ERRCODE='22023'; END IF;
            cmd:=(envelope->>'command_id')::uuid; cid:=envelope->>'collection_id'; action_name:=envelope->>'action';
            IF cmd IS NULL OR cid IS NULL OR action_name IS NULL OR jsonb_typeof(envelope->'reason') IS DISTINCT FROM 'string'
                OR length(btrim(envelope->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Invalid assembly command' USING ERRCODE='22023'; END IF;
            operator_id:={p}.current_actor(); PERFORM 1 FROM {p}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {p}.is_native_operator() OR NOT {p}.assigned(cid,'acquire')
                OR (action_name='record_snapshot' AND NOT {p}.assigned(cid,'content')) THEN
                RAISE EXCEPTION 'Native scoped assembly authority required' USING ERRCODE='42501'; END IF;
            IF action_name='record_synthetic_run' THEN
                IF NOT {p}.operator_keys(envelope,ARRAY['command_id','collection_id','reason','action','run_id','artifact_id',
                    'assessment_id','assessment_revision','artifact_hash','purpose','profile_key','candidates'])
                    OR NOT {p}.assembly_local_key(envelope->'profile_key',false) OR jsonb_typeof(envelope->'candidates') IS DISTINCT FROM 'array'
                    OR jsonb_array_length(envelope->'candidates') NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Invalid synthetic run' USING ERRCODE='22023'; END IF;
                artifact:={p}.lock_assembly_input((envelope->>'artifact_id')::uuid,cid,envelope->>'purpose',true);
                IF (artifact.assessment_id,artifact.assessment_revision,artifact.artifact_hash) IS DISTINCT FROM
                    ((envelope->>'assessment_id')::uuid,(envelope->>'assessment_revision')::bigint,envelope->>'artifact_hash') THEN
                    RAISE EXCEPTION 'Exact source lineage required' USING ERRCODE='42501'; END IF;
            ELSIF action_name='record_snapshot' THEN
                IF NOT {p}.operator_keys(envelope,ARRAY['command_id','collection_id','reason','action','snapshot_id','run_id','purpose','expected_revision','payload'],ARRAY['parent_hash']) THEN
                    RAISE EXCEPTION 'Invalid snapshot command' USING ERRCODE='22023'; END IF;
                SELECT r.* INTO run FROM {s}.synthetic_run r WHERE r.id=(envelope->>'run_id')::uuid;
                IF run.id IS NULL THEN RAISE EXCEPTION 'Scoped run required' USING ERRCODE='42501'; END IF;
                artifact:={p}.lock_assembly_input(run.artifact_id,cid,envelope->>'purpose',true);
                IF run.purpose IS DISTINCT FROM envelope->>'purpose' OR EXISTS(SELECT 1 FROM {s}.adapter_candidate WHERE run_id=run.id AND state<>'active') THEN
                    RAISE EXCEPTION 'Active unchanged candidate lineage required' USING ERRCODE='42501'; END IF;
            ELSIF action_name='reconcile_assembly' THEN
                IF NOT {p}.operator_keys(envelope,ARRAY['command_id','collection_id','reason','action'],ARRAY['after','limit','erase_due'])
                    OR (envelope ? 'limit' AND (jsonb_typeof(envelope->'limit') IS DISTINCT FROM 'number' OR envelope->>'limit' !~ '^[0-9]{{1,3}}$'))
                    OR (envelope ? 'erase_due' AND jsonb_typeof(envelope->'erase_due') IS DISTINCT FROM 'boolean') THEN RAISE EXCEPTION 'Invalid assembly reconciliation' USING ERRCODE='22023'; END IF;
                batch_limit:=coalesce((envelope->>'limit')::integer,50); cursor_id:=(envelope->>'after')::uuid; erase_due:=coalesce((envelope->>'erase_due')::boolean,false);
                IF batch_limit NOT BETWEEN 1 AND 100 OR NOT EXISTS(SELECT 1 FROM {p}.synthetic_staging_limit WHERE collection_id=cid) THEN
                    RAISE EXCEPTION 'Declared bounded scope required' USING ERRCODE='22023'; END IF;
            ELSE RAISE EXCEPTION 'Unsupported assembly command' USING ERRCODE='22023'; END IF;
            fingerprint:=encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:assembly:'||cmd::text,0));
            SELECT * INTO previous FROM {p}.assembly_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>operator_id THEN RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Assembly retry changed' USING ERRCODE='40001'; END IF;
                RETURN previous.result || jsonb_build_object('replayed',true);
            END IF;
            IF action_name='record_synthetic_run' THEN
                object_id:=(envelope->>'run_id')::uuid;
                IF object_id IS NULL THEN RAISE EXCEPTION 'Run identity required' USING ERRCODE='22023'; END IF;
                IF (SELECT count(*) FROM {s}.synthetic_run WHERE artifact_id=artifact.id)>=4 THEN RAISE EXCEPTION 'Synthetic run bound exceeded' USING ERRCODE='54000'; END IF;
                FOR item IN SELECT * FROM jsonb_array_elements(envelope->'candidates') LOOP
                    IF NOT {p}.assembly_payload_valid(item) OR item->>'id' IS NULL THEN RAISE EXCEPTION 'Invalid synthetic candidate' USING ERRCODE='22023'; END IF;
                    item:=jsonb_build_object('parent_local_id',NULL,'content_state','present','content_reason',NULL,'language',NULL,'direction','unknown','quality_state','unassessed','warnings','[]'::jsonb,'cell',NULL)||item;
                    IF item->>'block_type'='table_cell' THEN item:=jsonb_set(item,'{{cell}}',jsonb_build_object('row_span',1,'column_span',1)||item->'cell'); END IF;
                    normalized:=normalized||jsonb_build_array(item); bytes:=bytes+octet_length(item::text);
                END LOOP;
                PERFORM {p}.reserve_assembly_capacity(artifact.id,cid,bytes,jsonb_array_length(normalized));
                profile_hash:=encode(sha256(convert_to(jsonb_build_object('producer','original_synthetic','version','synthetic-candidates-1','profile_key',envelope->>'profile_key')::text,'UTF8')),'hex');
                output_hash:=encode(sha256(convert_to(normalized::text,'UTF8')),'hex');
                INSERT INTO {s}.synthetic_run(id,artifact_id,collection_id,assessment_id,assessment_revision,artifact_hash,purpose,
                    retention_deadline,profile_key,profile_hash,output_hash,actor_id)
                    VALUES(object_id,artifact.id,cid,artifact.assessment_id,artifact.assessment_revision,artifact.artifact_hash,artifact.purpose,
                        artifact.retention_deadline,envelope->>'profile_key',profile_hash,output_hash,operator_id);
                FOR item IN SELECT * FROM jsonb_array_elements(normalized) LOOP
                    INSERT INTO {s}.adapter_candidate(id,run_id,artifact_id,adapter_local_id,region_key,parent_local_id,artifact_class,block_type,payload,payload_hash,text_hash,payload_bytes)
                        VALUES((item->>'id')::uuid,object_id,artifact.id,item->>'adapter_local_id',item->>'region_key',item->>'parent_local_id',item->>'artifact_class',item->>'block_type',item,
                            encode(sha256(convert_to(item::text,'UTF8')),'hex'),encode(sha256(convert_to(item->>'text','UTF8')),'hex'),octet_length(item::text));
                END LOOP;
                IF EXISTS(SELECT 1 FROM {s}.adapter_candidate c WHERE c.run_id=object_id AND c.parent_local_id IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM {s}.adapter_candidate parent WHERE parent.run_id=c.run_id AND parent.adapter_local_id=c.parent_local_id)) THEN RAISE EXCEPTION 'Missing local parent' USING ERRCODE='23514'; END IF;
                IF EXISTS(WITH RECURSIVE walk AS (
                    SELECT c.adapter_local_id,c.parent_local_id,ARRAY[c.adapter_local_id] path,false cycle FROM {s}.adapter_candidate c WHERE c.run_id=object_id
                    UNION ALL SELECT parent.adapter_local_id,parent.parent_local_id,w.path||parent.adapter_local_id,parent.adapter_local_id=ANY(w.path)
                        FROM walk w JOIN {s}.adapter_candidate parent ON parent.run_id=object_id AND parent.adapter_local_id=w.parent_local_id WHERE NOT w.cycle)
                    SELECT 1 FROM walk WHERE cycle) THEN RAISE EXCEPTION 'Local hierarchy cycle' USING ERRCODE='23514'; END IF;
                rev:=1;
            ELSIF action_name='record_snapshot' THEN
                object_id:=(envelope->>'snapshot_id')::uuid; expected:=(envelope->>'expected_revision')::bigint; body:=envelope->'payload';
                IF object_id IS NULL OR expected IS NULL OR expected NOT BETWEEN 0 AND 31 OR jsonb_typeof(envelope->'expected_revision')<>'number'
                    OR envelope->>'expected_revision' !~ '^[0-9]{{1,2}}$' OR NOT {p}.operator_keys(body,ARRAY['selections','order','reason'],ARRAY['resolutions'])
                    OR jsonb_typeof(body->'selections') IS DISTINCT FROM 'array' OR jsonb_array_length(body->'selections') NOT BETWEEN 1 AND 50
                    OR jsonb_typeof(body->'order') IS DISTINCT FROM 'array' OR jsonb_array_length(body->'order') NOT BETWEEN 1 AND 50
                    OR jsonb_typeof(body->'reason') IS DISTINCT FROM 'string' OR length(btrim(body->>'reason')) NOT BETWEEN 1 AND 2048
                    OR jsonb_typeof(coalesce(body->'resolutions','[]'::jsonb)) IS DISTINCT FROM 'array' OR jsonb_array_length(coalesce(body->'resolutions','[]'::jsonb))>50 THEN
                    RAISE EXCEPTION 'Bounded snapshot proposal required' USING ERRCODE='22023'; END IF;
                SELECT h.* INTO head FROM {s}.snapshot_head h WHERE h.id=object_id FOR UPDATE;
                IF coalesce(head.revision,0)<>expected OR (head.id IS NOT NULL AND head.run_id<>run.id)
                    OR (expected=0 AND envelope->>'parent_hash' IS NOT NULL)
                    OR (expected>0 AND NOT EXISTS(SELECT 1 FROM {s}.staging_snapshot t WHERE t.id=object_id AND t.revision=expected
                        AND t.snapshot_hash=envelope->>'parent_hash' AND t.state='active')) THEN RAISE EXCEPTION 'Snapshot parent conflict' USING ERRCODE='40001'; END IF;
                body:=jsonb_build_object('resolutions','[]'::jsonb)||body;
                rev:=expected+1; body_hash:=encode(sha256(convert_to(body::text,'UTF8')),'hex');
                SELECT jsonb_agg(jsonb_build_object('candidate_id',c.id,'payload_hash',c.payload_hash) ORDER BY o.ord)
                    INTO selected_hashes FROM jsonb_array_elements_text(body->'order') WITH ORDINALITY o(id,ord)
                    JOIN {s}.adapter_candidate c ON c.id=o.id::uuid AND c.run_id=run.id;
                output_hash:=encode(sha256(convert_to(jsonb_build_object('profile','assembly-proposal-1','snapshot_id',object_id,
                    'revision',rev,'parent_hash',envelope->>'parent_hash','run_id',run.id,'run_output_hash',run.output_hash,
                    'producer',run.producer,'producer_profile_hash',run.profile_hash,'artifact_id',run.artifact_id,
                    'artifact_hash',run.artifact_hash,'collection_id',run.collection_id,'assessment_id',run.assessment_id,
                    'assessment_revision',run.assessment_revision,'purpose',run.purpose,'retention_epoch',extract(epoch FROM run.retention_deadline),
                    'editor',operator_id,'selected_candidate_hashes',selected_hashes,'payload',body)::text,'UTF8')),'hex');
                PERFORM {p}.reserve_assembly_capacity(artifact.id,cid,octet_length(body::text),1);
                INSERT INTO {s}.staging_snapshot(id,revision,run_id,artifact_id,parent_hash,payload,payload_hash,snapshot_hash,payload_bytes,actor_id)
                    VALUES(object_id,rev,run.id,artifact.id,envelope->>'parent_hash',body,body_hash,output_hash,octet_length(body::text),operator_id);
                SELECT count(DISTINCT region_key) INTO region_count FROM {s}.adapter_candidate WHERE run_id=run.id;
                IF jsonb_array_length(body->'selections')<>region_count OR jsonb_array_length(body->'order')<>region_count THEN RAISE EXCEPTION 'Complete explicit region selection required' USING ERRCODE='23514'; END IF;
                FOR selection IN SELECT * FROM jsonb_array_elements(body->'selections') LOOP
                    IF NOT {p}.operator_keys(selection,ARRAY['region_key','candidate_id']) THEN RAISE EXCEPTION 'Invalid selection' USING ERRCODE='22023'; END IF;
                    INSERT INTO {s}.snapshot_selection(snapshot_id,revision,run_id,region_key,candidate_id,position)
                        SELECT object_id,rev,run.id,selection->>'region_key',(selection->>'candidate_id')::uuid,ord::integer
                            FROM jsonb_array_elements_text(body->'order') WITH ORDINALITY o(id,ord) WHERE id=selection->>'candidate_id';
                END LOOP;
                IF (SELECT count(*) FROM {s}.snapshot_selection WHERE snapshot_id=object_id AND revision=rev)<>region_count THEN RAISE EXCEPTION 'Duplicate or missing order selection' USING ERRCODE='23514'; END IF;
                FOR resolution IN SELECT * FROM jsonb_array_elements(body->'resolutions') LOOP
                    IF NOT {p}.operator_keys(resolution,ARRAY['region_key','candidate_id','rejected_candidate_ids','reason'])
                        OR jsonb_typeof(resolution->'rejected_candidate_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(resolution->'rejected_candidate_ids') NOT BETWEEN 1 AND 49
                        OR jsonb_typeof(resolution->'reason') IS DISTINCT FROM 'string' OR length(btrim(resolution->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Explicit conflict resolution required' USING ERRCODE='22023'; END IF;
                    SELECT count(*) INTO candidate_count FROM {s}.adapter_candidate WHERE run_id=run.id AND region_key=resolution->>'region_key';
                    IF candidate_count<=1 OR jsonb_array_length(resolution->'rejected_candidate_ids')<>candidate_count-1 THEN RAISE EXCEPTION 'All alternatives require resolution' USING ERRCODE='23514'; END IF;
                    FOR rejected IN SELECT * FROM jsonb_array_elements(resolution->'rejected_candidate_ids') LOOP
                        INSERT INTO {s}.snapshot_resolution(snapshot_id,revision,run_id,region_key,selected_candidate_id,rejected_candidate_id)
                            VALUES(object_id,rev,run.id,resolution->>'region_key',(resolution->>'candidate_id')::uuid,(rejected#>>'{{}}')::uuid);
                    END LOOP;
                END LOOP;
                SELECT count(*) INTO candidate_count FROM {s}.adapter_candidate WHERE run_id=run.id;
                IF (SELECT count(*) FROM {s}.snapshot_resolution WHERE snapshot_id=object_id AND revision=rev)<>candidate_count-region_count THEN RAISE EXCEPTION 'Unresolved overlapping candidates' USING ERRCODE='23514'; END IF;
                INSERT INTO {s}.snapshot_head(id,revision,run_id) VALUES(object_id,rev,run.id)
                    ON CONFLICT(id) DO UPDATE SET revision=excluded.revision;
            ELSE
                FOR row_item IN SELECT id,collection_id,assessment_id,assessment_revision,purpose,artifact_hash,state,retention_deadline
                    FROM {s}.staged_artifact WHERE collection_id=cid AND (cursor_id IS NULL OR id>cursor_id) ORDER BY id LIMIT batch_limit FOR UPDATE
                LOOP
                    scanned:=scanned+1; last_id:=row_item.id;
                    PERFORM {p}.lock_acquisition_authority(row_item.assessment_id,row_item.assessment_revision,cid);
                    SELECT a.* INTO assessment FROM {p}.acquisition_assessment a WHERE a.id=row_item.assessment_id AND a.revision=row_item.assessment_revision AND a.collection_id=cid;
                    target:=NULL; cause_value:=NULL;
                    IF row_item.state='erased' THEN target:='erased'; cause_value:='raw_parent_state';
                    ELSIF row_item.state='erasure_required' OR row_item.retention_deadline<=statement_timestamp() THEN target:='erasure_required'; cause_value:='retention_due';
                    ELSIF EXISTS(SELECT 1 FROM {p}.privacy_review v WHERE v.id=assessment.privacy_review_id AND v.collection_id=cid
                        AND v.revision>=assessment.privacy_review_revision AND v.clearance='rejected' AND v.valid_from<=statement_timestamp()) THEN target:='erasure_required'; cause_value:='privacy_rejected';
                    ELSIF row_item.state='held_pending_reassessment' OR NOT {p}.assembly_input_current(row_item.id,row_item.purpose) THEN target:='held'; cause_value:='derivation_not_current'; END IF;
                    IF target IS NOT NULL THEN
                        count_value:={p}.transition_assembly(row_item.id,target,cause_value);
                        IF target='held' THEN held:=held+count_value; ELSIF target='erasure_required' THEN required:=required+count_value; ELSE erased:=erased+count_value; END IF;
                    END IF;
                    IF erase_due AND (target='erasure_required' OR EXISTS(SELECT 1 FROM {s}.adapter_candidate WHERE artifact_id=row_item.id AND state='erasure_required')
                        OR EXISTS(SELECT 1 FROM {s}.staging_snapshot WHERE artifact_id=row_item.id AND state='erasure_required')) THEN
                        erased:=erased+{p}.transition_assembly(row_item.id,'erased','due_erasure');
                    END IF;
                END LOOP;
            END IF;
            IF action_name<>'reconcile_assembly' THEN
                SELECT a.evidence_id INTO aid FROM {p}.acquisition_assessment a WHERE a.id=artifact.assessment_id AND a.revision=artifact.assessment_revision;
                INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                    VALUES(operator_id,cid,aid,CASE action_name WHEN 'record_synthetic_run' THEN 'synthetic_run' ELSE 'staging_snapshot' END,object_id::text,rev,envelope->>'reason');
            END IF;
            result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'object_id',object_id,'revision',rev,'output_hash',output_hash,
                'scanned',scanned,'held',held,'erasure_required',required,'erased',erased,'next_after',CASE WHEN scanned=batch_limit THEN last_id ELSE NULL END);
            INSERT INTO {p}.assembly_receipt(command_id,actor_id,collection_id,action,payload_hash,result) VALUES(cmd,operator_id,cid,action_name,fingerprint,result);
            RETURN result;
        END $$;
    """)


def install_reads(p, s):
    op.execute(f"""
        CREATE FUNCTION {p}.read_candidates(run_uuid uuid,requested_purpose text,cursor_id uuid,batch_limit integer) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE run {s}.synthetic_run; item record; result jsonb:='[]'; last_id uuid; count_value integer:=0;
        BEGIN
            IF batch_limit IS NULL OR batch_limit NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Bounded candidate read required' USING ERRCODE='22023'; END IF;
            SELECT r.* INTO run FROM {s}.synthetic_run r WHERE r.id=run_uuid;
            IF run.id IS NULL THEN RETURN jsonb_build_object('items','[]'::jsonb,'next_after',NULL); END IF;
            PERFORM {p}.lock_assembly_input(run.artifact_id,run.collection_id,requested_purpose,false);
            FOR item IN SELECT id,payload,payload_hash,text_hash FROM {s}.adapter_candidate WHERE run_id=run_uuid AND state='active'
                AND (cursor_id IS NULL OR id>cursor_id) ORDER BY id LIMIT batch_limit FOR SHARE
            LOOP
                count_value:=count_value+1; last_id:=item.id;
                result:=result||jsonb_build_array(jsonb_build_object('run_id',run.id,'artifact_id',run.artifact_id,'collection_id',run.collection_id,
                    'assessment_id',run.assessment_id,'assessment_revision',run.assessment_revision,'artifact_hash',run.artifact_hash,'purpose',run.purpose,
                    'retention_deadline',run.retention_deadline,'payload_hash',item.payload_hash,'text_hash',item.text_hash,'payload',item.payload));
            END LOOP;
            RETURN jsonb_build_object('items',result,'next_after',CASE WHEN count_value=batch_limit THEN last_id ELSE NULL END);
        END $$;
        CREATE FUNCTION {p}.read_snapshot(snapshot_uuid uuid,rev bigint,requested_purpose text) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE item record; run {s}.synthetic_run; result jsonb;
        BEGIN
            IF rev IS NULL OR rev NOT BETWEEN 1 AND 32 THEN RAISE EXCEPTION 'Bounded snapshot read required' USING ERRCODE='22023'; END IF;
            SELECT id,revision,run_id,artifact_id,parent_hash,payload_hash INTO item FROM {s}.staging_snapshot WHERE id=snapshot_uuid AND revision=rev AND state='active';
            IF item.id IS NULL THEN RETURN NULL; END IF;
            SELECT r.* INTO run FROM {s}.synthetic_run r WHERE r.id=item.run_id;
            PERFORM {p}.lock_assembly_input(item.artifact_id,run.collection_id,requested_purpose,false);
            SELECT jsonb_build_object('snapshot_id',id,'revision',revision,'run_id',run_id,'parent_hash',parent_hash,'payload_hash',payload_hash,'snapshot_hash',snapshot_hash,'payload',payload)
                INTO result FROM {s}.staging_snapshot WHERE id=snapshot_uuid AND revision=rev AND state='active' FOR SHARE;
            RETURN result;
        END $$;
    """)


def downgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    _, p, s, _, _ = names()
    if conn.exec_driver_sql(
        f"SELECT EXISTS(SELECT 1 FROM {s}.synthetic_run) OR EXISTS(SELECT 1 FROM {p}.assembly_receipt) OR EXISTS(SELECT 1 FROM {p}.assembly_lifecycle_event)"
    ).scalar_one():
        raise RuntimeError("Private assembly requires forward repair")
    op.execute(f"DROP TRIGGER assembly_parent ON {s}.staged_artifact")
    op.execute(f"DROP TRIGGER synthetic_staging ON {s}.staged_artifact")
    op.execute(f"DROP FUNCTION {p}.validate_synthetic_staging()")
    op.execute(f"ALTER FUNCTION {p}.validate_synthetic_staging_m23() RENAME TO validate_synthetic_staging")
    op.execute(
        f"CREATE TRIGGER synthetic_staging BEFORE INSERT OR UPDATE ON {s}.staged_artifact FOR EACH ROW EXECUTE FUNCTION {p}.validate_synthetic_staging()"
    )
    op.execute(f"DROP TRIGGER candidate_update ON {s}.adapter_candidate")
    op.execute(f"DROP TRIGGER snapshot_update ON {s}.staging_snapshot")
    op.execute(f"DROP TRIGGER head_update ON {s}.snapshot_head")
    op.execute(f"ALTER TABLE {s}.adapter_candidate DROP CONSTRAINT candidate_payload_integrity")
    for fn in function_signatures()[:-1]:
        op.execute(f"DROP FUNCTION {p}.{fn}")
    op.execute(f"""
        DROP TABLE {s}.snapshot_resolution,{s}.snapshot_selection,{s}.snapshot_head;
        DROP TABLE {s}.staging_snapshot,{s}.adapter_candidate;
        DROP TABLE {s}.synthetic_run;
        DROP TABLE {p}.assembly_receipt,{p}.assembly_lifecycle_event;
        ALTER TABLE {s}.staged_artifact DROP CONSTRAINT assembly_source_lineage;
    """)
