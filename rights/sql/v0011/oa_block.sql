CREATE OR REPLACE FUNCTION {{policy}}.oa_block(st jsonb, inputs jsonb, item jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE c jsonb; first_c jsonb; shape jsonb; cell jsonb; absent jsonb; id_value jsonb; h jsonb;
            positions jsonb:='{}'; origins jsonb:='{}'; covering jsonb; members jsonb:='[]'; key text;
            nr integer; nc integer; rr integer; cc integer; r integer; col integer; rs integer; cs integer; n integer;
            partial boolean:=false;
        BEGIN
            IF item->>'kind'='text' THEN
                PERFORM {{policy}}.oa_shape(item,ARRAY['kind','candidate_id'],ARRAY[]::text[],ARRAY[]::text[]);
                c:=inputs->'source'->(item->>'candidate_id');
                IF c IS NULL OR c->>'content_state'<>'present' THEN RAISE EXCEPTION 'Present literal block required' USING ERRCODE='23514'; END IF;
                RETURN {{policy}}.oa_leaf(st,inputs,(item->>'candidate_id')::uuid,ARRAY['heading','paragraph','list_item'],true);
            END IF;
            IF item->>'kind' IS DISTINCT FROM 'table' THEN RAISE EXCEPTION 'Unsupported block' USING ERRCODE='22023'; END IF;
            PERFORM {{policy}}.oa_shape(item,ARRAY['kind','table_local_id','rows','columns','cells'],ARRAY['headers','unavailable'],ARRAY['cells','headers','unavailable']);
            IF NOT {{policy}}.assembly_local_key(item->'table_local_id',false) OR (st->'local_ids') ? (item->>'table_local_id')
                OR jsonb_typeof(item->'rows') IS DISTINCT FROM 'number' OR item->>'rows' !~ '^[0-9]{1,3}$'
                OR jsonb_typeof(item->'columns') IS DISTINCT FROM 'number' OR item->>'columns' !~ '^[0-9]{1,3}$' THEN RAISE EXCEPTION 'Bounded table required' USING ERRCODE='22023'; END IF;
            nr:=(item->>'rows')::integer; nc:=(item->>'columns')::integer;
            IF nr NOT BETWEEN 1 AND 100 OR nc NOT BETWEEN 1 AND 100 OR (st->>'grid_positions')::integer+nr*nc>10000
                OR jsonb_array_length(item->'cells') NOT BETWEEN 1 AND 50 OR jsonb_array_length(coalesce(item->'headers','[]'))>50 THEN RAISE EXCEPTION 'Table grid bound exceeded' USING ERRCODE='22023'; END IF;
            st:=jsonb_set(st,'{local_ids}',(st->'local_ids')||to_jsonb(item->>'table_local_id'));
            st:=jsonb_set(st,'{grid_positions}',to_jsonb((st->>'grid_positions')::integer+nr*nc));
            FOR cell IN SELECT * FROM jsonb_array_elements(item->'cells') LOOP
                PERFORM {{policy}}.oa_shape(cell,ARRAY['candidates'],ARRAY[]::text[],ARRAY['candidates']);
                IF jsonb_array_length(cell->'candidates') NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Bounded cell children required' USING ERRCODE='22023'; END IF;
                first_c:=inputs->'source'->(cell->'candidates'->>0); shape:=first_c->'cell';
                IF shape IS NULL OR shape='null'::jsonb OR shape->>'table_local_id' IS DISTINCT FROM item->>'table_local_id' THEN RAISE EXCEPTION 'Explicit table lineage required' USING ERRCODE='23514'; END IF;
                rr:=(shape->>'row')::integer; cc:=(shape->>'column')::integer; rs:=(shape->>'row_span')::integer; cs:=(shape->>'column_span')::integer;
                IF rr+rs>nr OR cc+cs>nc THEN RAISE EXCEPTION 'Cell outside declared table' USING ERRCODE='23514'; END IF;
                FOR id_value IN SELECT * FROM jsonb_array_elements(cell->'candidates') LOOP
                    c:=inputs->'source'->(id_value#>>'{}');
                    IF c IS NULL OR c->'cell' IS DISTINCT FROM shape OR (members ? (id_value#>>'{}'))
                        OR (jsonb_array_length(cell->'candidates')>1 AND c->>'content_state'<>'present') THEN RAISE EXCEPTION 'Invalid cell children' USING ERRCODE='23514'; END IF;
                    members:=members||id_value;
                END LOOP;
                SELECT jsonb_object_agg(grid_row.n::text||':'||grid_col.n::text,'covered') INTO covering
                    FROM generate_series(rr,rr+rs-1) grid_row(n) CROSS JOIN generate_series(cc,cc+cs-1) grid_col(n);
                IF EXISTS(SELECT 1 FROM jsonb_object_keys(covering) k WHERE positions ? k) THEN RAISE EXCEPTION 'Overlapping cell grid' USING ERRCODE='23514'; END IF;
                positions:=positions||covering; origins:=jsonb_set(origins,ARRAY[rr::text||':'||cc::text],cell);
            END LOOP;
            FOR absent IN SELECT * FROM jsonb_array_elements(coalesce(item->'unavailable','[]')) LOOP
                PERFORM {{policy}}.oa_shape(absent,ARRAY['row','column','content_state','reason'],ARRAY[]::text[],ARRAY[]::text[]);
                IF jsonb_typeof(absent->'row') IS DISTINCT FROM 'number' OR absent->>'row' !~ '^[0-9]{1,2}$'
                    OR jsonb_typeof(absent->'column') IS DISTINCT FROM 'number' OR absent->>'column' !~ '^[0-9]{1,2}$'
                    OR coalesce(absent->>'content_state','') NOT IN ('illegible','unsupported')
                    OR jsonb_typeof(absent->'reason') IS DISTINCT FROM 'string' OR length(btrim(absent->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Explicit unavailable grid position required' USING ERRCODE='22023'; END IF;
                rr:=(absent->>'row')::integer; cc:=(absent->>'column')::integer; key:=rr::text||':'||cc::text;
                IF rr>=nr OR cc>=nc OR positions ? key THEN RAISE EXCEPTION 'Invalid unavailable grid position' USING ERRCODE='23514'; END IF;
                positions:=jsonb_set(positions,ARRAY[key],'"unavailable"'); partial:=true;
            END LOOP;
            IF (SELECT count(*) FROM jsonb_object_keys(positions))<>nr*nc THEN RAISE EXCEPTION 'Missing grid position' USING ERRCODE='23514'; END IF;
            FOR h IN SELECT * FROM jsonb_array_elements(coalesce(item->'headers','[]')) LOOP
                IF NOT(members ? (h#>>'{}')) OR st->'headers' ? (h#>>'{}') THEN RAISE EXCEPTION 'Invalid table header' USING ERRCODE='23514'; END IF;
                st:=jsonb_set(st,'{headers}',(st->'headers')||h);
            END LOOP;
            FOR r IN 0..nr-1 LOOP
                IF r>0 THEN st:={{policy}}.oa_separator(st,'row'); END IF;
                FOR col IN 0..nc-1 LOOP
                    IF col>0 THEN st:={{policy}}.oa_separator(st,'column'); END IF;
                    cell:=origins->(r::text||':'||col::text);
                    IF cell IS NULL THEN CONTINUE; END IF;
                    n:=0;
                    FOR id_value IN SELECT * FROM jsonb_array_elements(cell->'candidates') LOOP
                        IF n>0 THEN st:={{policy}}.oa_separator(st,'cell'); END IF;
                        st:={{policy}}.oa_leaf(st,inputs,(id_value#>>'{}')::uuid,ARRAY['table_cell'],true);
                        c:=inputs->'source'->(id_value#>>'{}'); partial:=partial OR c->>'content_state' IN ('illegible','unsupported'); n:=n+1;
                    END LOOP;
                END LOOP;
            END LOOP;
            IF partial THEN st:=jsonb_set(st,'{incomplete_tables}',(st->'incomplete_tables')||to_jsonb(item->>'table_local_id')); END IF;
            RETURN st;
        END $function$
