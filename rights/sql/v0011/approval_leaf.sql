CREATE OR REPLACE FUNCTION {{policy}}.approval_leaf(st jsonb, inputs jsonb, cid text, parent_key text, kind text)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE source jsonb; value text; begin_byte integer; finish_byte integer; node jsonb;
        BEGIN
            source:=inputs->'source'->cid;value:={{policy}}.oa_norm(source->>'text');
            begin_byte:=(st->>'offset')::integer;finish_byte:=begin_byte+octet_length(value);
            node:=jsonb_build_object('key',cid,'parent',parent_key,'kind',kind,'candidate_id',cid,
                'start',CASE WHEN value<>'' THEN begin_byte END,'end',CASE WHEN value<>'' THEN finish_byte END,
                'span_hash',CASE WHEN value<>'' THEN encode(sha256(convert_to(value,'UTF8')),'hex') END,
                'unavailable_reason',CASE WHEN value='' THEN 'empty_source_content' END);
            st:=jsonb_set(st,'{nodes}',(st->'nodes')||jsonb_build_array(node));
            st:=jsonb_set(st,'{order}',(st->'order')||to_jsonb(cid));
            RETURN jsonb_set(st,'{offset}',to_jsonb(finish_byte));
        END $function$
