CREATE OR REPLACE FUNCTION {{policy}}.assembly_payload_valid(body jsonb)
 RETURNS boolean
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE cell jsonb; warning jsonb; key text; value text;
        BEGIN
            IF body IS NULL OR NOT {{policy}}.operator_keys(body,
                ARRAY['id','adapter_local_id','region_key','artifact_class','block_type','text','unavailable_page_reason','unavailable_geometry_reason'],
                ARRAY['parent_local_id','content_state','content_reason','language','direction','quality_state','warnings','cell'])
                OR NOT {{policy}}.assembly_local_key(body->'adapter_local_id',false)
                OR NOT {{policy}}.assembly_local_key(body->'region_key',false)
                OR NOT {{policy}}.assembly_local_key(body->'parent_local_id',true)
                OR NOT {{policy}}.assembly_local_key(body->'language',true)
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
                IF jsonb_typeof(warning)<>'string' OR length(warning#>>'{}') NOT BETWEEN 1 AND 256 THEN RETURN false; END IF;
            END LOOP;
            cell:=body->'cell';
            IF body->>'block_type'='table_cell' THEN
                IF NOT {{policy}}.operator_keys(cell,ARRAY['table_local_id','row','column'],ARRAY['row_span','column_span'])
                    OR NOT {{policy}}.assembly_local_key(cell->'table_local_id',false) THEN RETURN false; END IF;
                FOREACH key IN ARRAY ARRAY['row','column','row_span','column_span'] LOOP
                    value:=coalesce(cell->>key,CASE WHEN key IN ('row_span','column_span') THEN '1' ELSE NULL END);
                    IF value IS NULL OR (cell ? key AND jsonb_typeof(cell->key)<>'number') OR value !~ '^[0-9]{1,3}$'
                        OR value::integer NOT BETWEEN (CASE WHEN key IN ('row','column') THEN 0 ELSE 1 END) AND (CASE WHEN key IN ('row','column') THEN 99 ELSE 100 END) THEN RETURN false; END IF;
                END LOOP;
                IF (cell->>'row')::integer+coalesce((cell->>'row_span')::integer,1)>100
                    OR (cell->>'column')::integer+coalesce((cell->>'column_span')::integer,1)>100 THEN RETURN false; END IF;
            ELSIF cell IS NOT NULL AND cell<>'null'::jsonb THEN RETURN false; END IF;
            RETURN true;
        END $function$
