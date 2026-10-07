CREATE OR REPLACE FUNCTION {{policy}}.oa_scope(st jsonb, inputs jsonb, scope_body jsonb, scope_name text)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE item jsonb; foot jsonb; marker jsonb; c jsonb; span jsonb; def jsonb; id_value jsonb;
            definitions jsonb:='{}'; reference_order jsonb:='[]'; seen jsonb:='[]'; previous_ends jsonb:='{}';
            direct jsonb:='[]'; before_used integer; begin_byte integer; finish_byte integer; n integer:=0; child integer;
            raw bytea; prefix_bytes bytea; end_bytes bytea; marker_bytes bytea; whole bytea; a integer; z integer; key text;
            sorted_defs jsonb:='[]'; fn_ids jsonb; old_notice_count integer;
        BEGIN
            PERFORM {{policy}}.oa_shape(scope_body,ARRAY['blocks'],ARRAY['footnotes','markers','exclusions'],ARRAY['blocks','footnotes','markers','exclusions']);
            IF jsonb_array_length(scope_body->'blocks')>50 OR jsonb_array_length(coalesce(scope_body->'footnotes','[]'))>50
                OR jsonb_array_length(coalesce(scope_body->'markers','[]'))>100 THEN RAISE EXCEPTION 'Scope bound exceeded' USING ERRCODE='22023'; END IF;
            before_used:=jsonb_array_length(st->'used'); old_notice_count:=jsonb_array_length(st->'notice_ranges');
            FOR item IN SELECT * FROM jsonb_array_elements(scope_body->'blocks') LOOP
                IF n>0 THEN st:={{policy}}.oa_separator(st,'block'); END IF;
                IF item->>'kind'='notice' THEN
                    IF scope_name IS NOT NULL THEN RAISE EXCEPTION 'Nested notice unsupported' USING ERRCODE='22023'; END IF;
                    PERFORM {{policy}}.oa_shape(item,ARRAY['kind','local_id','blocks'],ARRAY['footnotes','markers'],ARRAY['blocks','footnotes','markers']);
                    IF NOT {{policy}}.assembly_local_key(item->'local_id',false) OR st->'local_ids' ? (item->>'local_id') OR jsonb_array_length(item->'blocks')<1 THEN RAISE EXCEPTION 'Invalid notice scope' USING ERRCODE='23514'; END IF;
                    st:=jsonb_set(st,'{local_ids}',(st->'local_ids')||to_jsonb(item->>'local_id'));
                    begin_byte:=octet_length(st->>'text');
                    st:={{policy}}.oa_scope(st,inputs,jsonb_build_object('blocks',item->'blocks','footnotes',coalesce(item->'footnotes','[]'),'markers',coalesce(item->'markers','[]')),item->>'local_id');
                    finish_byte:=octet_length(st->>'text');
                    st:=jsonb_set(st,'{notice_ranges}',(st->'notice_ranges')||jsonb_build_array(jsonb_build_object('local_id',item->>'local_id','start',begin_byte,'end',finish_byte,
                        'content_hash',encode(sha256(substring(convert_to(st->>'text','UTF8') FROM begin_byte+1 FOR finish_byte-begin_byte)),'hex'))));
                ELSE st:={{policy}}.oa_block(st,inputs,item); END IF;
                n:=n+1;
            END LOOP;
            SELECT coalesce(jsonb_agg(u.v ORDER BY u.ord),'[]') INTO direct FROM jsonb_array_elements(st->'used') WITH ORDINALITY u(v,ord) WHERE ord>before_used;
            IF scope_name IS NULL THEN
                SELECT coalesce(jsonb_agg(d.v ORDER BY d.ord),'[]') INTO direct FROM jsonb_array_elements(direct) WITH ORDINALITY d(v,ord)
                    WHERE NOT EXISTS(SELECT 1 FROM jsonb_array_elements(st->'spans') sp CROSS JOIN jsonb_array_elements(st->'notice_ranges') nr
                        WHERE sp->>'candidate_id'=d.v#>>'{}' AND (sp->>'start')::integer>=(nr->>'start')::integer AND (sp->>'end')::integer<=(nr->>'end')::integer);
            END IF;
            FOR foot IN SELECT * FROM jsonb_array_elements(coalesce(scope_body->'footnotes','[]')) LOOP
                PERFORM {{policy}}.oa_shape(foot,ARRAY['local_id','candidates'],ARRAY[]::text[],ARRAY['candidates']);
                IF NOT {{policy}}.assembly_local_key(foot->'local_id',false) OR definitions ? (foot->>'local_id') OR jsonb_array_length(foot->'candidates') NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Invalid footnote definition' USING ERRCODE='23514'; END IF;
                SELECT coalesce(jsonb_agg(c.v ORDER BY o.ord),'[]') INTO fn_ids FROM jsonb_array_elements(foot->'candidates') c(v)
                    LEFT JOIN jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) ON c.v=o.v;
                IF fn_ids IS DISTINCT FROM foot->'candidates' THEN RAISE EXCEPTION 'Footnote source order changed' USING ERRCODE='23514'; END IF;
                definitions:=jsonb_set(definitions,ARRAY[foot->>'local_id'],foot);
            END LOOP;
            FOR marker IN SELECT m.v FROM jsonb_array_elements(coalesce(scope_body->'markers','[]')) m(v)
                LEFT JOIN jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) ON m.v->'candidate_id'=o.v
                ORDER BY o.ord,(m.v->>'source_start')::integer
            LOOP
                PERFORM {{policy}}.oa_shape(marker,ARRAY['candidate_id','source_start','source_end','footnote_id'],ARRAY[]::text[],ARRAY[]::text[]);
                IF NOT(direct ? (marker->>'candidate_id')) OR NOT(definitions ? (marker->>'footnote_id'))
                    OR jsonb_typeof(marker->'source_start') IS DISTINCT FROM 'number' OR marker->>'source_start' !~ '^[0-9]{1,5}$'
                    OR jsonb_typeof(marker->'source_end') IS DISTINCT FROM 'number' OR marker->>'source_end' !~ '^[0-9]{1,5}$' THEN RAISE EXCEPTION 'Invalid marker scope/range' USING ERRCODE='23514'; END IF;
                a:=(marker->>'source_start')::integer; z:=(marker->>'source_end')::integer;
                c:=inputs->'source'->(marker->>'candidate_id'); raw:=convert_to(c->>'text','UTF8');
                IF a<coalesce((previous_ends->>(marker->>'candidate_id'))::integer,0) OR a<0 OR z<=a OR z>octet_length(raw) OR z>32768 THEN RAISE EXCEPTION 'Invalid or overlapping marker' USING ERRCODE='23514'; END IF;
                previous_ends:=jsonb_set(previous_ends,ARRAY[marker->>'candidate_id'],to_jsonb(z));
                prefix_bytes:=convert_to({{policy}}.oa_norm(convert_from(substring(raw FROM 1 FOR a),'UTF8')),'UTF8');
                end_bytes:=convert_to({{policy}}.oa_norm(convert_from(substring(raw FROM 1 FOR z),'UTF8')),'UTF8');
                marker_bytes:=convert_to({{policy}}.oa_norm(convert_from(substring(raw FROM a+1 FOR z-a),'UTF8')),'UTF8'); whole:=convert_to({{policy}}.oa_norm(c->>'text'),'UTF8');
                IF substring(whole FROM 1 FOR octet_length(prefix_bytes))<>prefix_bytes OR substring(whole FROM 1 FOR octet_length(end_bytes))<>end_bytes
                    OR substring(whole FROM octet_length(prefix_bytes)+1 FOR octet_length(end_bytes)-octet_length(prefix_bytes))<>marker_bytes OR octet_length(marker_bytes)=0 THEN RAISE EXCEPTION 'Ambiguous normalized marker' USING ERRCODE='23514'; END IF;
                SELECT sp INTO span FROM jsonb_array_elements(st->'spans') sp WHERE sp->>'candidate_id'=marker->>'candidate_id';
                st:=jsonb_set(st,'{markers}',(st->'markers')||jsonb_build_array(jsonb_build_object('candidate_id',(marker->>'candidate_id')::uuid,'source_start',a,'source_end',z,
                    'start',(span->>'start')::integer+octet_length(prefix_bytes),'end',(span->>'start')::integer+octet_length(end_bytes),
                    'marker_hash',encode(sha256(marker_bytes),'hex'),'footnote_id',marker->>'footnote_id','scope',scope_name)));
                IF NOT(reference_order ? (marker->>'footnote_id')) THEN reference_order:=reference_order||to_jsonb(marker->>'footnote_id'); END IF;
            END LOOP;
            SELECT coalesce(jsonb_agg(to_jsonb(d.key) ORDER BY o.ord),'[]') INTO sorted_defs FROM jsonb_each(definitions) d
                LEFT JOIN jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) ON d.value->'candidates'->0=o.v
                WHERE NOT(reference_order ? d.key);
            child:=0;
            FOR id_value IN SELECT * FROM jsonb_array_elements(reference_order||sorted_defs) LOOP
                IF n>0 OR child>0 THEN st:={{policy}}.oa_separator(st,'block'); END IF;
                def:=definitions->(id_value#>>'{}'); begin_byte:=0;
                FOR item IN SELECT * FROM jsonb_array_elements(def->'candidates') LOOP
                    c:=inputs->'source'->(item#>>'{}');
                    IF c IS NULL OR c->>'content_state'<>'present' THEN RAISE EXCEPTION 'Present footnote body required' USING ERRCODE='23514'; END IF;
                    IF begin_byte>0 THEN st:={{policy}}.oa_separator(st,'block'); END IF;
                    st:={{policy}}.oa_leaf(st,inputs,(item#>>'{}')::uuid,ARRAY['footnote'],false); begin_byte:=begin_byte+1;
                END LOOP;
                child:=child+1;
            END LOOP;
            RETURN st;
        END $function$
