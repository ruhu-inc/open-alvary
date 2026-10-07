CREATE OR REPLACE FUNCTION {{policy}}.apply_operator_command(envelope jsonb)
 RETURNS TABLE(revision bigint, replayed boolean)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE aid uuid; cid text; cmd uuid; action_name text; expected bigint; reason_value text;
            body jsonb; fingerprint text; previous {{policy}}.command_receipt; evidence uuid;
            material jsonb; classes text[];
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR NOT {{policy}}.operator_keys(envelope,
                ARRAY['command_id','collection_id','action','expected_revision','reason','payload']) THEN
                RAISE EXCEPTION 'Invalid operator command' USING ERRCODE='22023'; END IF;
            -- Hold authorization until the caller commits; administrator revocation waits.
            SELECT a.id INTO aid FROM {{policy}}.actor a JOIN pg_catalog.pg_roles r ON r.oid=a.database_role
                WHERE r.rolname=session_user::text AND a.database_role_name=r.rolname AND a.active
                FOR SHARE OF a;
            IF aid IS NULL OR aid IS DISTINCT FROM {{policy}}.current_actor() THEN
                RAISE EXCEPTION 'Individual operator session required' USING ERRCODE='42501'; END IF;
            -- Synthetic shadow commands never become a live target-authority writer.
            PERFORM {{base}}.identity_check_legacy_authority(true);
            cid := envelope->>'collection_id'; cmd := (envelope->>'command_id')::uuid;
            action_name := envelope->>'action'; expected := (envelope->>'expected_revision')::bigint;
            reason_value := envelope->>'reason'; body := envelope->'payload';
            IF cmd IS NULL OR cid IS NULL OR expected IS NULL OR expected<0
                OR reason_value IS NULL OR length(btrim(reason_value)) NOT BETWEEN 1 AND 2048
                OR action_name IS NULL OR action_name NOT IN
                    ('record_evidence','record_collection_decision','record_verification_review') THEN
                RAISE EXCEPTION 'Invalid operator command' USING ERRCODE='22023'; END IF;
            IF (action_name='record_verification_review' AND NOT {{policy}}.assigned(cid,'content'))
                OR (action_name='record_collection_decision' AND NOT {{policy}}.assigned(cid,'rights'))
                OR (action_name='record_evidence' AND NOT
                    ({{policy}}.assigned(cid,'rights') OR {{policy}}.assigned(cid,'content'))) THEN
                RAISE EXCEPTION 'Collection authority required' USING ERRCODE='42501'; END IF;
            fingerprint := encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            -- One transaction-scoped retry lock, shared by both individual sessions.
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_e7ac078666}}||cmd::text,0));
            SELECT * INTO previous FROM {{policy}}.command_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>aid THEN
                    RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN
                    RAISE EXCEPTION 'Command retry changed' USING ERRCODE='40001'; END IF;
                RETURN QUERY SELECT previous.revision,true; RETURN;
            END IF;
            IF action_name='record_evidence' THEN
                IF expected<>0 OR NOT {{policy}}.operator_keys(body,
                    ARRAY['id','private_reference','evidence_hash','observed_at']) THEN
                    RAISE EXCEPTION 'Invalid evidence command' USING ERRCODE='22023'; END IF;
                evidence := (body->>'id')::uuid;
                IF (body->>'observed_at')::timestamptz>statement_timestamp() THEN
                    RAISE EXCEPTION 'Future evidence observation' USING ERRCODE='22023'; END IF;
                INSERT INTO {{policy}}.evidence(id,collection_id,private_reference,evidence_hash,observed_at)
                    VALUES(evidence,cid,body->>'private_reference',body->>'evidence_hash',
                        (body->>'observed_at')::timestamptz);
                INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                    VALUES(aid,cid,evidence,'evidence',evidence::text,1,reason_value);
            ELSE
                evidence := (body->>'evidence_id')::uuid;
                IF NOT isfinite((body->>'valid_from')::timestamptz)
                    OR NOT isfinite((body->>'expires_at')::timestamptz) THEN
                    RAISE EXCEPTION 'Finite review interval required' USING ERRCODE='22023'; END IF;
                IF body->>'state'='approved' AND (body->>'expires_at')::timestamptz<=statement_timestamp() THEN
                    RAISE EXCEPTION 'Expired review cannot approve' USING ERRCODE='22023'; END IF;
                IF action_name='record_collection_decision' THEN
                    IF NOT {{policy}}.operator_keys(body,ARRAY['evidence_id','valid_from','expires_at','state', 'basis_type','conditions_satisfied','metadata_privacy_class','privacy_reason'],ARRAY[{{vocabulary_discover}},{{vocabulary_acquire}},{{vocabulary_retain}},{{vocabulary_external_process}},{{vocabulary_redistribute_metadata}},{{vocabulary_redistribute_text}},{{vocabulary_quote}},{{vocabulary_derive}},{{vocabulary_commercial_reuse}},{{vocabulary_publish_fixture}},'privacy_review_id','privacy_review_revision']) THEN
                        RAISE EXCEPTION 'Invalid collection decision' USING ERRCODE='22023'; END IF;
                    INSERT INTO {{policy}}.collection_decision(collection_id,actor_id,revision,evidence_id,
                        valid_from,expires_at,state,reason,basis_type,conditions_satisfied,metadata_privacy_class,
                        privacy_reason,privacy_review_id,privacy_review_revision,discover,acquire,retain,external_process,redistribute_metadata,redistribute_text,quote,derive,commercial_reuse,publish_fixture)
                    VALUES(cid,aid,expected+1,evidence,(body->>'valid_from')::timestamptz,
                        (body->>'expires_at')::timestamptz,body->>'state',reason_value,body->>'basis_type',
                        (body->>'conditions_satisfied')::boolean,body->>'metadata_privacy_class',
                        body->>'privacy_reason',(body->>'privacy_review_id')::uuid,
                        (body->>'privacy_review_revision')::bigint,coalesce(body->>{{vocabulary_discover}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_acquire}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_retain}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_external_process}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_redistribute_metadata}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_redistribute_text}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_quote}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_derive}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_commercial_reuse}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_publish_fixture}},'unknown')::{{policy}}.permission);
                ELSE
                    IF NOT {{policy}}.operator_keys(body,ARRAY['evidence_id','valid_from','expires_at','state','sample_numerator',
                        'sample_denominator','escalation_rule','materials'])
                        OR jsonb_typeof(body->'materials') IS DISTINCT FROM 'array' THEN
                        RAISE EXCEPTION 'Invalid verification review' USING ERRCODE='22023'; END IF;
                    SELECT array_agg(m->>'material_class') INTO classes FROM jsonb_array_elements(body->'materials') m;
                    IF cardinality(classes)<>6 OR NOT classes @> ARRAY[{{vocabulary_operative_tables}},{{vocabulary_amounts}},{{vocabulary_dates}},{{vocabulary_negations}},{{vocabulary_cross_references}},{{vocabulary_identity_citation}}]::text[] THEN
                        RAISE EXCEPTION 'Complete material coverage required' USING ERRCODE='22023'; END IF;
                    INSERT INTO {{policy}}.verification_policy(collection_id,actor_id,revision,evidence_id,
                        valid_from,expires_at,state,reason,sample_numerator,sample_denominator,
                        escalation_rule,material_classes)
                    VALUES(cid,aid,expected+1,evidence,(body->>'valid_from')::timestamptz,
                        (body->>'expires_at')::timestamptz,body->>'state',reason_value,
                        (body->>'sample_numerator')::integer,(body->>'sample_denominator')::integer,
                        body->>'escalation_rule',classes);
                    FOR material IN SELECT value FROM jsonb_array_elements(body->'materials') LOOP
                        IF NOT {{policy}}.operator_keys(material,ARRAY['material_class','method','sample_numerator',
                            'sample_denominator','competence_evidence_id','escalation_rule','exclusion_rule']) THEN
                            RAISE EXCEPTION 'Invalid material review' USING ERRCODE='22023'; END IF;
                        INSERT INTO {{policy}}.verification_material(collection_id,revision,material_class,method,
                            sample_numerator,sample_denominator,competence_evidence_id,escalation_rule,exclusion_rule)
                        VALUES(cid,expected+1,material->>'material_class',material->>'method',
                            (material->>'sample_numerator')::integer,(material->>'sample_denominator')::integer,
                            (material->>'competence_evidence_id')::uuid,material->>'escalation_rule',
                            material->>'exclusion_rule');
                    END LOOP;
                END IF;
            END IF;
            INSERT INTO {{policy}}.command_receipt(command_id,actor_id,collection_id,action,payload_hash,evidence_id,revision)
                VALUES(cmd,aid,cid,action_name,fingerprint,evidence,expected+1);
            RETURN QUERY SELECT expected+1,false;
        END $function$
