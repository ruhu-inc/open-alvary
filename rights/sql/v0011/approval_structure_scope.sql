CREATE OR REPLACE FUNCTION {{policy}}.approval_structure_scope(st jsonb, inputs jsonb, body jsonb, projection jsonb, scope text, parent_key text)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE item jsonb; cell jsonb; source jsonb; origins jsonb; shape jsonb; id_value jsonb;
            nodes jsonb; grid jsonb; table_key text; key text; start_value integer; n integer:=0; row_value integer; col_value integer; child integer;
            foot jsonb; foot_key text; foot_index integer:=0; ordered_footnotes jsonb;
        BEGIN
            FOR item IN SELECT * FROM jsonb_array_elements(body->'blocks') LOOP
                IF n>0 THEN st:=jsonb_set(st,'{offset}',to_jsonb((st->>'offset')::integer+2)); END IF;
                start_value:=(st->>'offset')::integer;
                IF item->>'kind'='text' THEN st:={{policy}}.approval_leaf(st,inputs,item->>'candidate_id',parent_key,(inputs->'source'->(item->>'candidate_id'))->>'block_type');
                ELSIF item->>'kind'='notice' THEN
                    key:='notice:'||(item->>'local_id');
                    st:={{policy}}.approval_structure_scope(st,inputs,item,projection,item->>'local_id',key);
                    st:=jsonb_set(st,'{nodes}',(st->'nodes')||jsonb_build_array(jsonb_build_object('key',key,'parent',parent_key,'kind','notice','candidate_id',NULL,'start',start_value,'end',(st->>'offset')::integer)));
                ELSE
                    table_key:='table:'||(item->>'table_local_id'); origins:='{}';grid:='[]';
                    FOR cell IN SELECT * FROM jsonb_array_elements(item->'cells') LOOP
                        shape:=inputs->'source'->(cell->'candidates'->>0)->'cell';
                        key:=(shape->>'row')||':'||(shape->>'column');origins:=jsonb_set(origins,ARRAY[key],cell);
                        grid:=grid||jsonb_build_array(jsonb_build_object('row',(shape->>'row')::integer,'column',(shape->>'column')::integer,
                            'row_span',(shape->>'row_span')::integer,'column_span',(shape->>'column_span')::integer,
                            'candidates',cell->'candidates','content_state',inputs->'source'->(cell->'candidates'->>0)->>'content_state'));
                    END LOOP;
                    FOR row_value IN 0..(item->>'rows')::integer-1 LOOP
                        IF row_value>0 THEN st:=jsonb_set(st,'{offset}',to_jsonb((st->>'offset')::integer+1)); END IF;
                        FOR col_value IN 0..(item->>'columns')::integer-1 LOOP
                            IF col_value>0 THEN st:=jsonb_set(st,'{offset}',to_jsonb((st->>'offset')::integer+1)); END IF;
                            cell:=origins->(row_value::text||':'||col_value::text); child:=0;
                            IF cell IS NOT NULL THEN
                                FOR id_value IN SELECT * FROM jsonb_array_elements(cell->'candidates') LOOP
                                    IF child>0 THEN st:=jsonb_set(st,'{offset}',to_jsonb((st->>'offset')::integer+2)); END IF;
                                    st:={{policy}}.approval_leaf(st,inputs,id_value#>>'{}',table_key,'table_cell');child:=child+1;
                                END LOOP;
                            END IF;
                        END LOOP;
                    END LOOP;
                    st:=jsonb_set(st,'{nodes}',(st->'nodes')||jsonb_build_array(jsonb_build_object('key',table_key,'parent',parent_key,'kind','table','candidate_id',NULL,'start',start_value,'end',(st->>'offset')::integer)));
                    st:=jsonb_set(st,'{tables}',(st->'tables')||jsonb_build_array(jsonb_build_object('key',table_key,'table_local_id',item->>'table_local_id',
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
                IF n>0 OR foot_index>0 THEN st:=jsonb_set(st,'{offset}',to_jsonb((st->>'offset')::integer+2)); END IF;
                start_value:=(st->>'offset')::integer;foot_key:='foot:'||coalesce(scope,'document')||':'||(foot->>'local_id');child:=0;
                FOR id_value IN SELECT * FROM jsonb_array_elements(foot->'candidates') LOOP
                    IF child>0 THEN st:=jsonb_set(st,'{offset}',to_jsonb((st->>'offset')::integer+2)); END IF;
                    st:={{policy}}.approval_leaf(st,inputs,id_value#>>'{}',foot_key,'footnote');child:=child+1;
                END LOOP;
                st:=jsonb_set(st,'{nodes}',(st->'nodes')||jsonb_build_array(jsonb_build_object('key',foot_key,'parent',parent_key,'kind','footnote_group','candidate_id',NULL,'start',start_value,'end',(st->>'offset')::integer)));
                foot_index:=foot_index+1;
            END LOOP;
            RETURN st;
        END $function$
