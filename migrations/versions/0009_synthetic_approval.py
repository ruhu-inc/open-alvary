"""Private original-synthetic identity-bound review and atomic approved record sets."""

from hashlib import sha256

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def names():
    conn = op.get_bind()
    base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
    quote = conn.dialect.identifier_preparer.quote_identifier
    p, s, c = (
        quote(f"{base}_{kind}" if base.startswith("test_") else kind)
        for kind in ("policy", "staging", "corpus")
    )
    return quote(base), p, s, c, "oa_" + sha256(base.encode()).hexdigest()[:12], quote


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
        for fn in helpers() + ("lock_assembly_input(uuid,text,text,boolean)", "oa_compile(jsonb,jsonb)"):
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
        ALTER TABLE {b}.identity_expressions ADD CONSTRAINT approval_expression_work UNIQUE(id,work_id);
        ALTER TABLE {b}.identity_works ADD CONSTRAINT approval_work_collection UNIQUE(id,collection_id);
        ALTER TABLE {b}.identity_manifestations ADD CONSTRAINT approval_manifestation_bytes UNIQUE(id,raw_hash,size_bytes);
        ALTER TABLE {s}.staged_artifact ADD CONSTRAINT approval_artifact_bytes UNIQUE(id,collection_id,artifact_hash,size_bytes);
        CREATE TABLE {s}.synthetic_expression_binding (
            id uuid PRIMARY KEY,expression_id text NOT NULL,work_id text NOT NULL,manifestation_id text NOT NULL,
            artifact_id uuid NOT NULL,collection_id text NOT NULL,artifact_hash text NOT NULL,size_bytes bigint NOT NULL,
            identity_hash text NOT NULL CHECK(identity_hash ~ '^[a-f0-9]{{64}}$'),
            evidence_id uuid NOT NULL REFERENCES {p}.evidence(id),reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 2048),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            payload jsonb,
            UNIQUE(expression_id,artifact_id),UNIQUE(id,expression_id,artifact_id,collection_id),
            FOREIGN KEY(expression_id,work_id) REFERENCES {b}.identity_expressions(id,work_id),
            FOREIGN KEY(work_id,collection_id) REFERENCES {b}.identity_works(id,collection_id),
            FOREIGN KEY(work_id,manifestation_id) REFERENCES {b}.identity_work_manifestations(work_id,manifestation_id),
            FOREIGN KEY(manifestation_id,artifact_hash,size_bytes) REFERENCES {b}.identity_manifestations(id,raw_hash,size_bytes),
            FOREIGN KEY(artifact_id,collection_id,artifact_hash,size_bytes) REFERENCES {s}.staged_artifact(id,collection_id,artifact_hash,size_bytes),
            CHECK((state='erased')=(payload IS NULL))
        );
        CREATE TABLE {s}.snapshot_review (
            id uuid PRIMARY KEY,command_id uuid NOT NULL UNIQUE,binding_id uuid NOT NULL,expression_id text NOT NULL,artifact_id uuid NOT NULL,
            collection_id text NOT NULL,proposal_id uuid NOT NULL REFERENCES {s}.canonical_proposal(id),
            snapshot_hash text NOT NULL,profile_hash text NOT NULL,content_hash text NOT NULL,
            verification_revision bigint NOT NULL,actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            evidence_id uuid NOT NULL REFERENCES {p}.evidence(id),review_hash text NOT NULL CHECK(review_hash ~ '^[a-f0-9]{{64}}$'),
            payload jsonb,created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            UNIQUE(id,expression_id,artifact_id,collection_id),
            FOREIGN KEY(binding_id,expression_id,artifact_id,collection_id) REFERENCES {s}.synthetic_expression_binding(id,expression_id,artifact_id,collection_id),
            FOREIGN KEY(collection_id,verification_revision) REFERENCES {p}.verification_policy(collection_id,revision),
            CHECK((state='erased')=(payload IS NULL)),
            CHECK(state='erased' OR coalesce(encode(sha256(convert_to(payload::text,'UTF8')),'hex')=review_hash AND octet_length(payload::text)<=65536,false))
        );
        CREATE TABLE {c}.approved_version (
            id text PRIMARY KEY CHECK(id ~ '^ver_[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'),
            expression_id text NOT NULL,binding_id uuid NOT NULL,artifact_id uuid NOT NULL,collection_id text NOT NULL,
            review_id uuid NOT NULL,rights_revision bigint NOT NULL,verification_revision bigint NOT NULL,
            snapshot_hash text NOT NULL,profile_hash text NOT NULL,content_hash text NOT NULL,
            serialization_profile_id text NOT NULL DEFAULT 'OA-text-1' CHECK(serialization_profile_id='OA-text-1'),
            signature_hash text NOT NULL,canonical_bytes bytea,payload jsonb,
            size_bytes bigint NOT NULL CHECK(size_bytes BETWEEN 1 AND 131072),
            prior_version_id text REFERENCES {c}.approved_version(id),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            review_state text NOT NULL DEFAULT 'approved' CHECK(review_state='approved'),
            publication_eligible boolean NOT NULL DEFAULT false CHECK(NOT publication_eligible),
            UNIQUE(id,artifact_id,expression_id),UNIQUE(id,expression_id),
            FOREIGN KEY(binding_id,expression_id,artifact_id,collection_id) REFERENCES {s}.synthetic_expression_binding(id,expression_id,artifact_id,collection_id),
            FOREIGN KEY(review_id,expression_id,artifact_id,collection_id) REFERENCES {s}.snapshot_review(id,expression_id,artifact_id,collection_id),
            FOREIGN KEY(collection_id,rights_revision) REFERENCES {p}.collection_decision(collection_id,revision),
            FOREIGN KEY(collection_id,verification_revision) REFERENCES {p}.verification_policy(collection_id,revision),
            CHECK((state='erased')=(canonical_bytes IS NULL AND payload IS NULL)),
            CHECK(state='erased' OR coalesce(octet_length(canonical_bytes)=size_bytes AND encode(sha256(canonical_bytes),'hex')=content_hash AND payload IS NOT NULL,false))
        );
        CREATE TABLE {c}.representation_revision (
            id text PRIMARY KEY CHECK(id ~ '^prp_[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'),
            version_id text NOT NULL,artifact_id uuid NOT NULL,expression_id text NOT NULL,
            revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 32),parent_rep_id text,
            review_id uuid NOT NULL REFERENCES {s}.snapshot_review(id),
            snapshot_hash text NOT NULL,profile_hash text NOT NULL,record_set_hash text NOT NULL,
            payload jsonb,created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            UNIQUE(id,version_id),UNIQUE(version_id,revision),
            FOREIGN KEY(version_id,artifact_id,expression_id) REFERENCES {c}.approved_version(id,artifact_id,expression_id),
            FOREIGN KEY(parent_rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id),
            CHECK((revision=1)=(parent_rep_id IS NULL)),CHECK((state='erased')=(payload IS NULL)),
            CHECK(state='erased' OR coalesce(encode(sha256(convert_to(payload::text,'UTF8')),'hex')=record_set_hash AND octet_length(payload::text)<=262144,false))
        );
        CREATE TABLE {c}.version_head (
            expression_id text PRIMARY KEY REFERENCES {b}.identity_expressions(id),version_id text NOT NULL,
            representation_id text NOT NULL,
            FOREIGN KEY(version_id,expression_id) REFERENCES {c}.approved_version(id,expression_id) DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(representation_id,version_id) REFERENCES {c}.representation_revision(id,version_id)
        );
        CREATE TABLE {c}.representation_head (
            version_id text PRIMARY KEY REFERENCES {c}.approved_version(id),rep_id text NOT NULL,revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 32),
            FOREIGN KEY(rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id)
        );
        CREATE TABLE {c}.version_node (
            id text PRIMARY KEY CHECK(id ~ '^nod_[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'),
            version_id text NOT NULL REFERENCES {c}.approved_version(id),local_key text NOT NULL,
            parent_id text,node_kind text NOT NULL CHECK(node_kind IN ('heading','paragraph','list_item','table_cell','footnote','table','notice','footnote_group')),
            candidate_id uuid REFERENCES {s}.adapter_candidate(id),start_byte bigint,end_byte bigint,span_hash text,
            unavailable_reason text,
            UNIQUE(version_id,local_key),UNIQUE(version_id,candidate_id),UNIQUE(id,version_id),
            CHECK((node_kind IN ('heading','paragraph','list_item','table_cell','footnote'))=(candidate_id IS NOT NULL)),
            FOREIGN KEY(parent_id,version_id) REFERENCES {c}.version_node(id,version_id) DEFERRABLE INITIALLY DEFERRED,
            CHECK(coalesce((start_byte IS NULL AND end_byte IS NULL AND span_hash IS NULL AND unavailable_reason='empty_source_content') OR
                (start_byte>=0 AND end_byte>start_byte AND span_hash ~ '^[a-f0-9]{{64}}$' AND unavailable_reason IS NULL),false))
        );
        CREATE TABLE {c}.node_alignment (
            node_id text PRIMARY KEY,version_id text NOT NULL,candidate_id uuid NOT NULL REFERENCES {s}.adapter_candidate(id),
            input_artifact_hash text NOT NULL,source_candidate_hash text NOT NULL,profile_hash text NOT NULL,
            source_unit text NOT NULL DEFAULT 'utf8_byte' CHECK(source_unit='utf8_byte'),
            source_start bigint NOT NULL DEFAULT 0 CHECK(source_start=0),source_end bigint NOT NULL CHECK(source_end>0),
            target_start bigint NOT NULL CHECK(target_start>=0),target_end bigint NOT NULL CHECK(target_end>target_start),
            operations text[] NOT NULL CHECK(operations <@ ARRAY['lf','nfc'] AND cardinality(operations)<=2),
            FOREIGN KEY(node_id,version_id) REFERENCES {c}.version_node(id,version_id)
        );
        CREATE TABLE {c}.approved_anchor (
            id text PRIMARY KEY CHECK(id ~ '^anc_[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'),
            version_id text NOT NULL,node_id text NOT NULL,start_byte bigint NOT NULL,end_byte bigint NOT NULL,
            span_hash text NOT NULL,profile_hash text NOT NULL,locator text NOT NULL,
            FOREIGN KEY(node_id,version_id) REFERENCES {c}.version_node(id,version_id),
            UNIQUE(version_id,start_byte,end_byte,profile_hash),CHECK(start_byte>=0 AND end_byte>start_byte)
        );
        CREATE TABLE {c}.approved_table (
            id uuid PRIMARY KEY,rep_id text NOT NULL,version_id text NOT NULL,node_id text NOT NULL,
            table_local_id text NOT NULL,row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 100),
            column_count integer NOT NULL CHECK(column_count BETWEEN 1 AND 100),
            header_semantics_status text NOT NULL DEFAULT 'unknown' CHECK(header_semantics_status='unknown'),
            quality_state text NOT NULL DEFAULT 'needs_review' CHECK(quality_state='needs_review'),
            UNIQUE(id,rep_id),UNIQUE(rep_id,table_local_id),
            FOREIGN KEY(rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id),
            FOREIGN KEY(node_id,version_id) REFERENCES {c}.version_node(id,version_id)
        );
        CREATE TABLE {c}.approved_cell (
            id uuid PRIMARY KEY,table_id uuid NOT NULL,rep_id text NOT NULL,
            row_index integer NOT NULL CHECK(row_index>=0),column_index integer NOT NULL CHECK(column_index>=0),
            row_span integer NOT NULL CHECK(row_span>0),column_span integer NOT NULL CHECK(column_span>0),
            role text NOT NULL CHECK(role IN ('header','unknown')),content_state text NOT NULL CHECK(content_state IN ('present','empty')),
            UNIQUE(id,rep_id),UNIQUE(table_id,row_index,column_index),
            FOREIGN KEY(table_id,rep_id) REFERENCES {c}.approved_table(id,rep_id)
        );
        CREATE TABLE {c}.cell_node (
            cell_id uuid NOT NULL,rep_id text NOT NULL,node_id text NOT NULL,version_id text NOT NULL,position integer NOT NULL CHECK(position>=0),
            PRIMARY KEY(cell_id,position),UNIQUE(cell_id,node_id),
            FOREIGN KEY(cell_id,rep_id) REFERENCES {c}.approved_cell(id,rep_id),
            FOREIGN KEY(rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id),
            FOREIGN KEY(node_id,version_id) REFERENCES {c}.version_node(id,version_id)
        );
        CREATE TABLE {c}.document_order (
            rep_id text PRIMARY KEY,version_id text NOT NULL,position integer NOT NULL CHECK(position>=0),node_id text NOT NULL,
            FOREIGN KEY(rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id),
            FOREIGN KEY(node_id,version_id) REFERENCES {c}.version_node(id,version_id)
        );
        ALTER TABLE {c}.document_order DROP CONSTRAINT document_order_pkey;
        ALTER TABLE {c}.document_order ADD PRIMARY KEY(rep_id,position),ADD UNIQUE(rep_id,node_id);
        CREATE TABLE {c}.unavailable_mapping (
            rep_id text NOT NULL,version_id text NOT NULL,node_id text NOT NULL,
            quality text NOT NULL DEFAULT 'unavailable' CHECK(quality='unavailable'),
            reason text NOT NULL DEFAULT 'synthetic_fixture_has_no_physical_geometry' CHECK(reason='synthetic_fixture_has_no_physical_geometry'),
            PRIMARY KEY(rep_id,node_id),FOREIGN KEY(rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id),
            FOREIGN KEY(node_id,version_id) REFERENCES {c}.version_node(id,version_id)
        );
        CREATE TABLE {c}.footnote_reference (
            id uuid PRIMARY KEY,rep_id text NOT NULL,version_id text NOT NULL,target_node_id text NOT NULL,
            marker_start bigint NOT NULL,marker_end bigint NOT NULL,marker_hash text NOT NULL,
            FOREIGN KEY(rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id),
            FOREIGN KEY(target_node_id,version_id) REFERENCES {c}.version_node(id,version_id),
            UNIQUE(rep_id,marker_start,marker_end),CHECK(marker_start>=0 AND marker_end>marker_start)
        );
        CREATE TABLE {p}.approval_receipt (
            command_id uuid PRIMARY KEY,actor_id uuid NOT NULL REFERENCES {p}.actor(id),collection_id text NOT NULL,
            fingerprint text NOT NULL,result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT statement_timestamp()
        );
        CREATE TABLE {p}.approval_commit (
            expression_id text NOT NULL,snapshot_hash text NOT NULL,version_id text NOT NULL,rep_id text NOT NULL,
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),review_id uuid NOT NULL REFERENCES {s}.snapshot_review(id),
            rights_revision bigint NOT NULL,verification_revision bigint NOT NULL,
            PRIMARY KEY(expression_id,snapshot_hash),FOREIGN KEY(rep_id,version_id) REFERENCES {c}.representation_revision(id,version_id)
        );
    """)
    install_commit_guard(p, s, c)
    install_identity_guards(b, p, s)
    install_structure(p)
    install_functions(b, p, s, c, prefix)
    extend_lifecycle(p, s, c)
    for table in (
        roots() + children() + ("approval_receipt", "approval_commit", "version_head", "representation_head")
    ):
        schema = (
            p
            if table in ("approval_receipt", "approval_commit")
            else s
            if table in ("synthetic_expression_binding", "snapshot_review")
            else c
        )
        op.execute(
            f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY; CREATE POLICY private_guard ON {schema}.{table} USING(current_user='{prefix}_guard'); REVOKE ALL ON {schema}.{table} FROM PUBLIC"
        )
    for fn in functions() + helpers():
        op.execute(f"REVOKE ALL ON FUNCTION {p}.{fn} FROM PUBLIC")
    if guard_oid is not None:
        guard = quote(prefix + "_guard")
        op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {s},{c} TO {guard}")
        op.execute(
            f"GRANT SELECT ON {b}.identity_works,{b}.identity_expressions,{b}.identity_manifestations,{b}.identity_work_manifestations TO {guard}"
        )
        for table in ("snapshot_review",):
            op.execute(f"GRANT INSERT,UPDATE(state,payload) ON {s}.{table} TO {guard}")
        op.execute(f"GRANT UPDATE(state,payload) ON {s}.synthetic_expression_binding TO {guard}")
        op.execute(f"GRANT INSERT,UPDATE(state,payload,canonical_bytes) ON {c}.approved_version TO {guard}")
        op.execute(f"GRANT INSERT,UPDATE(state,payload) ON {c}.representation_revision TO {guard}")
        op.execute(f"GRANT INSERT,UPDATE ON {c}.version_head,{c}.representation_head TO {guard}")
        for table in children():
            op.execute(f"GRANT INSERT ON {c}.{table} TO {guard}")
        op.execute(f"GRANT INSERT ON {p}.approval_receipt,{p}.approval_commit TO {guard}")
        op.execute(f"GRANT CREATE ON SCHEMA {p} TO {guard}")
        for fn in functions() + helpers():
            op.execute(f"ALTER FUNCTION {p}.{fn} OWNER TO {guard}")
        op.execute(f"REVOKE CREATE ON SCHEMA {p} FROM {guard}")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {p}.apply_synthetic_approval(jsonb) TO {quote(prefix + '_review')},{quote(prefix + '_release')}"
        )
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {p}.read_approved_synthetic_version(text,text) TO {quote(prefix + '_acquisition')}"
        )


def roots():
    return ("synthetic_expression_binding", "snapshot_review", "approved_version", "representation_revision")


def children():
    return (
        "version_node",
        "node_alignment",
        "approved_anchor",
        "approved_table",
        "approved_cell",
        "cell_node",
        "document_order",
        "unavailable_mapping",
        "footnote_reference",
    )


def helpers():
    return (
        "staging_live_bytes(text)",
        "reserve_assembly_capacity(uuid,text,bigint,integer)",
        "transition_assembly(uuid,text,text)",
        "apply_assembly_command(jsonb)",
    )


def functions():
    return (
        "approval_leaf(jsonb,jsonb,text,text,text)",
        "approval_structure_scope(jsonb,jsonb,jsonb,jsonb,text,text)",
        "approval_structure(jsonb,jsonb,jsonb)",
        "synthetic_binding_hash(uuid)",
        "binding_current(uuid)",
        "validate_synthetic_binding()",
        "approval_lifecycle()",
        "approval_policy_current(text,bigint,bigint)",
        "lock_synthetic_approval_policy(text,bigint,bigint)",
        "approval_source(uuid,text,text,boolean)",
        "approved_version_current(text,text)",
        "hold_stale_approval(uuid)",
        "apply_synthetic_approval(jsonb)",
        "read_approved_synthetic_version(text,text)",
        "validate_approved_span()",
        "validate_approved_cell()",
        "bound_identity_immutable()",
        "validate_approved_tree()",
        "validate_approved_grid()",
        "validate_node_alignment()",
        "validate_approval_commit()",
    )


def install_structure(p):
    op.execute(f"""
        CREATE FUNCTION {p}.approval_leaf(st jsonb,inputs jsonb,cid text,parent_key text,kind text) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE source jsonb; value text; begin_byte integer; finish_byte integer; node jsonb;
        BEGIN
            source:=inputs->'source'->cid;value:={p}.oa_norm(source->>'text');
            begin_byte:=(st->>'offset')::integer;finish_byte:=begin_byte+octet_length(value);
            node:=jsonb_build_object('key',cid,'parent',parent_key,'kind',kind,'candidate_id',cid,
                'start',CASE WHEN value<>'' THEN begin_byte END,'end',CASE WHEN value<>'' THEN finish_byte END,
                'span_hash',CASE WHEN value<>'' THEN encode(sha256(convert_to(value,'UTF8')),'hex') END,
                'unavailable_reason',CASE WHEN value='' THEN 'empty_source_content' END);
            st:=jsonb_set(st,'{{nodes}}',(st->'nodes')||jsonb_build_array(node));
            st:=jsonb_set(st,'{{order}}',(st->'order')||to_jsonb(cid));
            RETURN jsonb_set(st,'{{offset}}',to_jsonb(finish_byte));
        END $$;
        CREATE FUNCTION {p}.approval_structure_scope(st jsonb,inputs jsonb,body jsonb,projection jsonb,scope text,parent_key text) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE item jsonb; cell jsonb; source jsonb; origins jsonb; shape jsonb; id_value jsonb;
            nodes jsonb; grid jsonb; table_key text; key text; start_value integer; n integer:=0; row_value integer; col_value integer; child integer;
            foot jsonb; foot_key text; foot_index integer:=0; ordered_footnotes jsonb;
        BEGIN
            FOR item IN SELECT * FROM jsonb_array_elements(body->'blocks') LOOP
                IF n>0 THEN st:=jsonb_set(st,'{{offset}}',to_jsonb((st->>'offset')::integer+2)); END IF;
                start_value:=(st->>'offset')::integer;
                IF item->>'kind'='text' THEN st:={p}.approval_leaf(st,inputs,item->>'candidate_id',parent_key,(inputs->'source'->(item->>'candidate_id'))->>'block_type');
                ELSIF item->>'kind'='notice' THEN
                    key:='notice:'||(item->>'local_id');
                    st:={p}.approval_structure_scope(st,inputs,item,projection,item->>'local_id',key);
                    st:=jsonb_set(st,'{{nodes}}',(st->'nodes')||jsonb_build_array(jsonb_build_object('key',key,'parent',parent_key,'kind','notice','candidate_id',NULL,'start',start_value,'end',(st->>'offset')::integer)));
                ELSE
                    table_key:='table:'||(item->>'table_local_id'); origins:='{{}}';grid:='[]';
                    FOR cell IN SELECT * FROM jsonb_array_elements(item->'cells') LOOP
                        shape:=inputs->'source'->(cell->'candidates'->>0)->'cell';
                        key:=(shape->>'row')||':'||(shape->>'column');origins:=jsonb_set(origins,ARRAY[key],cell);
                        grid:=grid||jsonb_build_array(jsonb_build_object('row',(shape->>'row')::integer,'column',(shape->>'column')::integer,
                            'row_span',(shape->>'row_span')::integer,'column_span',(shape->>'column_span')::integer,
                            'candidates',cell->'candidates','content_state',inputs->'source'->(cell->'candidates'->>0)->>'content_state'));
                    END LOOP;
                    FOR row_value IN 0..(item->>'rows')::integer-1 LOOP
                        IF row_value>0 THEN st:=jsonb_set(st,'{{offset}}',to_jsonb((st->>'offset')::integer+1)); END IF;
                        FOR col_value IN 0..(item->>'columns')::integer-1 LOOP
                            IF col_value>0 THEN st:=jsonb_set(st,'{{offset}}',to_jsonb((st->>'offset')::integer+1)); END IF;
                            cell:=origins->(row_value::text||':'||col_value::text); child:=0;
                            IF cell IS NOT NULL THEN
                                FOR id_value IN SELECT * FROM jsonb_array_elements(cell->'candidates') LOOP
                                    IF child>0 THEN st:=jsonb_set(st,'{{offset}}',to_jsonb((st->>'offset')::integer+2)); END IF;
                                    st:={p}.approval_leaf(st,inputs,id_value#>>'{{}}',table_key,'table_cell');child:=child+1;
                                END LOOP;
                            END IF;
                        END LOOP;
                    END LOOP;
                    st:=jsonb_set(st,'{{nodes}}',(st->'nodes')||jsonb_build_array(jsonb_build_object('key',table_key,'parent',parent_key,'kind','table','candidate_id',NULL,'start',start_value,'end',(st->>'offset')::integer)));
                    st:=jsonb_set(st,'{{tables}}',(st->'tables')||jsonb_build_array(jsonb_build_object('key',table_key,'table_local_id',item->>'table_local_id',
                        'rows',(item->>'rows')::integer,'columns',(item->>'columns')::integer,'cells',grid,'headers',coalesce(item->'headers','[]'::jsonb))));
                END IF;
                n:=n+1;
            END LOOP;
            -- The independently checked projection establishes actual footnote emission order.
            SELECT coalesce(jsonb_agg(f.v ORDER BY emitted.ord),'[]') INTO ordered_footnotes
            FROM jsonb_array_elements(coalesce(body->'footnotes','[]')) f(v)
                JOIN jsonb_array_elements(projection->'normalizations') WITH ORDINALITY emitted(v,ord)
                    ON emitted.v->'candidate_id'=f.v->'candidates'->0;
            FOR foot IN SELECT * FROM jsonb_array_elements(ordered_footnotes) LOOP
                IF n>0 OR foot_index>0 THEN st:=jsonb_set(st,'{{offset}}',to_jsonb((st->>'offset')::integer+2)); END IF;
                start_value:=(st->>'offset')::integer;foot_key:='foot:'||coalesce(scope,'document')||':'||(foot->>'local_id');child:=0;
                FOR id_value IN SELECT * FROM jsonb_array_elements(foot->'candidates') LOOP
                    IF child>0 THEN st:=jsonb_set(st,'{{offset}}',to_jsonb((st->>'offset')::integer+2)); END IF;
                    st:={p}.approval_leaf(st,inputs,id_value#>>'{{}}',foot_key,'footnote');child:=child+1;
                END LOOP;
                st:=jsonb_set(st,'{{nodes}}',(st->'nodes')||jsonb_build_array(jsonb_build_object('key',foot_key,'parent',parent_key,'kind','footnote_group','candidate_id',NULL,'start',start_value,'end',(st->>'offset')::integer)));
                foot_index:=foot_index+1;
            END LOOP;
            RETURN st;
        END $$;
        CREATE FUNCTION {p}.approval_structure(inputs jsonb,plan jsonb,projection jsonb) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE st jsonb;
        BEGIN
            st:={p}.approval_structure_scope(jsonb_build_object('offset',0,'nodes','[]'::jsonb,'tables','[]'::jsonb,'order','[]'::jsonb),inputs,plan,projection,NULL,NULL);
            IF jsonb_array_length(st->'nodes')>200 THEN RAISE EXCEPTION 'Approved node bound exceeded' USING ERRCODE='54000'; END IF;
            RETURN st||jsonb_build_object('markers',projection->'markers','normalizations',projection->'normalizations','separators',projection->'separators');
        END $$;
    """)


def install_functions(b, p, s, c, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.synthetic_binding_hash(bid uuid) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT encode(sha256(convert_to((to_jsonb(b)-ARRAY['payload','state'])::text,'UTF8')),'hex') FROM {s}.synthetic_expression_binding b WHERE b.id=bid
        $$;
        CREATE FUNCTION {p}.binding_current(bid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {s}.synthetic_expression_binding b JOIN {b}.identity_expressions x ON x.id=b.expression_id
                JOIN {b}.identity_works w ON w.id=x.work_id JOIN {b}.identity_manifestations m ON m.id=b.manifestation_id
                WHERE b.id=bid AND b.state='active' AND w.identity_status='identified'
                AND b.identity_hash=encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(x),'manifestation',to_jsonb(m))::text,'UTF8')),'hex'))
        $$;
        CREATE FUNCTION {p}.validate_synthetic_binding() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
        DECLARE raw {s}.staged_artifact;
        BEGIN
            SELECT a.* INTO raw FROM {s}.staged_artifact a WHERE a.id=NEW.artifact_id FOR UPDATE;
            PERFORM {b}.identity_check_legacy_authority(true);
            IF raw.id IS NULL OR raw.state NOT IN ('staged','processing','validated') OR raw.retention_deadline<=statement_timestamp()
                OR NOT EXISTS(SELECT 1 FROM {p}.evidence e WHERE e.id=NEW.evidence_id AND e.collection_id=NEW.collection_id)
                OR NEW.state<>'active' THEN RAISE EXCEPTION 'Current synthetic identity declaration required' USING ERRCODE='23514'; END IF;
            PERFORM 1 FROM {b}.identity_expressions x JOIN {b}.identity_works w ON w.id=x.work_id
                JOIN {b}.identity_manifestations m ON m.id=NEW.manifestation_id
                WHERE x.id=NEW.expression_id FOR SHARE OF x,w,m;
            IF NOT EXISTS(SELECT 1 FROM {b}.identity_expressions x JOIN {b}.identity_works w ON w.id=x.work_id
                JOIN {b}.identity_manifestations m ON m.id=NEW.manifestation_id WHERE x.id=NEW.expression_id
                AND w.identity_status='identified' AND NEW.identity_hash=encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(x),'manifestation',to_jsonb(m))::text,'UTF8')),'hex')) THEN RAISE EXCEPTION 'Exact declared identity facts required' USING ERRCODE='23514'; END IF;
            NEW.payload:=jsonb_build_object('binding_id',NEW.id,'identity_hash',NEW.identity_hash,'expression_id',NEW.expression_id,
                'manifestation_id',NEW.manifestation_id,'artifact_hash',NEW.artifact_hash);
            PERFORM {p}.reserve_assembly_capacity(NEW.artifact_id,NEW.collection_id,octet_length(NEW.payload::text),1);
            RETURN NEW;
        END $$;
        CREATE TRIGGER declaration_insert BEFORE INSERT ON {s}.synthetic_expression_binding FOR EACH ROW EXECUTE FUNCTION {p}.validate_synthetic_binding();
        CREATE FUNCTION {p}.approval_lifecycle() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF TG_OP<>'UPDATE' OR (to_jsonb(NEW)-ARRAY['state','payload','canonical_bytes']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','payload','canonical_bytes'])
                OR ((to_jsonb(NEW)->'payload',to_jsonb(NEW)->'canonical_bytes') IS DISTINCT FROM (to_jsonb(OLD)->'payload',to_jsonb(OLD)->'canonical_bytes')
                    AND NOT(NEW.state='erased' AND NEW.payload IS NULL AND coalesce(to_jsonb(NEW)->'canonical_bytes','null')='null'))
                OR NOT((NEW.state='held' AND OLD.state='active') OR (NEW.state='erasure_required' AND OLD.state IN ('active','held')) OR (NEW.state='erased' AND OLD.state IN ('active','held','erasure_required')))
                OR NOT EXISTS(SELECT 1 FROM {p}.assembly_lifecycle_event e WHERE e.artifact_id=NEW.artifact_id AND e.target=NEW.state AND e.actor_id={p}.current_actor() AND e.transaction_id=pg_current_xact_id())
                THEN RAISE EXCEPTION 'Immutable audited approval lifecycle required' USING ERRCODE='55000'; END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION {p}.approval_policy_current(cid text,rights_rev bigint,verification_rev bigint) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {p}.collection_decision d JOIN {p}.verification_policy v USING(collection_id)
                JOIN {p}.revision_head dh ON dh.kind='collection' AND dh.scope_id=cid AND dh.revision=d.revision
                JOIN {p}.revision_head vh ON vh.kind='verification' AND vh.scope_id=cid AND vh.revision=v.revision
                JOIN {p}.actor da ON da.id=d.actor_id AND da.active JOIN {p}.actor va ON va.id=v.actor_id AND va.active
                WHERE d.collection_id=cid AND d.revision=rights_rev AND v.revision=verification_rev AND d.actor_id<>v.actor_id
                AND d.state='approved' AND v.state='approved' AND d.valid_from<=statement_timestamp() AND d.expires_at>statement_timestamp()
                AND v.valid_from<=statement_timestamp() AND v.expires_at>statement_timestamp()
                AND EXISTS(SELECT 1 FROM {p}.assignment WHERE actor_id=da.id AND collection_id=cid AND capability='rights')
                AND EXISTS(SELECT 1 FROM {p}.assignment WHERE actor_id=va.id AND collection_id=cid AND capability='content')
                AND d.metadata_privacy_class='no_personal_data' AND d.privacy_review_id IS NULL
                AND d.basis_type<>'unknown' AND d.conditions_satisfied AND d.redistribute_text='allow' AND d.quote='allow' AND d.derive='allow'
                AND (SELECT count(*) FROM {p}.verification_material m WHERE m.collection_id=cid AND m.revision=v.revision)=6)
        $$;
        CREATE FUNCTION {p}.lock_synthetic_approval_policy(cid text,rights_rev bigint,verification_rev bigint) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            -- Lock reviewer actors before heads, matching command/revocation order.
            PERFORM 1 FROM {p}.actor a WHERE a.id IN (SELECT actor_id FROM {p}.collection_decision WHERE collection_id=cid AND revision=rights_rev UNION SELECT actor_id FROM {p}.verification_policy WHERE collection_id=cid AND revision=verification_rev) ORDER BY a.id FOR SHARE;
            PERFORM 1 FROM {p}.revision_head WHERE scope_id=cid AND kind IN ('collection','verification') ORDER BY kind FOR SHARE;
            IF NOT {p}.approval_policy_current(cid,rights_rev,verification_rev) THEN RAISE EXCEPTION 'Independent exact current collection reviews required' USING ERRCODE='42501'; END IF;
        END $$;
        CREATE FUNCTION {p}.approval_source(pid uuid,cid text,purpose_value text,write boolean) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE proposal {s}.canonical_proposal;inputs jsonb;compiled jsonb;raw {s}.staged_artifact;
        BEGIN
            SELECT id,artifact_id,snapshot_id INTO proposal.id,proposal.artifact_id,proposal.snapshot_id FROM {s}.canonical_proposal WHERE id=pid;
            IF proposal.id IS NULL THEN RAISE EXCEPTION 'Current private proposal required' USING ERRCODE='42501'; END IF;
            raw:={p}.lock_assembly_input(proposal.artifact_id,cid,purpose_value,write);
            PERFORM 1 FROM {s}.snapshot_head WHERE id=proposal.snapshot_id FOR SHARE;
            IF NOT {p}.canonical_current(pid,purpose_value) OR NOT EXISTS(SELECT 1 FROM {p}.acquisition_assessment a WHERE a.id=raw.assessment_id AND a.revision=raw.assessment_revision AND a.privacy_class='no_personal_data' AND a.privacy_review_id IS NULL) THEN
                RAISE EXCEPTION 'Current complete nonpersonal synthetic proposal required' USING ERRCODE='42501'; END IF;
            SELECT * INTO proposal FROM {s}.canonical_proposal WHERE id=pid FOR SHARE;
            SELECT jsonb_build_object('source',jsonb_object_agg(d.id::text,d.payload),'order',t.payload->'order') INTO inputs
                FROM {s}.staging_snapshot t JOIN {s}.snapshot_selection sel ON sel.snapshot_id=t.id AND sel.revision=t.revision
                JOIN {s}.adapter_candidate d ON d.id=sel.candidate_id WHERE t.id=proposal.snapshot_id AND t.revision=proposal.snapshot_revision GROUP BY t.payload;
            compiled:={p}.oa_compile(inputs,proposal.payload->'plan');
            IF convert_to(compiled->>'text','UTF8') IS DISTINCT FROM proposal.canonical_bytes OR compiled->'projection' IS DISTINCT FROM proposal.payload->'projection'
                OR jsonb_array_length(compiled->'projection'->'incomplete_tables')<>0 OR octet_length(proposal.canonical_bytes)=0
                THEN RAISE EXCEPTION 'Incomplete or inconsistent proposal cannot be approved' USING ERRCODE='23514'; END IF;
            RETURN jsonb_build_object('proposal',to_jsonb(proposal),'inputs',inputs,'structure',{p}.approval_structure(inputs,proposal.payload->'plan',compiled->'projection'));
        END $$;
        CREATE FUNCTION {p}.approved_version_current(vid text,purpose_value text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {c}.approved_version v JOIN {c}.representation_head h ON h.version_id=v.id
                JOIN {c}.representation_revision r ON r.id=h.rep_id JOIN {s}.snapshot_review review ON review.id=r.review_id
                WHERE v.id=vid AND v.state='active' AND r.state='active' AND {p}.binding_current(v.binding_id)
                AND {p}.assembly_input_current(v.artifact_id,purpose_value) AND {p}.approval_policy_current(v.collection_id,v.rights_revision,v.verification_revision)
                AND review.verification_revision=v.verification_revision)
        $$;
        CREATE FUNCTION {p}.validate_approved_span() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE bytes bytea;begin_byte bigint;finish_byte bigint;
        BEGIN
            SELECT canonical_bytes INTO bytes FROM {c}.approved_version WHERE id=NEW.version_id;
            IF TG_TABLE_NAME='version_node' THEN
            IF NEW.candidate_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM {s}.adapter_candidate d JOIN {c}.approved_version v ON v.artifact_id=d.artifact_id
                JOIN {s}.snapshot_review r ON r.id=v.review_id JOIN {s}.canonical_proposal cp ON cp.id=r.proposal_id
                JOIN {s}.snapshot_selection sel ON sel.snapshot_id=cp.snapshot_id AND sel.revision=cp.snapshot_revision AND sel.candidate_id=d.id
                WHERE v.id=NEW.version_id AND d.id=NEW.candidate_id) THEN RAISE EXCEPTION 'Exact selected source candidate required' USING ERRCODE='23514'; END IF;
            IF NEW.candidate_id IS NOT NULL AND EXISTS(SELECT 1 FROM {s}.adapter_candidate d WHERE d.id=NEW.candidate_id AND d.payload->>'text'<>'')
                AND NOT EXISTS(SELECT 1 FROM {s}.adapter_candidate d WHERE d.id=NEW.candidate_id
                    AND convert_to({p}.oa_norm(d.payload->>'text'),'UTF8')=substring(bytes FROM NEW.start_byte::integer+1 FOR (NEW.end_byte-NEW.start_byte)::integer)) THEN
                RAISE EXCEPTION 'Exact normalized source leaf range required' USING ERRCODE='23514'; END IF;
            END IF;
            IF TG_TABLE_NAME='version_node' THEN begin_byte:=NEW.start_byte;finish_byte:=NEW.end_byte;
            ELSIF TG_TABLE_NAME='approved_anchor' THEN begin_byte:=NEW.start_byte;finish_byte:=NEW.end_byte;
            ELSE begin_byte:=NEW.marker_start;finish_byte:=NEW.marker_end; END IF;
            IF begin_byte IS NULL AND TG_TABLE_NAME='version_node' THEN RETURN NEW; END IF;
            IF bytes IS NULL OR begin_byte<0 OR finish_byte<=begin_byte OR finish_byte>octet_length(bytes) THEN RAISE EXCEPTION 'Invalid exact version range' USING ERRCODE='23514'; END IF;
            PERFORM convert_from(substring(bytes FROM 1 FOR begin_byte::integer),'UTF8');PERFORM convert_from(substring(bytes FROM 1 FOR finish_byte::integer),'UTF8');
            IF TG_TABLE_NAME='footnote_reference' THEN
                IF NEW.marker_hash<>encode(sha256(substring(bytes FROM begin_byte::integer+1 FOR (finish_byte-begin_byte)::integer)),'hex') THEN RAISE EXCEPTION 'Invalid marker hash' USING ERRCODE='23514'; END IF;
            ELSIF NEW.span_hash<>encode(sha256(substring(bytes FROM begin_byte::integer+1 FOR (finish_byte-begin_byte)::integer)),'hex') THEN RAISE EXCEPTION 'Invalid span hash' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION {p}.validate_approved_cell() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE t {c}.approved_table;
        BEGIN
            SELECT * INTO t FROM {c}.approved_table WHERE id=NEW.table_id;
            IF NEW.row_index+NEW.row_span>t.row_count OR NEW.column_index+NEW.column_span>t.column_count
                OR EXISTS(SELECT 1 FROM {c}.approved_cell a WHERE a.table_id=t.id AND a.row_index<NEW.row_index+NEW.row_span AND a.row_index+a.row_span>NEW.row_index AND a.column_index<NEW.column_index+NEW.column_span AND a.column_index+a.column_span>NEW.column_index)
                THEN RAISE EXCEPTION 'Invalid or overlapping approved cell' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;
    """)
    install_record_guards(p, s, c)
    install_commands(b, p, s, c, prefix)
    install_materializer(p, s, c)
    for table in roots():
        schema = s if table in ("synthetic_expression_binding", "snapshot_review") else c
        op.execute(
            f"CREATE TRIGGER lifecycle_update BEFORE UPDATE OR DELETE ON {schema}.{table} FOR EACH ROW EXECUTE FUNCTION {p}.approval_lifecycle(); CREATE TRIGGER no_truncate BEFORE TRUNCATE ON {schema}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record()"
        )
    for table in children() + ("approval_receipt", "approval_commit"):
        schema = p if table in ("approval_receipt", "approval_commit") else c
        op.execute(
            f"CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {schema}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record()"
        )
    for table in ("version_node", "approved_anchor", "footnote_reference"):
        op.execute(
            f"CREATE TRIGGER exact_span BEFORE INSERT ON {c}.{table} FOR EACH ROW EXECUTE FUNCTION {p}.validate_approved_span()"
        )
    op.execute(
        f"CREATE TRIGGER exact_cell BEFORE INSERT ON {c}.approved_cell FOR EACH ROW EXECUTE FUNCTION {p}.validate_approved_cell()"
    )


def install_commands(b, p, s, c, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.apply_synthetic_approval(command jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE operator_id uuid;cid text;cmd uuid;fingerprint text;old {p}.approval_receipt;result jsonb;source jsonb;proposal jsonb;structure jsonb;
            binding {s}.synthetic_expression_binding;review {s}.snapshot_review;verification {p}.verification_policy;
            prior {c}.approved_version;head {c}.version_head;rep_head {c}.representation_head;committed {p}.approval_commit;
            vid text;rid text;rev bigint;sig text;bytes bytea;node jsonb;table_value jsonb;cell jsonb;marker jsonb;id_value jsonb;
            node_ids jsonb:='{{}}';anchor_ids jsonb:='{{}}';node_id text;parent_id text;anchor_id text;table_id uuid;cell_id uuid;position_value integer;
            record_set jsonb;review_digest text;
        BEGIN
            IF command IS NULL OR octet_length(command::text)>65536 THEN RAISE EXCEPTION 'Bounded private approval required' USING ERRCODE='22023'; END IF;
            cid:=command->>'collection_id';cmd:=(command->>'command_id')::uuid;operator_id:={p}.current_actor();
            PERFORM 1 FROM {p}.actor WHERE id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {p}.is_native_operator() THEN RAISE EXCEPTION 'Native synthetic session required' USING ERRCODE='42501'; END IF;
            IF jsonb_typeof(command->'reason') IS DISTINCT FROM 'string' OR length(btrim(command->>'reason')) NOT BETWEEN 1 AND 2048
                OR jsonb_typeof(command->'purpose') IS DISTINCT FROM 'string' OR length(btrim(command->>'purpose')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Explicit bounded purpose/reason required' USING ERRCODE='22023'; END IF;
            IF command->>'action'='record_snapshot_review' THEN
                IF NOT {p}.operator_keys(command,ARRAY['command_id','collection_id','purpose','reason','action','review_id','binding_id','binding_hash','proposal_id','snapshot_hash','profile_hash','content_hash','verification_revision','evidence_id','reviewed_candidate_ids','comparison_method','review_scope','requires_exception_review'],ARRAY[]::text[])
                    OR command->>'comparison_method' IS DISTINCT FROM 'human_source_comparison' OR command->>'review_scope' IS DISTINCT FROM 'collection_policy'
                    OR command->'requires_exception_review' IS DISTINCT FROM 'false'::jsonb THEN RAISE EXCEPTION 'Unsupported approval scope' USING ERRCODE='22023'; END IF;
                IF NOT {p}.assigned(cid,'content') THEN RAISE EXCEPTION 'Scoped content comparison required' USING ERRCODE='42501'; END IF;
                source:={p}.approval_source((command->>'proposal_id')::uuid,cid,command->>'purpose',true);proposal:=source->'proposal';
                SELECT * INTO binding FROM {s}.synthetic_expression_binding WHERE id=(command->>'binding_id')::uuid FOR SHARE;
                IF binding.artifact_id IS DISTINCT FROM (proposal->>'artifact_id')::uuid OR binding.collection_id IS DISTINCT FROM cid
                    OR NOT {p}.binding_current(binding.id) OR {p}.synthetic_binding_hash(binding.id) IS DISTINCT FROM command->>'binding_hash'
                    OR proposal->>'snapshot_hash' IS DISTINCT FROM command->>'snapshot_hash' OR proposal->>'profile_hash' IS DISTINCT FROM command->>'profile_hash'
                    OR proposal->>'content_hash' IS DISTINCT FROM command->>'content_hash' THEN RAISE EXCEPTION 'Exact identity/snapshot/profile/content comparison required' USING ERRCODE='42501'; END IF;
                PERFORM 1 FROM {p}.revision_head WHERE kind='verification' AND scope_id=cid FOR SHARE;
                SELECT * INTO verification FROM {p}.verification_policy WHERE collection_id=cid AND revision=(command->>'verification_revision')::bigint;
                IF verification.actor_id IS DISTINCT FROM operator_id OR verification.state<>'approved' OR verification.valid_from>statement_timestamp() OR verification.expires_at<=statement_timestamp()
                    OR NOT EXISTS(SELECT 1 FROM {p}.revision_head WHERE kind='verification' AND scope_id=cid AND revision=verification.revision)
                    OR (SELECT count(*) FROM {p}.verification_material WHERE collection_id=cid AND revision=verification.revision)<>6
                    OR NOT EXISTS(SELECT 1 FROM {p}.evidence WHERE id=(command->>'evidence_id')::uuid AND collection_id=cid)
                    THEN RAISE EXCEPTION 'Current exact collection content policy and evidence required' USING ERRCODE='42501'; END IF;
                IF jsonb_typeof(command->'reviewed_candidate_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(command->'reviewed_candidate_ids') NOT BETWEEN 1 AND 50
                    OR jsonb_array_length(command->'reviewed_candidate_ids')<>jsonb_array_length(source->'inputs'->'order')
                    OR (SELECT count(DISTINCT x) FROM jsonb_array_elements_text(command->'reviewed_candidate_ids') x)<>jsonb_array_length(command->'reviewed_candidate_ids')
                    OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(source->'inputs'->'order') x WHERE NOT(command->'reviewed_candidate_ids' ? x))
                    THEN RAISE EXCEPTION 'Every selected leaf and exclusion requires source comparison' USING ERRCODE='23514'; END IF;
            ELSIF command->>'action'='approve_synthetic_version' THEN
                IF NOT {p}.operator_keys(command,ARRAY['command_id','collection_id','purpose','reason','action','review_id','review_hash','rights_revision','verification_revision','version_id','representation_id','mode'],ARRAY['prior_version_id','expected_rep_revision'])
                    OR coalesce(command->>'mode','') NOT IN ('new_version','representation_revision')
                    OR command->>'version_id' !~ '^ver_[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'
                    OR command->>'representation_id' !~ '^prp_[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'
                    THEN RAISE EXCEPTION 'Invalid private approval command' USING ERRCODE='22023'; END IF;
                IF NOT {p}.assigned(cid,'release') THEN RAISE EXCEPTION 'Scoped release maintainer required' USING ERRCODE='42501'; END IF;
                SELECT id,proposal_id,artifact_id INTO review.id,review.proposal_id,review.artifact_id FROM {s}.snapshot_review WHERE id=(command->>'review_id')::uuid;
                source:={p}.approval_source(review.proposal_id,cid,command->>'purpose',true);proposal:=source->'proposal';structure:=source->'structure';
                SELECT * INTO review FROM {s}.snapshot_review WHERE id=review.id FOR SHARE;
                SELECT * INTO binding FROM {s}.synthetic_expression_binding WHERE id=review.binding_id FOR SHARE;
                IF review.state IS DISTINCT FROM 'active' OR review.collection_id IS DISTINCT FROM cid OR review.review_hash IS DISTINCT FROM command->>'review_hash'
                    OR review.snapshot_hash IS DISTINCT FROM proposal->>'snapshot_hash' OR review.profile_hash IS DISTINCT FROM proposal->>'profile_hash'
                    OR review.content_hash IS DISTINCT FROM proposal->>'content_hash' OR NOT {p}.binding_current(binding.id)
                    OR review.payload->>'binding_hash' IS DISTINCT FROM {p}.synthetic_binding_hash(binding.id)
                    OR review.verification_revision IS DISTINCT FROM (command->>'verification_revision')::bigint THEN RAISE EXCEPTION 'Current exact reviewed snapshot required' USING ERRCODE='42501'; END IF;
                PERFORM {p}.lock_synthetic_approval_policy(cid,(command->>'rights_revision')::bigint,(command->>'verification_revision')::bigint);
                IF review.actor_id IS DISTINCT FROM (SELECT actor_id FROM {p}.verification_policy WHERE collection_id=cid AND revision=review.verification_revision) THEN RAISE EXCEPTION 'Exact accountable content operator_id required' USING ERRCODE='42501'; END IF;
                -- Expression lock serializes first mint as well as revisions; no absent-row race.
                PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:approve-expression:'||binding.expression_id,0));
            ELSE RAISE EXCEPTION 'Unsupported private approval action' USING ERRCODE='22023'; END IF;
            fingerprint:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:approval-command:'||cmd::text,0));
            SELECT * INTO old FROM {p}.approval_receipt WHERE command_id=cmd;
            IF old.command_id IS NOT NULL THEN
                IF old.actor_id<>operator_id THEN RAISE EXCEPTION 'Private receipt belongs to another operator_id' USING ERRCODE='42501'; END IF;
                IF old.fingerprint<>fingerprint THEN RAISE EXCEPTION 'Changed approval retry' USING ERRCODE='40001'; END IF;
                IF command->>'action'='approve_synthetic_version' AND NOT {p}.approved_version_current(old.result->>'version_id',command->>'purpose') THEN RAISE EXCEPTION 'Held version retry cannot resume' USING ERRCODE='42501'; END IF;
                RETURN old.result||jsonb_build_object('replayed',true);
            END IF;
            IF command->>'action'='record_snapshot_review' THEN
                review_digest:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
                PERFORM {p}.reserve_assembly_capacity(binding.artifact_id,cid,octet_length(command::text),1);
                INSERT INTO {s}.snapshot_review(id,command_id,binding_id,expression_id,artifact_id,collection_id,proposal_id,snapshot_hash,profile_hash,content_hash,
                    verification_revision,actor_id,evidence_id,review_hash,payload)
                    VALUES((command->>'review_id')::uuid,cmd,binding.id,binding.expression_id,binding.artifact_id,cid,(proposal->>'id')::uuid,proposal->>'snapshot_hash',proposal->>'profile_hash',proposal->>'content_hash',verification.revision,operator_id,(command->>'evidence_id')::uuid,review_digest,command);
                INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                    VALUES(operator_id,cid,(command->>'evidence_id')::uuid,'snapshot_source_compared',command->>'review_id',1,command->>'reason');
                result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'review_hash',review_digest);
            ELSE
                SELECT * INTO committed FROM {p}.approval_commit WHERE expression_id=binding.expression_id AND snapshot_hash=review.snapshot_hash;
                IF committed.expression_id IS NOT NULL THEN
                    IF committed.rights_revision<>(command->>'rights_revision')::bigint OR committed.verification_revision<>review.verification_revision
                        OR NOT {p}.approved_version_current(committed.version_id,command->>'purpose') THEN RAISE EXCEPTION 'Existing frozen approval requires forward review' USING ERRCODE='42501'; END IF;
                    result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',true,'version_id',committed.version_id,'representation_id',committed.rep_id,
                        'revision',(SELECT revision FROM {c}.representation_revision WHERE id=committed.rep_id),'content_hash',review.content_hash);
                ELSE
                    bytes:=(proposal->>'canonical_bytes')::bytea;
                    -- Span/node/order/grid identity excludes header associations and minted IDs.
                    SELECT jsonb_agg(t-ARRAY['headers']) INTO id_value FROM jsonb_array_elements(structure->'tables') t;
                    sig:=encode(sha256(convert_to(jsonb_build_object('nodes',structure->'nodes','order',structure->'order','grids',coalesce(id_value,'[]'))::text,'UTF8')),'hex');
                    SELECT * INTO head FROM {c}.version_head WHERE expression_id=binding.expression_id FOR UPDATE;
                    IF head.version_id IS DISTINCT FROM command->>'prior_version_id' THEN RAISE EXCEPTION 'Stale expression head' USING ERRCODE='40001'; END IF;
                    IF head.version_id IS NOT NULL THEN
                        SELECT * INTO prior FROM {c}.approved_version WHERE id=head.version_id;
                        SELECT * INTO rep_head FROM {c}.representation_head WHERE version_id=head.version_id FOR UPDATE;
                        IF rep_head.revision IS DISTINCT FROM coalesce((command->>'expected_rep_revision')::bigint,0) THEN RAISE EXCEPTION 'Stale representation head' USING ERRCODE='40001'; END IF;
                    ELSIF coalesce((command->>'expected_rep_revision')::bigint,0)<>0 THEN RAISE EXCEPTION 'Initial representation revision must be zero' USING ERRCODE='40001'; END IF;
                    rid:=command->>'representation_id';
                    IF command->>'mode'='representation_revision' THEN
                        IF prior.id IS NULL OR prior.id IS DISTINCT FROM command->>'version_id' OR prior.artifact_id<>binding.artifact_id
                            OR prior.content_hash<>review.content_hash OR prior.signature_hash<>sig OR NOT {p}.approved_version_current(prior.id,command->>'purpose')
                            THEN RAISE EXCEPTION 'Representation revision must preserve exact bytes nodes order and source' USING ERRCODE='23514'; END IF;
                        vid:=prior.id;rev:=rep_head.revision+1;
                        SELECT jsonb_object_agg(local_key,id) INTO node_ids FROM {c}.version_node WHERE version_id=vid;
                    ELSE
                        IF prior.id IS NOT NULL AND prior.content_hash=review.content_hash AND prior.signature_hash=sig THEN RAISE EXCEPTION 'Unchanged canonical identity requires representation revision' USING ERRCODE='23514'; END IF;
                        vid:=command->>'version_id';rev:=1;
                        PERFORM {p}.reserve_assembly_capacity(binding.artifact_id,cid,octet_length(bytes)+octet_length(jsonb_build_object('signature_hash',sig)::text),1);
                        INSERT INTO {c}.approved_version(id,expression_id,binding_id,artifact_id,collection_id,review_id,rights_revision,verification_revision,snapshot_hash,profile_hash,content_hash,signature_hash,canonical_bytes,payload,size_bytes,prior_version_id)
                            VALUES(vid,binding.expression_id,binding.id,binding.artifact_id,cid,review.id,(command->>'rights_revision')::bigint,review.verification_revision,review.snapshot_hash,review.profile_hash,review.content_hash,sig,bytes,jsonb_build_object('signature_hash',sig),octet_length(bytes),head.version_id);
                        FOR node IN SELECT * FROM jsonb_array_elements(structure->'nodes') LOOP
                            node_ids:=jsonb_set(node_ids,ARRAY[node->>'key'],to_jsonb('nod_'||gen_random_uuid()::text));
                        END LOOP;
                        FOR node IN SELECT * FROM jsonb_array_elements(structure->'nodes') LOOP
                            node_id:=node_ids->>(node->>'key');parent_id:=node_ids->>(node->>'parent');
                            IF node->>'end'=node->>'start' THEN node:=node||jsonb_build_object('start',NULL,'end',NULL,'unavailable_reason','empty_source_content'); END IF;
                            INSERT INTO {c}.version_node(id,version_id,local_key,parent_id,node_kind,candidate_id,start_byte,end_byte,span_hash,unavailable_reason)
                                VALUES(node_id,vid,node->>'key',parent_id,node->>'kind',(node->>'candidate_id')::uuid,(node->>'start')::bigint,(node->>'end')::bigint,
                                    CASE WHEN node->>'start' IS NOT NULL THEN encode(sha256(substring(bytes FROM (node->>'start')::integer+1 FOR (node->>'end')::integer-(node->>'start')::integer)),'hex') END,node->>'unavailable_reason');
                            IF node->>'start' IS NOT NULL THEN
                                IF node->>'candidate_id' IS NOT NULL THEN
                                    INSERT INTO {c}.node_alignment(node_id,version_id,candidate_id,input_artifact_hash,source_candidate_hash,profile_hash,source_end,target_start,target_end,operations)
                                    SELECT node_id,vid,(node->>'candidate_id')::uuid,binding.artifact_hash,n->>'source_hash',review.profile_hash,(n->>'source_bytes')::bigint,
                                        (node->>'start')::bigint,(node->>'end')::bigint,ARRAY(SELECT jsonb_array_elements_text(n->'operations'))
                                        FROM jsonb_array_elements(structure->'normalizations') n WHERE n->>'candidate_id'=node->>'candidate_id';
                                END IF;
                                INSERT INTO {c}.approved_anchor(id,version_id,node_id,start_byte,end_byte,span_hash,profile_hash,locator)
                                    SELECT 'anc_'||gen_random_uuid()::text,vid,node_id,start_byte,end_byte,span_hash,review.profile_hash,'node:'||node_id FROM {c}.version_node WHERE id=node_id
                                    ON CONFLICT(version_id,start_byte,end_byte,profile_hash) DO NOTHING;
                            END IF;
                        END LOOP;
                    END IF;
                    SELECT coalesce(jsonb_object_agg(n.local_key,a.id),'{{}}') INTO anchor_ids FROM {c}.version_node n JOIN {c}.approved_anchor a ON a.version_id=n.version_id AND a.start_byte=n.start_byte AND a.end_byte=n.end_byte AND a.profile_hash=review.profile_hash WHERE n.version_id=vid;
                    record_set:=structure||jsonb_build_object('node_ids',node_ids,'anchor_ids',anchor_ids,'profile_hash',review.profile_hash,'snapshot_hash',review.snapshot_hash,
                        'input_artifact_hash',binding.artifact_hash,'geometry_quality','unavailable','geometry_reason','synthetic_fixture_has_no_physical_geometry','publication_eligible',false);
                    IF octet_length(record_set::text)>262144 THEN RAISE EXCEPTION 'Approved record set bound exceeded' USING ERRCODE='54000'; END IF;
                    PERFORM {p}.reserve_assembly_capacity(binding.artifact_id,cid,octet_length(record_set::text),1);
                    INSERT INTO {c}.representation_revision(id,version_id,artifact_id,expression_id,revision,parent_rep_id,review_id,snapshot_hash,profile_hash,record_set_hash,payload)
                        VALUES(rid,vid,binding.artifact_id,binding.expression_id,rev,CASE WHEN rev>1 THEN rep_head.rep_id END,review.id,review.snapshot_hash,review.profile_hash,encode(sha256(convert_to(record_set::text,'UTF8')),'hex'),record_set);
                    INSERT INTO {c}.document_order(rep_id,version_id,position,node_id) SELECT rid,vid,o.ord-1,node_ids->>(o.id#>>'{{}}') FROM jsonb_array_elements(structure->'order') WITH ORDINALITY o(id,ord);
                    INSERT INTO {c}.unavailable_mapping(rep_id,version_id,node_id) SELECT rid,vid,id FROM {c}.version_node WHERE version_id=vid;
                    FOR table_value IN SELECT * FROM jsonb_array_elements(structure->'tables') LOOP
                        table_id:=gen_random_uuid();
                        INSERT INTO {c}.approved_table(id,rep_id,version_id,node_id,table_local_id,row_count,column_count)
                            VALUES(table_id,rid,vid,node_ids->>(table_value->>'key'),table_value->>'table_local_id',(table_value->>'rows')::integer,(table_value->>'columns')::integer);
                        FOR cell IN SELECT * FROM jsonb_array_elements(table_value->'cells') LOOP
                            cell_id:=gen_random_uuid();
                            INSERT INTO {c}.approved_cell(id,table_id,rep_id,row_index,column_index,row_span,column_span,role,content_state)
                                VALUES(cell_id,table_id,rid,(cell->>'row')::integer,(cell->>'column')::integer,(cell->>'row_span')::integer,(cell->>'column_span')::integer,
                                    CASE WHEN EXISTS(SELECT 1 FROM jsonb_array_elements_text(cell->'candidates') x WHERE table_value->'headers' ? x) THEN 'header' ELSE 'unknown' END,cell->>'content_state');
                            INSERT INTO {c}.cell_node(cell_id,rep_id,node_id,version_id,position) SELECT cell_id,rid,node_ids->>(o.id#>>'{{}}'),vid,o.ord-1 FROM jsonb_array_elements(cell->'candidates') WITH ORDINALITY o(id,ord);
                        END LOOP;
                    END LOOP;
                    FOR marker IN SELECT * FROM jsonb_array_elements(structure->'markers') LOOP
                        INSERT INTO {c}.footnote_reference(id,rep_id,version_id,target_node_id,marker_start,marker_end,marker_hash)
                            VALUES(gen_random_uuid(),rid,vid,node_ids->>('foot:'||coalesce(marker->>'scope','document')||':'||(marker->>'footnote_id')),(marker->>'start')::bigint,(marker->>'end')::bigint,marker->>'marker_hash');
                    END LOOP;
                    INSERT INTO {c}.representation_head(version_id,rep_id,revision) VALUES(vid,rid,rev) ON CONFLICT(version_id) DO UPDATE SET rep_id=excluded.rep_id,revision=excluded.revision;
                    INSERT INTO {c}.version_head(expression_id,version_id,representation_id) VALUES(binding.expression_id,vid,rid) ON CONFLICT(expression_id) DO UPDATE SET version_id=excluded.version_id,representation_id=excluded.representation_id;
                    INSERT INTO {p}.approval_commit(expression_id,snapshot_hash,version_id,rep_id,actor_id,review_id,rights_revision,verification_revision)
                        VALUES(binding.expression_id,review.snapshot_hash,vid,rid,operator_id,review.id,(command->>'rights_revision')::bigint,review.verification_revision);
                    INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                        VALUES(operator_id,cid,review.evidence_id,'synthetic_version_approved',vid,rev,command->>'reason');
                    result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'version_id',vid,'representation_id',rid,'revision',rev,'content_hash',review.content_hash);
                END IF;
            END IF;
            INSERT INTO {p}.approval_receipt(command_id,actor_id,collection_id,fingerprint,result) VALUES(cmd,operator_id,cid,fingerprint,result);
            RETURN result;
        END $$;
        CREATE FUNCTION {p}.read_approved_synthetic_version(vid text,purpose_value text) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE meta record;result jsonb;
        BEGIN
            SELECT artifact_id,collection_id INTO meta FROM {c}.approved_version WHERE id=vid;
            IF meta.artifact_id IS NULL THEN RETURN NULL; END IF;
            PERFORM {p}.lock_assembly_input(meta.artifact_id,meta.collection_id,purpose_value,false);
            PERFORM 1 FROM {c}.representation_head WHERE version_id=vid FOR SHARE;
            PERFORM {p}.lock_synthetic_approval_policy(meta.collection_id,(SELECT rights_revision FROM {c}.approved_version WHERE id=vid),(SELECT verification_revision FROM {c}.approved_version WHERE id=vid));
            IF NOT {p}.approved_version_current(vid,purpose_value) THEN RETURN NULL; END IF;
            SELECT jsonb_build_object('version_id',v.id,'expression_id',v.expression_id,'current_rep_id',r.id,'rep_revision',r.revision,
                'snapshot_hash',r.snapshot_hash,'profile_hash',r.profile_hash,'serialization_profile_id',v.serialization_profile_id,'content_hash',v.content_hash,'canonical_text',convert_from(v.canonical_bytes,'UTF8'),
                'record_set',r.payload,'publication_eligible',false) INTO result FROM {c}.approved_version v JOIN {c}.representation_head h ON h.version_id=v.id
                JOIN {c}.representation_revision r ON r.id=h.rep_id WHERE v.id=vid;
            RETURN result;
        END $$;
    """)


def install_materializer(p, s, c):
    op.execute(f"""
        CREATE FUNCTION {p}.hold_stale_approval(artifact uuid) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE changed integer;next_rev bigint;audit uuid;meta record;targets jsonb;
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision,purpose INTO meta FROM {s}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT jsonb_build_object('binding',coalesce((SELECT jsonb_agg(id) FROM {s}.synthetic_expression_binding b WHERE artifact_id=artifact AND state='active' AND NOT {p}.binding_current(b.id)),'[]'),
                'review',coalesce((SELECT jsonb_agg(id) FROM {s}.snapshot_review r WHERE artifact_id=artifact AND state='active' AND
                    (NOT {p}.binding_current(r.binding_id) OR NOT EXISTS(SELECT 1 FROM {p}.revision_head WHERE kind='verification' AND scope_id=r.collection_id AND revision=r.verification_revision)
                    OR (NOT EXISTS(SELECT 1 FROM {p}.approval_commit WHERE review_id=r.id) AND NOT {p}.canonical_current(r.proposal_id,meta.purpose)))),'[]'),
                'version',coalesce((SELECT jsonb_agg(id) FROM {c}.approved_version v WHERE artifact_id=artifact AND state='active' AND NOT {p}.approved_version_current(v.id,meta.purpose)),'[]'),
                'representation',coalesce((SELECT jsonb_agg(id) FROM {c}.representation_revision r WHERE artifact_id=artifact AND state='active' AND NOT {p}.approved_version_current(r.version_id,meta.purpose)),'[]')) INTO targets;
            changed:=jsonb_array_length(targets->'binding')+jsonb_array_length(targets->'review')+jsonb_array_length(targets->'version')+jsonb_array_length(targets->'representation');
            IF changed=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(revision),0)+1 INTO next_rev FROM {p}.assembly_lifecycle_event WHERE artifact_id=artifact;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {p}.current_actor(),meta.collection_id,evidence_id,'assembly_lifecycle',artifact::text,next_rev,'Approval authority no longer current'
                FROM {p}.acquisition_assessment WHERE id=meta.assessment_id AND revision=meta.assessment_revision RETURNING id INTO audit;
            INSERT INTO {p}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id)
                VALUES(artifact,next_rev,{p}.current_actor(),'held','derivation_not_current',changed,audit);
            UPDATE {s}.synthetic_expression_binding SET state='held' WHERE targets->'binding' ? id::text;
            UPDATE {s}.snapshot_review SET state='held' WHERE targets->'review' ? id::text;
            UPDATE {c}.approved_version SET state='held' WHERE targets->'version' ? id;
            UPDATE {c}.representation_revision SET state='held' WHERE targets->'representation' ? id;
            RETURN changed;
        END $$;
    """)


def extend_lifecycle(p, s, c):
    conn = op.get_bind()
    for signature in helpers():
        definition = conn.exec_driver_sql(
            "SELECT pg_get_functiondef(to_regprocedure(%s))", (p + "." + signature,)
        ).scalar_one()
        if signature.startswith("staging_live_bytes"):
            anchor = f"+coalesce((SELECT sum(octet_length(c.payload::text)+octet_length(c.canonical_bytes)) FROM {s}.canonical_proposal c WHERE c.collection_id=cid),0)"
            replacement = (
                anchor
                + f"\n                +coalesce((SELECT sum(octet_length(payload::text)) FROM {s}.synthetic_expression_binding WHERE collection_id=cid),0)\n                +coalesce((SELECT sum(octet_length(payload::text)) FROM {s}.snapshot_review WHERE collection_id=cid),0)\n                +coalesce((SELECT sum(octet_length(payload::text)+octet_length(canonical_bytes)) FROM {c}.approved_version WHERE collection_id=cid),0)\n                +coalesce((SELECT sum(octet_length(r.payload::text)) FROM {c}.representation_revision r JOIN {c}.approved_version v ON v.id=r.version_id WHERE v.collection_id=cid),0)"
            )
        elif signature.startswith("reserve_assembly_capacity"):
            anchor = f"UNION ALL SELECT octet_length(payload::text)+octet_length(canonical_bytes) FROM {s}.canonical_proposal WHERE artifact_id=artifact)"
            replacement = (
                anchor[:-1]
                + f"\n                UNION ALL SELECT octet_length(payload::text) FROM {s}.synthetic_expression_binding WHERE artifact_id=artifact\n                UNION ALL SELECT octet_length(payload::text) FROM {s}.snapshot_review WHERE artifact_id=artifact\n                UNION ALL SELECT octet_length(payload::text)+octet_length(canonical_bytes) FROM {c}.approved_version WHERE artifact_id=artifact\n                UNION ALL SELECT octet_length(payload::text) FROM {c}.representation_revision WHERE artifact_id=artifact)"
            )
        elif signature.startswith("transition_assembly"):
            anchor = f"UNION ALL SELECT 1 FROM {s}.canonical_proposal WHERE artifact_id=artifact AND state=ANY(eligible))"
            replacement = (
                anchor[:-1]
                + f"\n                UNION ALL SELECT 1 FROM {s}.synthetic_expression_binding WHERE artifact_id=artifact AND state=ANY(eligible)\n                UNION ALL SELECT 1 FROM {s}.snapshot_review WHERE artifact_id=artifact AND state=ANY(eligible)\n                UNION ALL SELECT 1 FROM {c}.approved_version WHERE artifact_id=artifact AND state=ANY(eligible)\n                UNION ALL SELECT 1 FROM {c}.representation_revision WHERE artifact_id=artifact AND state=ANY(eligible))"
            )
            final = "            RETURN changed;"
            if definition.count(final) != 1:
                raise RuntimeError("Installed approval erasure requires boundary review")
            updates = ""
            for schema, table in (
                (s, "synthetic_expression_binding"),
                (s, "snapshot_review"),
                (c, "approved_version"),
                (c, "representation_revision"),
            ):
                updates += f"            UPDATE {schema}.{table} SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END"
                if table == "approved_version":
                    updates += (
                        ",canonical_bytes=CASE WHEN following='erased' THEN NULL ELSE canonical_bytes END"
                    )
                updates += " WHERE artifact_id=artifact AND state=ANY(eligible);\n"
            definition = definition.replace(final, updates + final, 1)
        else:
            anchor = f"held:=held+{p}.hold_stale_canonical(row_item.id);"
            replacement = anchor + f"\n                    held:=held+{p}.hold_stale_approval(row_item.id);"
            erase_anchor = f"OR EXISTS(SELECT 1 FROM {s}.canonical_proposal WHERE artifact_id=row_item.id AND state='erasure_required'))"
            if definition.count(erase_anchor) != 1:
                raise RuntimeError("Installed approval materializer requires boundary review")
            extension = erase_anchor[:-1]
            for schema, table in (
                (s, "synthetic_expression_binding"),
                (s, "snapshot_review"),
                (c, "approved_version"),
                (c, "representation_revision"),
            ):
                extension += f"\n                        OR EXISTS(SELECT 1 FROM {schema}.{table} WHERE artifact_id=row_item.id AND state='erasure_required')"
            definition = definition.replace(erase_anchor, extension + ")", 1)
        if definition.count(anchor) != 1:
            raise RuntimeError("Installed lifecycle requires boundary review")
        name = signature.split("(")[0]
        op.execute(f"ALTER FUNCTION {p}.{signature} RENAME TO {name}_m32")
        op.execute(definition.replace(anchor, replacement, 1))


def downgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    b, p, s, c, _, _ = names()
    if conn.exec_driver_sql(
        f"SELECT EXISTS(SELECT 1 FROM {s}.synthetic_expression_binding) OR EXISTS(SELECT 1 FROM {s}.snapshot_review) OR EXISTS(SELECT 1 FROM {c}.approved_version) OR EXISTS(SELECT 1 FROM {p}.approval_receipt)"
    ).scalar_one():
        raise RuntimeError("Private synthetic approval requires forward repair")
    for table in roots():
        schema = s if table in ("synthetic_expression_binding", "snapshot_review") else c
        op.execute(
            f"DROP TRIGGER lifecycle_update ON {schema}.{table}; DROP TRIGGER no_truncate ON {schema}.{table}"
        )
    op.execute(f"DROP TRIGGER declaration_insert ON {s}.synthetic_expression_binding")
    for table in ("version_node", "approved_anchor", "footnote_reference"):
        op.execute(f"DROP TRIGGER exact_span ON {c}.{table}")
    op.execute(f"DROP TRIGGER exact_cell ON {c}.approved_cell")
    for signature in helpers():
        name = signature.split("(")[0]
        args = signature[signature.index("(") :]
        op.execute(f"DROP FUNCTION {p}.{signature}; ALTER FUNCTION {p}.{name}_m32{args} RENAME TO {name}")
    for table in ("identity_works", "identity_expressions", "identity_manifestations"):
        op.execute(f"DROP TRIGGER bound_identity ON {b}.{table}")
    op.execute(
        f"DROP TRIGGER approved_tree ON {c}.version_node; DROP TRIGGER approved_grid ON {c}.approved_table; DROP TRIGGER exact_alignment ON {c}.node_alignment"
    )
    op.execute(f"DROP TRIGGER approval_commit_recheck ON {p}.approval_receipt")
    for signature in functions():
        op.execute(f"DROP FUNCTION {p}.{signature}")
    for schema, table in (
        (p, "approval_commit"),
        (p, "approval_receipt"),
        (c, "version_head"),
        (c, "representation_head"),
    ):
        op.execute(f"DROP TABLE {schema}.{table}")
    for table in reversed(children()):
        op.execute(f"DROP TABLE {c}.{table}")
    op.execute(
        f"DROP TABLE {c}.representation_revision; DROP TABLE {c}.approved_version; DROP TABLE {s}.snapshot_review; DROP TABLE {s}.synthetic_expression_binding; ALTER TABLE {b}.identity_expressions DROP CONSTRAINT approval_expression_work; ALTER TABLE {b}.identity_works DROP CONSTRAINT approval_work_collection; ALTER TABLE {b}.identity_manifestations DROP CONSTRAINT approval_manifestation_bytes; ALTER TABLE {s}.staged_artifact DROP CONSTRAINT approval_artifact_bytes"
    )


def install_identity_guards(b, p, s):
    op.execute(f"""
        CREATE FUNCTION {p}.bound_identity_immutable() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF TG_OP='UPDATE' AND to_jsonb(NEW) IS NOT DISTINCT FROM to_jsonb(OLD) THEN RETURN NEW; END IF;
            IF EXISTS(SELECT 1 FROM {s}.synthetic_expression_binding WHERE
                (TG_TABLE_NAME='identity_works' AND work_id=OLD.id) OR
                (TG_TABLE_NAME='identity_expressions' AND expression_id=OLD.id) OR
                (TG_TABLE_NAME='identity_manifestations' AND manifestation_id=OLD.id)) THEN
                RAISE EXCEPTION 'Frozen synthetic identity requires forward correction' USING ERRCODE='55000'; END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF;RETURN NEW;
        END $$;
        CREATE TRIGGER bound_identity BEFORE UPDATE OR DELETE ON {b}.identity_works FOR EACH ROW EXECUTE FUNCTION {p}.bound_identity_immutable();
        CREATE TRIGGER bound_identity BEFORE UPDATE OR DELETE ON {b}.identity_expressions FOR EACH ROW EXECUTE FUNCTION {p}.bound_identity_immutable();
        CREATE TRIGGER bound_identity BEFORE UPDATE OR DELETE ON {b}.identity_manifestations FOR EACH ROW EXECUTE FUNCTION {p}.bound_identity_immutable();
    """)


def install_record_guards(p, s, c):
    op.execute(f"""
        CREATE FUNCTION {p}.validate_approved_tree() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF (SELECT count(*) FROM {c}.version_node WHERE version_id=NEW.version_id)>200 OR EXISTS(
                WITH RECURSIVE ancestors AS (
                    SELECT id,parent_id,ARRAY[id] AS seen,false AS cycle FROM {c}.version_node WHERE id=NEW.id
                    UNION ALL SELECT n.id,n.parent_id,a.seen||n.id,n.id=ANY(a.seen) FROM ancestors a JOIN {c}.version_node n ON n.id=a.parent_id
                        WHERE NOT a.cycle AND cardinality(a.seen)<=200
                ) SELECT 1 FROM ancestors WHERE cycle OR cardinality(seen)>200) THEN
                RAISE EXCEPTION 'Bounded acyclic version tree required' USING ERRCODE='23514'; END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER approved_tree AFTER INSERT ON {c}.version_node DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {p}.validate_approved_tree();
        CREATE FUNCTION {p}.validate_approved_grid() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF (SELECT coalesce(sum(row_span*column_span),0) FROM {c}.approved_cell WHERE table_id=NEW.id)<>NEW.row_count*NEW.column_count THEN
                RAISE EXCEPTION 'Complete approved cell grid required' USING ERRCODE='23514'; END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER approved_grid AFTER INSERT ON {c}.approved_table DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {p}.validate_approved_grid();
        CREATE FUNCTION {p}.validate_node_alignment() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM {c}.version_node n JOIN {c}.approved_version v ON v.id=n.version_id
                JOIN {s}.adapter_candidate d ON d.id=n.candidate_id JOIN {s}.staged_artifact a ON a.id=v.artifact_id
                WHERE n.id=NEW.node_id AND n.version_id=NEW.version_id AND d.id=NEW.candidate_id
                    AND a.artifact_hash=NEW.input_artifact_hash AND v.profile_hash=NEW.profile_hash
                    AND octet_length(d.payload->>'text')=NEW.source_end AND n.start_byte=NEW.target_start AND n.end_byte=NEW.target_end
                    AND encode(sha256(convert_to(d.payload->>'text','UTF8')),'hex')=NEW.source_candidate_hash) THEN
                RAISE EXCEPTION 'Exact whole-candidate alignment required' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER exact_alignment BEFORE INSERT ON {c}.node_alignment FOR EACH ROW EXECUTE FUNCTION {p}.validate_node_alignment();
    """)


def install_commit_guard(p, s, c):
    op.execute(f"""
        CREATE FUNCTION {p}.validate_approval_commit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE review {s}.snapshot_review;version {c}.approved_version;purpose_value text;commit_meta {p}.approval_commit;
        BEGIN
            IF NEW.actor_id IS DISTINCT FROM {p}.current_actor() OR NOT {p}.is_native_operator() OR NOT {p}.assigned(NEW.collection_id,'acquire') THEN RAISE EXCEPTION 'Native approval authority expired before commit' USING ERRCODE='42501'; END IF;
            IF NEW.result->>'version_id' IS NULL THEN
                SELECT * INTO review FROM {s}.snapshot_review WHERE command_id=NEW.command_id;
                IF review.id IS NULL OR review.state<>'active' OR review.actor_id<>NEW.actor_id OR NOT {p}.assigned(NEW.collection_id,'content')
                    OR NOT {p}.binding_current(review.binding_id) OR NOT {p}.canonical_current(review.proposal_id,review.payload->>'purpose')
                    OR NOT EXISTS(SELECT 1 FROM {p}.verification_policy v JOIN {p}.revision_head h ON h.kind='verification' AND h.scope_id=v.collection_id AND h.revision=v.revision
                        WHERE v.collection_id=NEW.collection_id AND v.revision=review.verification_revision AND v.actor_id=NEW.actor_id AND v.state='approved' AND v.valid_from<=statement_timestamp() AND v.expires_at>statement_timestamp())
                    THEN RAISE EXCEPTION 'Exact source comparison expired before commit' USING ERRCODE='42501'; END IF;
            ELSE
                SELECT * INTO version FROM {c}.approved_version WHERE id=NEW.result->>'version_id';
                SELECT purpose INTO purpose_value FROM {s}.staged_artifact WHERE id=version.artifact_id;
                IF NOT {p}.assigned(NEW.collection_id,'release') OR NOT {p}.approved_version_current(version.id,purpose_value) THEN RAISE EXCEPTION 'Approval inputs expired before commit' USING ERRCODE='42501'; END IF;
                SELECT * INTO commit_meta FROM {p}.approval_commit WHERE rep_id=NEW.result->>'representation_id';
                SELECT * INTO review FROM {s}.snapshot_review WHERE id=commit_meta.review_id;
                IF NOT {p}.canonical_current(review.proposal_id,purpose_value) THEN RAISE EXCEPTION 'Snapshot changed before commit' USING ERRCODE='42501'; END IF;
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER approval_commit_recheck AFTER INSERT ON {p}.approval_receipt DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {p}.validate_approval_commit();
    """)
