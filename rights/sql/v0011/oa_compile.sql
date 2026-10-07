CREATE OR REPLACE FUNCTION {{policy}}.oa_compile(inputs jsonb, plan jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE st jsonb; x jsonb; c jsonb; target jsonb; wanted jsonb; details jsonb;
        BEGIN
            PERFORM {{policy}}.oa_shape(plan,ARRAY['blocks'],ARRAY['footnotes','markers','exclusions'],ARRAY['blocks','footnotes','markers','exclusions']);
            IF jsonb_array_length(coalesce(plan->'exclusions','[]'))>50 THEN RAISE EXCEPTION 'Exclusion bound exceeded' USING ERRCODE='22023'; END IF;
            st:=jsonb_build_object('text','','spans','[]'::jsonb,'normalizations','[]'::jsonb,'separators',jsonb_build_object('block','[]'::jsonb,'cell','[]'::jsonb,'column','[]'::jsonb,'row','[]'::jsonb,'terminator','[]'::jsonb),
                'markers','[]'::jsonb,'notice_ranges','[]'::jsonb,'incomplete_tables','[]'::jsonb,'used','[]'::jsonb,'body_order','[]'::jsonb,'headers','[]'::jsonb,'local_ids','[]'::jsonb,'grid_positions',0);
            st:={{policy}}.oa_scope(st,inputs,plan,NULL);
            FOR x IN SELECT * FROM jsonb_array_elements(coalesce(plan->'exclusions','[]')) LOOP
                PERFORM {{policy}}.oa_shape(x,ARRAY['candidate_id','kind','reason'],ARRAY['target_candidate_id'],ARRAY[]::text[]);
                c:=inputs->'source'->(x->>'candidate_id');
                IF c IS NULL OR st->'used' ? (x->>'candidate_id') OR jsonb_typeof(x->'reason') IS DISTINCT FROM 'string' OR length(btrim(x->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Invalid exclusion' USING ERRCODE='23514'; END IF;
                IF x->>'kind'='repeated_header' THEN
                    target:=inputs->'source'->(x->>'target_candidate_id');
                    IF target IS NULL OR NOT(st->'headers' ? (x->>'target_candidate_id')) OR c->'cell' IS NULL OR c->'cell'='null'::jsonb
                        OR target->'cell'->>'table_local_id' IS DISTINCT FROM c->'cell'->>'table_local_id'
                        OR {{policy}}.oa_norm(c->>'text') IS DISTINCT FROM {{policy}}.oa_norm(target->>'text') OR c->>'content_state' IS DISTINCT FROM target->>'content_state' THEN RAISE EXCEPTION 'Unproven repeated header' USING ERRCODE='23514'; END IF;
                ELSIF x->>'kind' IS DISTINCT FROM 'furniture' OR x->>'target_candidate_id' IS NOT NULL THEN RAISE EXCEPTION 'Unsupported exclusion' USING ERRCODE='23514'; END IF;
                st:=jsonb_set(st,'{used}',(st->'used')||to_jsonb(x->>'candidate_id'));
            END LOOP;
            IF jsonb_array_length(st->'used')<>jsonb_array_length(inputs->'order') OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(inputs->'order') o WHERE NOT(st->'used' ? o)) THEN RAISE EXCEPTION 'Unaccounted selected candidate' USING ERRCODE='23514'; END IF;
            SELECT coalesce(jsonb_agg(o.v ORDER BY o.ord),'[]') INTO wanted FROM jsonb_array_elements(inputs->'order') WITH ORDINALITY o(v,ord) WHERE st->'body_order' ? (o.v#>>'{}');
            IF wanted IS DISTINCT FROM st->'body_order' THEN RAISE EXCEPTION 'Snapshot logical order changed' USING ERRCODE='23514'; END IF;
            IF jsonb_array_length(plan->'blocks')>0 OR jsonb_array_length(coalesce(plan->'footnotes','[]'))>0 THEN st:={{policy}}.oa_separator(st,'terminator'); END IF;
            IF octet_length(st->>'text')>131072 THEN RAISE EXCEPTION 'Output bound exceeded' USING ERRCODE='54000'; END IF;
            details:=(st-ARRAY['text','used','body_order','headers','local_ids','grid_positions'])||jsonb_build_object('publication_eligible',false);
            RETURN jsonb_build_object('text',st->>'text','projection',details);
        END $function$
