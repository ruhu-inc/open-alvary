CREATE OR REPLACE FUNCTION {{policy}}.validate_node_alignment()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM {{corpus}}.version_node n JOIN {{corpus}}.approved_version v ON v.id=n.version_id
                JOIN {{staging}}.adapter_candidate d ON d.id=n.candidate_id JOIN {{staging}}.staged_artifact a ON a.id=v.artifact_id
                WHERE n.id=NEW.node_id AND n.version_id=NEW.version_id AND d.id=NEW.candidate_id
                    AND a.artifact_hash=NEW.input_artifact_hash AND v.profile_hash=NEW.profile_hash
                    AND octet_length(d.payload->>'text')=NEW.source_end AND n.start_byte=NEW.target_start AND n.end_byte=NEW.target_end
                    AND encode(sha256(convert_to(d.payload->>'text','UTF8')),'hex')=NEW.source_candidate_hash) THEN
                RAISE EXCEPTION 'Exact whole-candidate alignment required' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $function$
