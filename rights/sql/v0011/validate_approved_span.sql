CREATE OR REPLACE FUNCTION {{policy}}.validate_approved_span()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE bytes bytea;begin_byte bigint;finish_byte bigint;
        BEGIN
            SELECT canonical_bytes INTO bytes FROM {{corpus}}.approved_version WHERE id=NEW.version_id;
            IF TG_TABLE_NAME='version_node' THEN
            IF NEW.candidate_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM {{staging}}.adapter_candidate d JOIN {{corpus}}.approved_version v ON v.artifact_id=d.artifact_id
                JOIN {{staging}}.snapshot_review r ON r.id=v.review_id JOIN {{staging}}.canonical_proposal cp ON cp.id=r.proposal_id
                JOIN {{staging}}.snapshot_selection sel ON sel.snapshot_id=cp.snapshot_id AND sel.revision=cp.snapshot_revision AND sel.candidate_id=d.id
                WHERE v.id=NEW.version_id AND d.id=NEW.candidate_id) THEN RAISE EXCEPTION 'Exact selected source candidate required' USING ERRCODE='23514'; END IF;
            IF NEW.candidate_id IS NOT NULL AND EXISTS(SELECT 1 FROM {{staging}}.adapter_candidate d WHERE d.id=NEW.candidate_id AND d.payload->>'text'<>'')
                AND NOT EXISTS(SELECT 1 FROM {{staging}}.adapter_candidate d WHERE d.id=NEW.candidate_id
                    AND convert_to({{policy}}.oa_norm(d.payload->>'text'),'UTF8')=substring(bytes FROM NEW.start_byte::integer+1 FOR (NEW.end_byte-NEW.start_byte)::integer)) THEN
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
        END $function$
