CREATE OR REPLACE FUNCTION {{policy}}.oa_leaf(st jsonb, inputs jsonb, cid uuid, types text[], body boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE c jsonb; original text; lf text; normalized text; ops jsonb:='[]'; begin_byte integer; end_byte integer;
        BEGIN
            c:=inputs->'source'->cid::text;
            IF c IS NULL OR (st->'used') ? cid::text OR NOT coalesce(c->>'block_type'=ANY(types),false) THEN
                RAISE EXCEPTION 'Invalid or repeated source candidate' USING ERRCODE='23514'; END IF;
            st:=jsonb_set(st,'{used}',(st->'used')||to_jsonb(cid::text));
            IF body THEN st:=jsonb_set(st,'{body_order}',(st->'body_order')||to_jsonb(cid::text)); END IF;
            original:=c->>'text'; lf:=replace(replace(original,E'\r\n',E'\n'),E'\r',E'\n'); normalized:={{policy}}.oa_norm(original);
            IF lf<>original THEN ops:=ops||'"lf"'::jsonb; END IF;
            IF normalized<>lf THEN ops:=ops||'"nfc"'::jsonb; END IF;
            begin_byte:=octet_length(st->>'text'); end_byte:=begin_byte+octet_length(normalized);
            st:=jsonb_set(st,'{normalizations}',(st->'normalizations')||jsonb_build_array(jsonb_build_object('candidate_id',cid,
                'source_hash',encode(sha256(convert_to(original,'UTF8')),'hex'),'source_bytes',octet_length(original),
                'normalized_hash',encode(sha256(convert_to(normalized,'UTF8')),'hex'),'normalized_bytes',octet_length(normalized),'operations',ops)));
            IF end_byte>begin_byte THEN st:=jsonb_set(st,'{spans}',(st->'spans')||jsonb_build_array(jsonb_build_object('candidate_id',cid,
                'start',begin_byte,'end',end_byte,'span_hash',encode(sha256(convert_to(normalized,'UTF8')),'hex')))); END IF;
            IF end_byte>131072 THEN RAISE EXCEPTION 'Canonical output bound exceeded' USING ERRCODE='54000'; END IF;
            RETURN jsonb_set(st,'{text}',to_jsonb((st->>'text')||normalized));
        END $function$
