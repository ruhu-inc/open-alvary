"""Private cross-checked OA-text-1 proposals, current snapshots and inherited erasure."""

from hashlib import sha256

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

PROFILE_DESCRIPTOR = '{"id":"OA-text-1","scope":"original-synthetic","encoding":"UTF-8","normalization":"NFC","line_endings":"LF","whitespace":"literal","block_separator":"LF LF","table_column_separator":"TAB","table_row_separator":"LF","terminator":"one added LF","dehyphenation":"unsupported","alignment":"whole private candidate"}'
PROFILE_HASH = sha256(PROFILE_DESCRIPTOR.encode()).hexdigest()


def names():
    conn = op.get_bind()
    base = conn.exec_driver_sql("SELECT current_schema()").scalar_one()
    quote = conn.dialect.identifier_preparer.quote_identifier
    p, s = (quote(f"{base}_{kind}" if base.startswith("test_") else kind) for kind in ("policy", "staging"))
    return p, s, "oa_" + sha256(base.encode()).hexdigest()[:12], quote


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    p, s, prefix, quote = names()
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
        for fn in (
            "staging_live_bytes(text)",
            "reserve_assembly_capacity(uuid,text,bigint,integer)",
            "transition_assembly(uuid,text,text)",
            "apply_assembly_command(jsonb)",
            "lock_assembly_input(uuid,text,text,boolean)",
        ):
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
        ALTER TABLE {s}.staging_snapshot ADD CONSTRAINT canonical_snapshot_lineage UNIQUE(id,revision,snapshot_hash,run_id,artifact_id);
        CREATE TABLE {s}.canonical_proposal (
            id uuid PRIMARY KEY,
            snapshot_id uuid NOT NULL, snapshot_revision bigint NOT NULL, snapshot_hash text NOT NULL,
            run_id uuid NOT NULL, artifact_id uuid NOT NULL,
            collection_id text NOT NULL REFERENCES {p}.synthetic_staging_limit(collection_id),
            serialization_profile_id text NOT NULL CHECK(serialization_profile_id='OA-text-1'),
            profile_hash text NOT NULL CHECK(profile_hash='{PROFILE_HASH}'),
            canonical_bytes bytea, payload jsonb,
            content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{{64}}$'),
            payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{{64}}$'),
            size_bytes bigint NOT NULL CHECK(size_bytes BETWEEN 0 AND 131072),
            payload_bytes bigint NOT NULL CHECK(payload_bytes BETWEEN 1 AND 262144),
            actor_id uuid NOT NULL REFERENCES {p}.actor(id),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','held','erasure_required','erased')),
            UNIQUE(snapshot_id,snapshot_revision,profile_hash),
            FOREIGN KEY(snapshot_id,snapshot_revision,snapshot_hash,run_id,artifact_id)
                REFERENCES {s}.staging_snapshot(id,revision,snapshot_hash,run_id,artifact_id),
            CHECK((state='erased')=(canonical_bytes IS NULL AND payload IS NULL)),
            CHECK(state='erased' OR coalesce(canonical_bytes IS NOT NULL AND payload IS NOT NULL
                AND octet_length(canonical_bytes)=size_bytes AND encode(sha256(canonical_bytes),'hex')=content_hash
                AND octet_length(payload::text)=payload_bytes AND encode(sha256(convert_to(payload::text,'UTF8')),'hex')=payload_hash
                AND payload->'projection'->'publication_eligible'='false'::jsonb,false))
        );
        CREATE INDEX canonical_family ON {s}.canonical_proposal(artifact_id);
        ALTER TABLE {s}.canonical_proposal ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {s}.canonical_proposal FORCE ROW LEVEL SECURITY;
        CREATE POLICY canonical_guard ON {s}.canonical_proposal USING(current_user='{prefix}_guard');
        REVOKE ALL ON {s}.canonical_proposal FROM PUBLIC;
        ALTER TABLE {p}.assembly_receipt DROP CONSTRAINT assembly_receipt_action_check,
            ADD CONSTRAINT assembly_receipt_action_check CHECK(action IN ('record_synthetic_run','record_snapshot','reconcile_assembly','record_canonical_proposal'));
    """)
    install_serializer(p)
    install_storage(p, s, prefix)
    extend_lifecycle(p, s)
    functions = signatures()
    for fn in functions:
        op.execute(f"REVOKE ALL ON FUNCTION {p}.{fn} FROM PUBLIC")
    if guard_oid is not None:
        guard = quote(prefix + "_guard")
        op.execute(
            f"GRANT SELECT,INSERT,UPDATE(state,payload,canonical_bytes) ON {s}.canonical_proposal TO {guard}"
        )
        op.execute(f"GRANT CREATE ON SCHEMA {p} TO {guard}")
        for fn in functions:
            op.execute(f"ALTER FUNCTION {p}.{fn} OWNER TO {guard}")
        op.execute(f"REVOKE CREATE ON SCHEMA {p} FROM {guard}")
        for fn in public_functions():
            op.execute(f"GRANT EXECUTE ON FUNCTION {p}.{fn} TO {quote(prefix + '_acquisition')}")


def signatures():
    return (
        "oa_norm(text)",
        "oa_shape(jsonb,text[],text[],text[])",
        "oa_separator(jsonb,text)",
        "oa_leaf(jsonb,jsonb,uuid,text[],boolean)",
        "oa_block(jsonb,jsonb,jsonb)",
        "oa_scope(jsonb,jsonb,jsonb,text)",
        "oa_compile(jsonb,jsonb)",
        "canonical_inputs(jsonb)",
        "canonical_current(uuid,text)",
        "validate_canonical_update()",
        "hold_stale_canonical(uuid)",
        "store_canonical_proposal(jsonb,bytea,jsonb)",
        "read_canonical_proposal(uuid,text)",
        "staging_live_bytes(text)",
        "reserve_assembly_capacity(uuid,text,bigint,integer)",
        "transition_assembly(uuid,text,text)",
        "apply_assembly_command(jsonb)",
    )


def public_functions():
    return (
        "canonical_inputs(jsonb)",
        "store_canonical_proposal(jsonb,bytea,jsonb)",
        "read_canonical_proposal(uuid,text)",
    )


def install_serializer(p):
    op.execute(f"""
        CREATE FUNCTION {p}.oa_norm(v text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
            SELECT normalize(replace(replace(v,E'\\r\\n',E'\\n'),E'\\r',E'\\n'),NFC)
        $$;
        CREATE FUNCTION {p}.oa_shape(body jsonb,required text[],optional text[],arrays text[]) RETURNS void
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE k text;
        BEGIN
            IF NOT {p}.operator_keys(body,required,optional) THEN RAISE EXCEPTION 'Invalid canonical shape' USING ERRCODE='22023'; END IF;
            FOREACH k IN ARRAY arrays LOOP
                IF jsonb_typeof(coalesce(body->k,'[]'::jsonb)) IS DISTINCT FROM 'array'
                    OR jsonb_array_length(coalesce(body->k,'[]'::jsonb))>10000 THEN RAISE EXCEPTION 'Bounded canonical array required' USING ERRCODE='22023'; END IF;
            END LOOP;
        END $$;
        CREATE FUNCTION {p}.oa_separator(st jsonb,kind text) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE bytes integer; value text;
        BEGIN
            bytes:=octet_length(st->>'text');
            value:=CASE WHEN kind IN ('block','cell') THEN E'\\n\\n' WHEN kind='column' THEN E'\\t' ELSE E'\\n' END;
            st:=jsonb_set(st,ARRAY['separators',kind],(st->'separators'->kind)||to_jsonb(bytes));
            RETURN jsonb_set(st,'{{text}}',to_jsonb((st->>'text')||value));
        END $$;
        CREATE FUNCTION {p}.oa_leaf(st jsonb,inputs jsonb,cid uuid,types text[],body boolean) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE c jsonb; original text; lf text; normalized text; ops jsonb:='[]'; begin_byte integer; end_byte integer;
        BEGIN
            c:=inputs->'source'->cid::text;
            IF c IS NULL OR (st->'used') ? cid::text OR NOT coalesce(c->>'block_type'=ANY(types),false) THEN
                RAISE EXCEPTION 'Invalid or repeated source candidate' USING ERRCODE='23514'; END IF;
            st:=jsonb_set(st,'{{used}}',(st->'used')||to_jsonb(cid::text));
            IF body THEN st:=jsonb_set(st,'{{body_order}}',(st->'body_order')||to_jsonb(cid::text)); END IF;
            original:=c->>'text'; lf:=replace(replace(original,E'\\r\\n',E'\\n'),E'\\r',E'\\n'); normalized:={p}.oa_norm(original);
            IF lf<>original THEN ops:=ops||'"lf"'::jsonb; END IF;
            IF normalized<>lf THEN ops:=ops||'"nfc"'::jsonb; END IF;
            begin_byte:=octet_length(st->>'text'); end_byte:=begin_byte+octet_length(normalized);
            st:=jsonb_set(st,'{{normalizations}}',(st->'normalizations')||jsonb_build_array(jsonb_build_object('candidate_id',cid,
                'source_hash',encode(sha256(convert_to(original,'UTF8')),'hex'),'source_bytes',octet_length(original),
                'normalized_hash',encode(sha256(convert_to(normalized,'UTF8')),'hex'),'normalized_bytes',octet_length(normalized),'operations',ops)));
            IF end_byte>begin_byte THEN st:=jsonb_set(st,'{{spans}}',(st->'spans')||jsonb_build_array(jsonb_build_object('candidate_id',cid,
                'start',begin_byte,'end',end_byte,'span_hash',encode(sha256(convert_to(normalized,'UTF8')),'hex')))); END IF;
            IF end_byte>131072 THEN RAISE EXCEPTION 'Canonical output bound exceeded' USING ERRCODE='54000'; END IF;
            RETURN jsonb_set(st,'{{text}}',to_jsonb((st->>'text')||normalized));
        END $$;
        CREATE FUNCTION {p}.oa_block(st jsonb,inputs jsonb,item jsonb) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE c jsonb; first_c jsonb; shape jsonb; cell jsonb; absent jsonb; id_value jsonb; h jsonb;
            positions jsonb:='{{}}'; origins jsonb:='{{}}'; covering jsonb; members jsonb:='[]'; key text;
            nr integer; nc integer; rr integer; cc integer; r integer; col integer; rs integer; cs integer; n integer;
            partial boolean:=false;
        BEGIN
            IF item->>'kind'='text' THEN
                PERFORM {p}.oa_shape(item,ARRAY['kind','candidate_id'],ARRAY[]::text[],ARRAY[]::text[]);
                c:=inputs->'source'->(item->>'candidate_id');
                IF c IS NULL OR c->>'content_state'<>'present' THEN RAISE EXCEPTION 'Present literal block required' USING ERRCODE='23514'; END IF;
                RETURN {p}.oa_leaf(st,inputs,(item->>'candidate_id')::uuid,ARRAY['heading','paragraph','list_item'],true);
            END IF;
            IF item->>'kind' IS DISTINCT FROM 'table' THEN RAISE EXCEPTION 'Unsupported block' USING ERRCODE='22023'; END IF;
            PERFORM {p}.oa_shape(item,ARRAY['kind','table_local_id','rows','columns','cells'],ARRAY['headers','unavailable'],ARRAY['cells','headers','unavailable']);
            IF NOT {p}.assembly_local_key(item->'table_local_id',false) OR (st->'local_ids') ? (item->>'table_local_id')
                OR jsonb_typeof(item->'rows') IS DISTINCT FROM 'number' OR item->>'rows' !~ '^[0-9]{{1,3}}$'
                OR jsonb_typeof(item->'columns') IS DISTINCT FROM 'number' OR item->>'columns' !~ '^[0-9]{{1,3}}$' THEN RAISE EXCEPTION 'Bounded table required' USING ERRCODE='22023'; END IF;
            nr:=(item->>'rows')::integer; nc:=(item->>'columns')::integer;
            IF nr NOT BETWEEN 1 AND 100 OR nc NOT BETWEEN 1 AND 100 OR (st->>'grid_positions')::integer+nr*nc>10000
                OR jsonb_array_length(item->'cells') NOT BETWEEN 1 AND 50 OR jsonb_array_length(coalesce(item->'headers','[]'))>50 THEN RAISE EXCEPTION 'Table grid bound exceeded' USING ERRCODE='22023'; END IF;
            st:=jsonb_set(st,'{{local_ids}}',(st->'local_ids')||to_jsonb(item->>'table_local_id'));
            st:=jsonb_set(st,'{{grid_positions}}',to_jsonb((st->>'grid_positions')::integer+nr*nc));
            FOR cell IN SELECT * FROM jsonb_array_elements(item->'cells') LOOP
                PERFORM {p}.oa_shape(cell,ARRAY['candidates'],ARRAY[]::text[],ARRAY['candidates']);
                IF jsonb_array_length(cell->'candidates') NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Bounded cell children required' USING ERRCODE='22023'; END IF;
                first_c:=inputs->'source'->(cell->'candidates'->>0); shape:=first_c->'cell';
                IF shape IS NULL OR shape='null'::jsonb OR shape->>'table_local_id' IS DISTINCT FROM item->>'table_local_id' THEN RAISE EXCEPTION 'Explicit table lineage required' USING ERRCODE='23514'; END IF;
                rr:=(shape->>'row')::integer; cc:=(shape->>'column')::integer; rs:=(shape->>'row_span')::integer; cs:=(shape->>'column_span')::integer;
                IF rr+rs>nr OR cc+cs>nc THEN RAISE EXCEPTION 'Cell outside declared table' USING ERRCODE='23514'; END IF;
                FOR id_value IN SELECT * FROM jsonb_array_elements(cell->'candidates') LOOP
                    c:=inputs->'source'->(id_value#>>'{{}}');
                    IF c IS NULL OR c->'cell' IS DISTINCT FROM shape OR (members ? (id_value#>>'{{}}'))
                        OR (jsonb_array_length(cell->'candidates')>1 AND c->>'content_state'<>'present') THEN RAISE EXCEPTION 'Invalid cell children' USING ERRCODE='23514'; END IF;
                    members:=members||id_value;
                END LOOP;
                SELECT jsonb_object_agg(grid_row.n::text||':'||grid_col.n::text,'covered') INTO covering
                    FROM generate_series(rr,rr+rs-1) grid_row(n) CROSS JOIN generate_series(cc,cc+cs-1) grid_col(n);
                IF EXISTS(SELECT 1 FROM jsonb_object_keys(covering) k WHERE positions ? k) THEN RAISE EXCEPTION 'Overlapping cell grid' USING ERRCODE='23514'; END IF;
                positions:=positions||covering; origins:=jsonb_set(origins,ARRAY[rr::text||':'||cc::text],cell);
            END LOOP;
            FOR absent IN SELECT * FROM jsonb_array_elements(coalesce(item->'unavailable','[]')) LOOP
                PERFORM {p}.oa_shape(absent,ARRAY['row','column','content_state','reason'],ARRAY[]::text[],ARRAY[]::text[]);
                IF jsonb_typeof(absent->'row') IS DISTINCT FROM 'number' OR absent->>'row' !~ '^[0-9]{{1,2}}$'
                    OR jsonb_typeof(absent->'column') IS DISTINCT FROM 'number' OR absent->>'column' !~ '^[0-9]{{1,2}}$'
                    OR coalesce(absent->>'content_state','') NOT IN ('illegible','unsupported')
                    OR jsonb_typeof(absent->'reason') IS DISTINCT FROM 'string' OR length(btrim(absent->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Explicit unavailable grid position required' USING ERRCODE='22023'; END IF;
                rr:=(absent->>'row')::integer; cc:=(absent->>'column')::integer; key:=rr::text||':'||cc::text;
                IF rr>=nr OR cc>=nc OR positions ? key THEN RAISE EXCEPTION 'Invalid unavailable grid position' USING ERRCODE='23514'; END IF;
                positions:=jsonb_set(positions,ARRAY[key],'"unavailable"'); partial:=true;
            END LOOP;
            IF (SELECT count(*) FROM jsonb_object_keys(positions))<>nr*nc THEN RAISE EXCEPTION 'Missing grid position' USING ERRCODE='23514'; END IF;
            FOR h IN SELECT * FROM jsonb_array_elements(coalesce(item->'headers','[]')) LOOP
                IF NOT(members ? (h#>>'{{}}')) OR st->'headers' ? (h#>>'{{}}') THEN RAISE EXCEPTION 'Invalid table header' USING ERRCODE='23514'; END IF;
                st:=jsonb_set(st,'{{headers}}',(st->'headers')||h);
            END LOOP;
            FOR r IN 0..nr-1 LOOP
                IF r>0 THEN st:={p}.oa_separator(st,'row'); END IF;
                FOR col IN 0..nc-1 LOOP
                    IF col>0 THEN st:={p}.oa_separator(st,'column'); END IF;
                    cell:=origins->(r::text||':'||col::text);
                    IF cell IS NULL THEN CONTINUE; END IF;
                    n:=0;
                    FOR id_value IN SELECT * FROM jsonb_array_elements(cell->'candidates') LOOP
                        IF n>0 THEN st:={p}.oa_separator(st,'cell'); END IF;
                        st:={p}.oa_leaf(st,inputs,(id_value#>>'{{}}')::uuid,ARRAY['table_cell'],true);
                        c:=inputs->'source'->(id_value#>>'{{}}'); partial:=partial OR c->>'content_state' IN ('illegible','unsupported'); n:=n+1;
                    END LOOP;
                END LOOP;
            END LOOP;
            IF partial THEN st:=jsonb_set(st,'{{incomplete_tables}}',(st->'incomplete_tables')||to_jsonb(item->>'table_local_id')); END IF;
            RETURN st;
        END $$;
        CREATE FUNCTION {p}.oa_scope(st jsonb,inputs jsonb,scope_body jsonb,scope_name text) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE item jsonb; foot jsonb; marker jsonb; c jsonb; span jsonb; def jsonb; id_value jsonb;
            definitions jsonb:='{{}}'; reference_order jsonb:='[]'; seen jsonb:='[]'; previous_ends jsonb:='{{}}';
            direct jsonb:='[]'; before_used integer; begin_byte integer; finish_byte integer; n integer:=0; child integer;
            raw bytea; prefix_bytes bytea; end_bytes bytea; marker_bytes bytea; whole bytea; a integer; z integer; key text;
            sorted_defs jsonb:='[]'; fn_ids jsonb; old_notice_count integer;
        BEGIN
            PERFORM {p}.oa_shape(scope_body,ARRAY['blocks'],ARRAY['footnotes','markers','exclusions'],ARRAY['blocks','footnotes','markers','exclusions']);
            IF jsonb_array_length(scope_body->'blocks')>50 OR jsonb_array_length(coalesce(scope_body->'footnotes','[]'))>50
                OR jsonb_array_length(coalesce(scope_body->'markers','[]'))>100 THEN RAISE EXCEPTION 'Scope bound exceeded' USING ERRCODE='22023'; END IF;
            before_used:=jsonb_array_length(st->'used'); old_notice_count:=jsonb_array_length(st->'notice_ranges');
            FOR item IN SELECT * FROM jsonb_array_elements(scope_body->'blocks') LOOP
                IF n>0 THEN st:={p}.oa_separator(st,'block'); END IF;
                IF item->>'kind'='notice' THEN
                    IF scope_name IS NOT NULL THEN RAISE EXCEPTION 'Nested notice unsupported' USING ERRCODE='22023'; END IF;
                    PERFORM {p}.oa_shape(item,ARRAY['kind','local_id','blocks'],ARRAY['footnotes','markers'],ARRAY['blocks','footnotes','markers']);
                    IF NOT {p}.assembly_local_key(item->'local_id',false) OR st->'local_ids' ? (item->>'local_id') OR jsonb_array_length(item->'blocks')<1 THEN RAISE EXCEPTION 'Invalid notice scope' USING ERRCODE='23514'; END IF;
                    st:=jsonb_set(st,'{{local_ids}}',(st->'local_ids')||to_jsonb(item->>'local_id'));
                    begin_byte:=octet_length(st->>'text');
                    st:={p}.oa_scope(st,inputs,jsonb_build_object('blocks',item->'blocks','footnotes',coalesce(item->'footnotes','[]'),'markers',coalesce(item->'markers','[]')),item->>'local_id');
                    finish_byte:=octet_length(st->>'text');
                    st:=jsonb_set(st,'{{notice_ranges}}',(st->'notice_ranges')||jsonb_build_array(jsonb_build_object('local_id',item->>'local_id','start',begin_byte,'end',finish_byte,
                        'content_hash',encode(sha256(substring(convert_to(st->>'text','UTF8') FROM begin_byte+1 FOR finish_byte-begin_byte)),'hex'))));
                ELSE st:={p}.oa_block(st,inputs,item); END IF;
                n:=n+1;
            END LOOP;
            SELECT coalesce(jsonb_agg(u.v ORDER BY u.ord),'[]') INTO direct FROM jsonb_array_elements(st->'used') WITH ORDINALITY u(v,ord) WHERE ord>before_used;
            IF scope_name IS NULL THEN
                SELECT coalesce(jsonb_agg(d.v ORDER BY d.ord),'[]') INTO direct FROM jsonb_array_elements(direct) WITH ORDINALITY d(v,ord)
                    WHERE NOT EXISTS(SELECT 1 FROM jsonb_array_elements(st->'spans') sp CROSS JOIN jsonb_array_elements(st->'notice_ranges') nr
                        WHERE sp->>'candidate_id'=d.v#>>'{{}}' AND (sp->>'start')::integer>=(nr->>'start')::integer AND (sp->>'end')::integer<=(nr->>'end')::integer);
            END IF;
            FOR foot IN SELECT * FROM jsonb_array_elements(coalesce(scope_body->'footnotes','[]')) LOOP
                PERFORM {p}.oa_shape(foot,ARRAY['local_id','candidates'],ARRAY[]::text[],ARRAY['candidates']);
                IF NOT {p}.assembly_local_key(foot->'local_id',false) OR definitions ? (foot->>'local_id') OR jsonb_array_length(foot->'candidates') NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Invalid footnote definition' USING ERRCODE='23514'; END IF;
                SELECT coalesce(jsonb_agg(c.v ORDER BY o.ord),'[]') INTO fn_ids FROM jsonb_array_elements(foot->'candidates') c(v)
                    LEFT JOIN jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) ON c.v=o.v;
                IF fn_ids IS DISTINCT FROM foot->'candidates' THEN RAISE EXCEPTION 'Footnote source order changed' USING ERRCODE='23514'; END IF;
                definitions:=jsonb_set(definitions,ARRAY[foot->>'local_id'],foot);
            END LOOP;
            FOR marker IN SELECT m.v FROM jsonb_array_elements(coalesce(scope_body->'markers','[]')) m(v)
                LEFT JOIN jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) ON m.v->'candidate_id'=o.v
                ORDER BY o.ord,(m.v->>'source_start')::integer
            LOOP
                PERFORM {p}.oa_shape(marker,ARRAY['candidate_id','source_start','source_end','footnote_id'],ARRAY[]::text[],ARRAY[]::text[]);
                IF NOT(direct ? (marker->>'candidate_id')) OR NOT(definitions ? (marker->>'footnote_id'))
                    OR jsonb_typeof(marker->'source_start') IS DISTINCT FROM 'number' OR marker->>'source_start' !~ '^[0-9]{{1,5}}$'
                    OR jsonb_typeof(marker->'source_end') IS DISTINCT FROM 'number' OR marker->>'source_end' !~ '^[0-9]{{1,5}}$' THEN RAISE EXCEPTION 'Invalid marker scope/range' USING ERRCODE='23514'; END IF;
                a:=(marker->>'source_start')::integer; z:=(marker->>'source_end')::integer;
                c:=inputs->'source'->(marker->>'candidate_id'); raw:=convert_to(c->>'text','UTF8');
                IF a<coalesce((previous_ends->>(marker->>'candidate_id'))::integer,0) OR a<0 OR z<=a OR z>octet_length(raw) OR z>32768 THEN RAISE EXCEPTION 'Invalid or overlapping marker' USING ERRCODE='23514'; END IF;
                previous_ends:=jsonb_set(previous_ends,ARRAY[marker->>'candidate_id'],to_jsonb(z));
                prefix_bytes:=convert_to({p}.oa_norm(convert_from(substring(raw FROM 1 FOR a),'UTF8')),'UTF8');
                end_bytes:=convert_to({p}.oa_norm(convert_from(substring(raw FROM 1 FOR z),'UTF8')),'UTF8');
                marker_bytes:=convert_to({p}.oa_norm(convert_from(substring(raw FROM a+1 FOR z-a),'UTF8')),'UTF8'); whole:=convert_to({p}.oa_norm(c->>'text'),'UTF8');
                IF substring(whole FROM 1 FOR octet_length(prefix_bytes))<>prefix_bytes OR substring(whole FROM 1 FOR octet_length(end_bytes))<>end_bytes
                    OR substring(whole FROM octet_length(prefix_bytes)+1 FOR octet_length(end_bytes)-octet_length(prefix_bytes))<>marker_bytes OR octet_length(marker_bytes)=0 THEN RAISE EXCEPTION 'Ambiguous normalized marker' USING ERRCODE='23514'; END IF;
                SELECT sp INTO span FROM jsonb_array_elements(st->'spans') sp WHERE sp->>'candidate_id'=marker->>'candidate_id';
                st:=jsonb_set(st,'{{markers}}',(st->'markers')||jsonb_build_array(jsonb_build_object('candidate_id',(marker->>'candidate_id')::uuid,'source_start',a,'source_end',z,
                    'start',(span->>'start')::integer+octet_length(prefix_bytes),'end',(span->>'start')::integer+octet_length(end_bytes),
                    'marker_hash',encode(sha256(marker_bytes),'hex'),'footnote_id',marker->>'footnote_id','scope',scope_name)));
                IF NOT(reference_order ? (marker->>'footnote_id')) THEN reference_order:=reference_order||to_jsonb(marker->>'footnote_id'); END IF;
            END LOOP;
            SELECT coalesce(jsonb_agg(to_jsonb(d.key) ORDER BY o.ord),'[]') INTO sorted_defs FROM jsonb_each(definitions) d
                LEFT JOIN jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) ON d.value->'candidates'->0=o.v
                WHERE NOT(reference_order ? d.key);
            child:=0;
            FOR id_value IN SELECT * FROM jsonb_array_elements(reference_order||sorted_defs) LOOP
                IF n>0 OR child>0 THEN st:={p}.oa_separator(st,'block'); END IF;
                def:=definitions->(id_value#>>'{{}}'); begin_byte:=0;
                FOR item IN SELECT * FROM jsonb_array_elements(def->'candidates') LOOP
                    c:=inputs->'source'->(item#>>'{{}}');
                    IF c IS NULL OR c->>'content_state'<>'present' THEN RAISE EXCEPTION 'Present footnote body required' USING ERRCODE='23514'; END IF;
                    IF begin_byte>0 THEN st:={p}.oa_separator(st,'block'); END IF;
                    st:={p}.oa_leaf(st,inputs,(item#>>'{{}}')::uuid,ARRAY['footnote'],false); begin_byte:=begin_byte+1;
                END LOOP;
                child:=child+1;
            END LOOP;
            RETURN st;
        END $$;
        CREATE FUNCTION {p}.oa_compile(inputs jsonb,plan jsonb) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
        DECLARE st jsonb; x jsonb; c jsonb; target jsonb; wanted jsonb; details jsonb;
        BEGIN
            PERFORM {p}.oa_shape(plan,ARRAY['blocks'],ARRAY['footnotes','markers','exclusions'],ARRAY['blocks','footnotes','markers','exclusions']);
            IF jsonb_array_length(coalesce(plan->'exclusions','[]'))>50 THEN RAISE EXCEPTION 'Exclusion bound exceeded' USING ERRCODE='22023'; END IF;
            st:=jsonb_build_object('text','','spans','[]'::jsonb,'normalizations','[]'::jsonb,'separators',jsonb_build_object('block','[]'::jsonb,'cell','[]'::jsonb,'column','[]'::jsonb,'row','[]'::jsonb,'terminator','[]'::jsonb),
                'markers','[]'::jsonb,'notice_ranges','[]'::jsonb,'incomplete_tables','[]'::jsonb,'used','[]'::jsonb,'body_order','[]'::jsonb,'headers','[]'::jsonb,'local_ids','[]'::jsonb,'grid_positions',0);
            st:={p}.oa_scope(st,inputs,plan,NULL);
            FOR x IN SELECT * FROM jsonb_array_elements(coalesce(plan->'exclusions','[]')) LOOP
                PERFORM {p}.oa_shape(x,ARRAY['candidate_id','kind','reason'],ARRAY['target_candidate_id'],ARRAY[]::text[]);
                c:=inputs->'source'->(x->>'candidate_id');
                IF c IS NULL OR st->'used' ? (x->>'candidate_id') OR jsonb_typeof(x->'reason') IS DISTINCT FROM 'string' OR length(btrim(x->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Invalid exclusion' USING ERRCODE='23514'; END IF;
                IF x->>'kind'='repeated_header' THEN
                    target:=inputs->'source'->(x->>'target_candidate_id');
                    IF target IS NULL OR NOT(st->'headers' ? (x->>'target_candidate_id')) OR c->'cell' IS NULL OR c->'cell'='null'::jsonb
                        OR target->'cell'->>'table_local_id' IS DISTINCT FROM c->'cell'->>'table_local_id'
                        OR {p}.oa_norm(c->>'text') IS DISTINCT FROM {p}.oa_norm(target->>'text') OR c->>'content_state' IS DISTINCT FROM target->>'content_state' THEN RAISE EXCEPTION 'Unproven repeated header' USING ERRCODE='23514'; END IF;
                ELSIF x->>'kind' IS DISTINCT FROM 'furniture' OR x->>'target_candidate_id' IS NOT NULL THEN RAISE EXCEPTION 'Unsupported exclusion' USING ERRCODE='23514'; END IF;
                st:=jsonb_set(st,'{{used}}',(st->'used')||to_jsonb(x->>'candidate_id'));
            END LOOP;
            IF jsonb_array_length(st->'used')<>jsonb_array_length(inputs->'order') OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(inputs->'order') o WHERE NOT(st->'used' ? o)) THEN RAISE EXCEPTION 'Unaccounted selected candidate' USING ERRCODE='23514'; END IF;
            SELECT coalesce(jsonb_agg(o.v ORDER BY o.ord),'[]') INTO wanted FROM jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) WHERE st->'body_order' ? (o.v#>>'{{}}');
            IF wanted IS DISTINCT FROM st->'body_order' THEN RAISE EXCEPTION 'Snapshot logical order changed' USING ERRCODE='23514'; END IF;
            IF jsonb_array_length(plan->'blocks')>0 OR jsonb_array_length(coalesce(plan->'footnotes','[]'))>0 THEN st:={p}.oa_separator(st,'terminator'); END IF;
            IF octet_length(st->>'text')>131072 THEN RAISE EXCEPTION 'Output bound exceeded' USING ERRCODE='54000'; END IF;
            details:=(st-ARRAY['text','used','body_order','headers','local_ids','grid_positions'])||jsonb_build_object('publication_eligible',false);
            RETURN jsonb_build_object('text',st->>'text','projection',details);
        END $$;
    """)


def install_storage(p, s, prefix):
    op.execute(f"""
        CREATE FUNCTION {p}.canonical_inputs(command jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE snap record; run {s}.synthetic_run; source jsonb; ordered jsonb; actor uuid;
        BEGIN
            IF command IS NULL OR octet_length(command::text)>65536 OR NOT {p}.operator_keys(command,
                ARRAY['command_id','collection_id','reason','action','proposal_id','snapshot_id','snapshot_revision','snapshot_hash','purpose','plan'],ARRAY['serialization_profile_id'])
                OR command->>'action' IS DISTINCT FROM 'record_canonical_proposal'
                OR coalesce(command->>'serialization_profile_id','OA-text-1')<>'OA-text-1'
                OR jsonb_typeof(command->'snapshot_revision') IS DISTINCT FROM 'number'
                OR command->>'snapshot_revision' !~ '^[0-9]{{1,2}}$' OR (command->>'snapshot_revision')::integer NOT BETWEEN 1 AND 32
                OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string' OR length(btrim(command->>'reason')) NOT BETWEEN 1 AND 2048
                OR command->>'proposal_id' IS NULL OR command->>'command_id' IS NULL THEN RAISE EXCEPTION 'Invalid canonical command' USING ERRCODE='22023'; END IF;
            actor:={p}.current_actor(); PERFORM 1 FROM {p}.actor a WHERE a.id=actor FOR SHARE;
            IF actor IS NULL OR NOT {p}.is_native_operator() OR NOT {p}.assigned(command->>'collection_id','acquire')
                OR NOT {p}.assigned(command->>'collection_id','content') THEN RAISE EXCEPTION 'Scoped native content/acquisition session required' USING ERRCODE='42501'; END IF;
            SELECT t.id,t.revision,t.snapshot_hash,t.run_id,t.artifact_id,t.state INTO snap FROM {s}.staging_snapshot t
                WHERE t.id=(command->>'snapshot_id')::uuid AND t.revision=(command->>'snapshot_revision')::bigint;
            IF snap.id IS NULL OR snap.state<>'active' OR snap.snapshot_hash IS DISTINCT FROM command->>'snapshot_hash' THEN RAISE EXCEPTION 'Current exact snapshot required' USING ERRCODE='42501'; END IF;
            SELECT r.* INTO run FROM {s}.synthetic_run r WHERE r.id=snap.run_id;
            PERFORM {p}.lock_assembly_input(snap.artifact_id,command->>'collection_id',command->>'purpose',true);
            PERFORM 1 FROM {s}.snapshot_head h WHERE h.id=snap.id FOR SHARE;
            IF NOT EXISTS(SELECT 1 FROM {s}.snapshot_head h WHERE h.id=snap.id AND h.revision=snap.revision AND h.run_id=snap.run_id)
                OR EXISTS(SELECT 1 FROM {s}.snapshot_selection sel JOIN {s}.adapter_candidate c ON c.id=sel.candidate_id
                    WHERE sel.snapshot_id=snap.id AND sel.revision=snap.revision AND c.state<>'active') THEN RAISE EXCEPTION 'Active current selection required' USING ERRCODE='42501'; END IF;
            SELECT jsonb_object_agg(c.id::text,c.payload),jsonb_agg(c.payload ORDER BY sel.position) INTO source,ordered
                FROM {s}.snapshot_selection sel JOIN {s}.adapter_candidate c ON c.id=sel.candidate_id
                WHERE sel.snapshot_id=snap.id AND sel.revision=snap.revision;
            RETURN jsonb_build_object('source',source,'candidates',ordered,'order',(SELECT payload->'order' FROM {s}.staging_snapshot WHERE id=snap.id AND revision=snap.revision));
        END $$;
        CREATE FUNCTION {p}.canonical_current(pid uuid,requested_purpose text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {s}.canonical_proposal c JOIN {s}.staging_snapshot t ON t.id=c.snapshot_id AND t.revision=c.snapshot_revision
                JOIN {s}.snapshot_head h ON h.id=t.id AND h.revision=t.revision AND h.run_id=t.run_id
                WHERE c.id=pid AND c.state='active' AND t.state='active' AND t.snapshot_hash=c.snapshot_hash
                    AND {p}.assembly_input_current(c.artifact_id,requested_purpose)
                    AND NOT EXISTS(SELECT 1 FROM {s}.snapshot_selection sel JOIN {s}.adapter_candidate d ON d.id=sel.candidate_id
                        WHERE sel.snapshot_id=t.id AND sel.revision=t.revision AND d.state<>'active'))
        $$;
        CREATE FUNCTION {p}.validate_canonical_update() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            IF TG_OP<>'UPDATE' OR (to_jsonb(NEW)-ARRAY['state','payload','canonical_bytes']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','payload','canonical_bytes'])
                OR ((NEW.payload,NEW.canonical_bytes) IS DISTINCT FROM (OLD.payload,OLD.canonical_bytes)
                    AND NOT(NEW.state='erased' AND NEW.payload IS NULL AND NEW.canonical_bytes IS NULL))
                OR NOT((NEW.state='held' AND OLD.state='active') OR (NEW.state='erasure_required' AND OLD.state IN ('active','held'))
                    OR (NEW.state='erased' AND OLD.state IN ('active','held','erasure_required')))
                OR NOT EXISTS(SELECT 1 FROM {p}.assembly_lifecycle_event e WHERE e.artifact_id=NEW.artifact_id AND e.target=NEW.state
                    AND e.actor_id={p}.current_actor() AND e.transaction_id=pg_current_xact_id()) THEN RAISE EXCEPTION 'Immutable audited canonical lifecycle required' USING ERRCODE='55000'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER canonical_update BEFORE UPDATE OR DELETE ON {s}.canonical_proposal FOR EACH ROW EXECUTE FUNCTION {p}.validate_canonical_update();
        CREATE TRIGGER no_truncate BEFORE TRUNCATE ON {s}.canonical_proposal FOR EACH STATEMENT EXECUTE FUNCTION {p}.immutable_record();
        CREATE FUNCTION {p}.hold_stale_canonical(artifact uuid) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE count_value integer; next_rev bigint; audit uuid; item record;
        BEGIN
            SELECT id,collection_id,assessment_id,assessment_revision,purpose INTO item FROM {s}.staged_artifact WHERE id=artifact FOR UPDATE;
            SELECT count(*) INTO count_value FROM {s}.canonical_proposal c WHERE c.artifact_id=artifact AND c.state='active' AND NOT {p}.canonical_current(c.id,item.purpose);
            IF count_value=0 THEN RETURN 0; END IF;
            SELECT coalesce(max(e.revision),0)+1 INTO next_rev FROM {p}.assembly_lifecycle_event e WHERE e.artifact_id=artifact;
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT {p}.current_actor(),item.collection_id,a.evidence_id,'assembly_lifecycle',artifact::text,next_rev,'Snapshot authority no longer current'
                FROM {p}.acquisition_assessment a WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision RETURNING id INTO audit;
            INSERT INTO {p}.assembly_lifecycle_event(artifact_id,revision,actor_id,target,cause,changed_count,audit_id)
                VALUES(artifact,next_rev,{p}.current_actor(),'held','derivation_not_current',count_value,audit);
            UPDATE {s}.canonical_proposal c SET state='held' WHERE c.artifact_id=artifact AND c.state='active' AND NOT {p}.canonical_current(c.id,item.purpose);
            RETURN count_value;
        END $$;
        CREATE FUNCTION {p}.store_canonical_proposal(command jsonb,expected_bytes bytea,expected_projection jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE inputs jsonb; compiled jsonb; bytes bytea; content_hash text; payload jsonb; fingerprint text;
            previous {p}.assembly_receipt; pid uuid; cmd uuid; actor uuid; snap record; run {s}.synthetic_run; result jsonb;
        BEGIN
            inputs:={p}.canonical_inputs(command);
            cmd:=(command->>'command_id')::uuid; pid:=(command->>'proposal_id')::uuid; actor:={p}.current_actor();
            fingerprint:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended('{prefix}:assembly:'||cmd::text,0));
            SELECT * INTO previous FROM {p}.assembly_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>actor THEN RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Canonical retry changed' USING ERRCODE='40001'; END IF;
                IF NOT {p}.canonical_current(pid,command->>'purpose') THEN RAISE EXCEPTION 'Held canonical retry cannot resume' USING ERRCODE='42501'; END IF;
                RETURN previous.result||jsonb_build_object('replayed',true);
            END IF;
            compiled:={p}.oa_compile(inputs,command->'plan'); bytes:=convert_to(compiled->>'text','UTF8');
            IF expected_bytes IS DISTINCT FROM bytes OR expected_projection IS DISTINCT FROM compiled->'projection' THEN RAISE EXCEPTION 'Independent serializer disagreement' USING ERRCODE='23514'; END IF;
            content_hash:=encode(sha256(bytes),'hex'); payload:=jsonb_build_object('plan',command->'plan','projection',compiled->'projection');
            IF octet_length(payload::text)>262144 THEN RAISE EXCEPTION 'Canonical metadata bound exceeded' USING ERRCODE='54000'; END IF;
            SELECT t.id,t.revision,t.snapshot_hash,t.run_id,t.artifact_id INTO snap FROM {s}.staging_snapshot t WHERE t.id=(command->>'snapshot_id')::uuid AND t.revision=(command->>'snapshot_revision')::bigint;
            SELECT r.* INTO run FROM {s}.synthetic_run r WHERE r.id=snap.run_id;
            PERFORM {p}.reserve_assembly_capacity(snap.artifact_id,run.collection_id,octet_length(payload::text)+octet_length(bytes),1);
            INSERT INTO {s}.canonical_proposal(id,snapshot_id,snapshot_revision,snapshot_hash,run_id,artifact_id,collection_id,serialization_profile_id,profile_hash,
                canonical_bytes,payload,content_hash,payload_hash,size_bytes,payload_bytes,actor_id)
                VALUES(pid,snap.id,snap.revision,snap.snapshot_hash,snap.run_id,snap.artifact_id,run.collection_id,'OA-text-1','{PROFILE_HASH}',bytes,payload,content_hash,
                    encode(sha256(convert_to(payload::text,'UTF8')),'hex'),octet_length(bytes),octet_length(payload::text),actor);
            INSERT INTO {p}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT actor,run.collection_id,a.evidence_id,'canonical_proposal',pid::text,1,command->>'reason'
                    FROM {p}.acquisition_assessment a WHERE a.id=run.assessment_id AND a.revision=run.assessment_revision;
            result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'object_id',pid,'revision',1,'output_hash',content_hash,
                'scanned',0,'held',0,'erasure_required',0,'erased',0,'next_after',NULL);
            INSERT INTO {p}.assembly_receipt(command_id,actor_id,collection_id,action,payload_hash,result)
                VALUES(cmd,actor,run.collection_id,'record_canonical_proposal',fingerprint,result);
            RETURN result;
        END $$;
        CREATE FUNCTION {p}.read_canonical_proposal(pid uuid,requested_purpose text) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE meta record; result jsonb;
        BEGIN
            SELECT id,artifact_id,collection_id,snapshot_id INTO meta FROM {s}.canonical_proposal WHERE id=pid;
            IF meta.id IS NULL THEN RETURN NULL; END IF;
            PERFORM {p}.lock_assembly_input(meta.artifact_id,meta.collection_id,requested_purpose,false);
            PERFORM 1 FROM {s}.snapshot_head WHERE id=meta.snapshot_id FOR SHARE;
            IF NOT {p}.canonical_current(pid,requested_purpose) THEN RETURN NULL; END IF;
            SELECT jsonb_build_object('proposal_id',id,'snapshot_id',snapshot_id,'snapshot_revision',snapshot_revision,'snapshot_hash',snapshot_hash,
                'serialization_profile_id',serialization_profile_id,'profile_hash',profile_hash,'content_hash',content_hash,'canonical_text',convert_from(canonical_bytes,'UTF8'),'payload',payload)
                INTO result FROM {s}.canonical_proposal WHERE id=pid AND state='active' FOR SHARE;
            RETURN result;
        END $$;
    """)


def extend_lifecycle(p, s):
    conn = op.get_bind()
    patches = {
        "staging_live_bytes(text)": (
            f"+coalesce((SELECT sum(octet_length(t.payload::text)) FROM {s}.staging_snapshot t JOIN {s}.synthetic_run r ON r.id=t.run_id WHERE r.collection_id=cid),0)",
            f"+coalesce((SELECT sum(octet_length(t.payload::text)) FROM {s}.staging_snapshot t JOIN {s}.synthetic_run r ON r.id=t.run_id WHERE r.collection_id=cid),0)\n                +coalesce((SELECT sum(octet_length(c.payload::text)+octet_length(c.canonical_bytes)) FROM {s}.canonical_proposal c WHERE c.collection_id=cid),0)",
        ),
        "reserve_assembly_capacity(uuid,text,bigint,integer)": (
            f"UNION ALL SELECT octet_length(payload::text) FROM {s}.staging_snapshot WHERE artifact_id=artifact)",
            f"UNION ALL SELECT octet_length(payload::text) FROM {s}.staging_snapshot WHERE artifact_id=artifact\n                UNION ALL SELECT octet_length(payload::text)+octet_length(canonical_bytes) FROM {s}.canonical_proposal WHERE artifact_id=artifact)",
        ),
        "transition_assembly(uuid,text,text)": (
            f"UNION ALL SELECT 1 FROM {s}.staging_snapshot WHERE artifact_id=artifact AND state=ANY(eligible))",
            f"UNION ALL SELECT 1 FROM {s}.staging_snapshot WHERE artifact_id=artifact AND state=ANY(eligible)\n                UNION ALL SELECT 1 FROM {s}.canonical_proposal WHERE artifact_id=artifact AND state=ANY(eligible))",
        ),
        "apply_assembly_command(jsonb)": ("erased:=erased+", "erased:=erased+"),
    }
    for signature, (anchor, replacement) in patches.items():
        definition = conn.exec_driver_sql(
            "SELECT pg_get_functiondef(to_regprocedure(%s))", (p + "." + signature,)
        ).scalar_one()
        if signature.startswith("apply_assembly_command"):
            anchor = f"erased:=erased+{p}.transition_assembly(row_item.id,'erased','due_erasure');\n                    END IF;"
            replacement = anchor + f"\n                    held:=held+{p}.hold_stale_canonical(row_item.id);"
            # Required canonical-only erasure also remains sticky after reassessment.
            original = f"OR EXISTS(SELECT 1 FROM {s}.staging_snapshot WHERE artifact_id=row_item.id AND state='erasure_required'))"
            if definition.count(original) != 1:
                raise RuntimeError("Installed derivative reconciliation requires boundary review")
            definition = definition.replace(
                original,
                original[:-1]
                + f"\n                        OR EXISTS(SELECT 1 FROM {s}.canonical_proposal WHERE artifact_id=row_item.id AND state='erasure_required'))",
                1,
            )
        if definition.count(anchor) != 1:
            raise RuntimeError("Installed derivative lifecycle requires boundary review")
        base = signature.split("(")[0]
        # Rename the installed function, retaining its exact body and grants for rollback.
        op.execute(f"ALTER FUNCTION {p}.{signature} RENAME TO {base}_m31")
        updated = definition.replace(anchor, replacement, 1)
        if base == "transition_assembly":
            final = "            RETURN changed;"
            if updated.count(final) != 1:
                raise RuntimeError("Installed erasure requires boundary review")
            updated = updated.replace(
                final,
                f"            UPDATE {s}.canonical_proposal SET state=following,payload=CASE WHEN following='erased' THEN NULL ELSE payload END,\n                canonical_bytes=CASE WHEN following='erased' THEN NULL ELSE canonical_bytes END WHERE artifact_id=artifact AND state=ANY(eligible);\n"
                + final,
                1,
            )
        op.execute(updated)


def downgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    p, s, _, _ = names()
    if conn.exec_driver_sql(
        f"SELECT EXISTS(SELECT 1 FROM {s}.canonical_proposal) OR EXISTS(SELECT 1 FROM {p}.assembly_receipt WHERE action='record_canonical_proposal')"
    ).scalar_one():
        raise RuntimeError("Private canonical workflow requires forward repair")
    op.execute(f"DROP TRIGGER canonical_update ON {s}.canonical_proposal")
    for signature in signatures()[-4:]:
        base = signature.split("(")[0]
        op.execute(f"DROP FUNCTION {p}.{signature}")
        args = signature[signature.index("(") :]
        op.execute(f"ALTER FUNCTION {p}.{base}_m31{args} RENAME TO {base}")
    for fn in signatures()[:-4]:
        op.execute(f"DROP FUNCTION {p}.{fn}")
    op.execute(f"""
        DROP TABLE {s}.canonical_proposal;
        ALTER TABLE {s}.staging_snapshot DROP CONSTRAINT canonical_snapshot_lineage;
        ALTER TABLE {p}.assembly_receipt DROP CONSTRAINT assembly_receipt_action_check,
            ADD CONSTRAINT assembly_receipt_action_check CHECK(action IN ('record_synthetic_run','record_snapshot','reconcile_assembly'));
    """)
