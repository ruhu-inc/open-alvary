CREATE OR REPLACE FUNCTION {{policy}}.apply_acquisition_command(envelope jsonb)
 RETURNS TABLE(revision bigint, replayed boolean)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE aid uuid; cid text; cmd uuid; action_name text; expected bigint; reason_value text;
            body jsonb; fingerprint text; previous {{policy}}.command_receipt; evidence uuid;
            ctl {{policy}}.controller_record; privacy {{policy}}.privacy_review; related uuid;
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR NOT {{policy}}.operator_keys(envelope,
                ARRAY['command_id','collection_id','action','expected_revision','reason','payload']) THEN
                RAISE EXCEPTION 'Invalid operator command' USING ERRCODE='22023'; END IF;
            SELECT a.id INTO aid FROM {{policy}}.actor a JOIN pg_roles r ON r.oid=a.database_role
                WHERE r.rolname=session_user::text AND a.database_role_name=r.rolname AND a.active FOR SHARE OF a;
            IF aid IS NULL OR aid IS DISTINCT FROM {{policy}}.current_actor() THEN
                RAISE EXCEPTION 'Individual operator session required' USING ERRCODE='42501'; END IF;
            PERFORM {{base}}.identity_check_legacy_authority(true);
            cid:=envelope->>'collection_id'; cmd:=(envelope->>'command_id')::uuid;
            action_name:=envelope->>'action'; expected:=(envelope->>'expected_revision')::bigint;
            reason_value:=envelope->>'reason'; body:=envelope->'payload';
            IF cmd IS NULL OR cid IS NULL OR expected IS NULL OR expected<0 OR reason_value IS NULL
                OR length(btrim(reason_value)) NOT BETWEEN 1 AND 2048 OR action_name IS NULL OR
                action_name NOT IN ('record_controller','record_privacy_review','record_acquisition_assessment') THEN
                RAISE EXCEPTION 'Invalid acquisition command' USING ERRCODE='22023'; END IF;
            IF NOT {{policy}}.assigned(cid,'rights') THEN
                RAISE EXCEPTION 'Rights assessment authority required' USING ERRCODE='42501'; END IF;
            fingerprint:=encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_e7ac078666}}||cmd::text,0));
            SELECT * INTO previous FROM {{policy}}.command_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>aid THEN RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Command retry changed' USING ERRCODE='40001'; END IF;
                RETURN QUERY SELECT previous.revision,true; RETURN;
            END IF;
            IF NOT {{policy}}.operator_keys(body,ARRAY['id','evidence_id','valid_from','expires_at','state'],
                CASE action_name WHEN 'record_controller' THEN ARRAY['legal_name','postal_address',
                    'jurisdiction_id','privacy_contact','accountable_role']
                WHEN 'record_privacy_review' THEN ARRAY['controller_id','controller_revision','purpose',
                    'lawful_basis','assessment_reference','clearance','cleared_hash']
                ELSE ARRAY[{{vocabulary_discover}},{{vocabulary_acquire}},{{vocabulary_retain}},{{vocabulary_derive}},'privacy_class','classification_reason',
                    'privacy_review_id','privacy_review_revision','purpose','retention_deadline','artifact_hash'] END)
                OR NOT isfinite((body->>'valid_from')::timestamptz) OR NOT isfinite((body->>'expires_at')::timestamptz)
                OR (body->>'state'='approved' AND (body->>'expires_at')::timestamptz<=statement_timestamp()) THEN
                RAISE EXCEPTION 'Invalid review interval or fields' USING ERRCODE='22023'; END IF;
            evidence:=(body->>'evidence_id')::uuid;
            IF action_name='record_controller' THEN
                INSERT INTO {{policy}}.controller_record(id,revision,collection_id,actor_id,evidence_id,
                    valid_from,expires_at,state,reason,legal_name,postal_address,jurisdiction_id,privacy_contact,accountable_role)
                VALUES((body->>'id')::uuid,expected+1,cid,aid,evidence,(body->>'valid_from')::timestamptz,
                    (body->>'expires_at')::timestamptz,body->>'state',reason_value,body->>'legal_name',
                    body->>'postal_address',body->>'jurisdiction_id',body->>'privacy_contact',body->>'accountable_role');
            ELSIF action_name='record_privacy_review' THEN
                related:=(body->>'controller_id')::uuid;
                SELECT c.* INTO ctl FROM {{policy}}.controller_record c WHERE c.id=related
                    AND c.revision=(body->>'controller_revision')::bigint AND c.collection_id=cid;
                PERFORM 1 FROM {{policy}}.actor WHERE id=ctl.actor_id FOR SHARE;
                PERFORM 1 FROM {{policy}}.revision_head WHERE kind='controller' AND scope_id=related::text FOR SHARE;
                IF body->>'state'='approved' AND body->>'clearance' IN ('cleared','redacted') AND
                    (ctl.id IS NULL OR ctl.state<>'approved' OR ctl.valid_from>statement_timestamp()
                    OR ctl.expires_at<=statement_timestamp() OR NOT EXISTS(SELECT 1 FROM {{policy}}.actor WHERE id=ctl.actor_id AND active)
                    OR NOT EXISTS(SELECT 1 FROM {{policy}}.revision_head h WHERE h.kind='controller' AND h.scope_id=related::text AND h.revision=ctl.revision)) THEN
                    RAISE EXCEPTION 'Current controller revision required' USING ERRCODE='42501'; END IF;
                INSERT INTO {{policy}}.privacy_review(id,revision,collection_id,actor_id,evidence_id,valid_from,expires_at,
                    state,reason,controller_id,controller_revision,purpose,lawful_basis,assessment_reference,clearance,cleared_hash)
                VALUES((body->>'id')::uuid,expected+1,cid,aid,evidence,(body->>'valid_from')::timestamptz,
                    (body->>'expires_at')::timestamptz,body->>'state',reason_value,related,(body->>'controller_revision')::bigint,
                    body->>'purpose',body->>'lawful_basis',body->>'assessment_reference',body->>'clearance',body->>'cleared_hash');
            ELSE
                SELECT v.* INTO privacy FROM {{policy}}.privacy_review v WHERE v.id=(body->>'privacy_review_id')::uuid
                    AND v.revision=(body->>'privacy_review_revision')::bigint AND v.collection_id=cid;
                SELECT c.* INTO ctl FROM {{policy}}.controller_record c WHERE c.id=privacy.controller_id
                    AND c.revision=privacy.controller_revision AND c.collection_id=cid;
                PERFORM 1 FROM {{policy}}.actor WHERE id IN (privacy.actor_id,ctl.actor_id) ORDER BY id FOR SHARE;
                PERFORM 1 FROM {{policy}}.revision_head WHERE (kind='privacy' AND scope_id=privacy.id::text)
                    OR (kind='controller' AND scope_id=ctl.id::text) ORDER BY kind,scope_id FOR SHARE;
                IF body->>'state'='approved' AND (body->>{{vocabulary_acquire}}='allow' OR body->>{{vocabulary_retain}}='allow'
                    OR body->>{{vocabulary_derive}}='allow' OR body->>{{vocabulary_discover}}='allow') AND
                    (((body->>'privacy_class'<>'no_personal_data' OR EXISTS(SELECT 1 FROM {{base}}.identity_collections
                        WHERE id=cid AND document_class IN ('judgment','other'))) AND privacy.id IS NULL)
                    OR ((body->>'privacy_review_id') IS NOT NULL AND
                        (privacy.purpose IS DISTINCT FROM body->>'purpose'
                        OR NOT {{policy}}.privacy_current(privacy.id,privacy.revision,cid,body->>'artifact_hash')))) THEN
                    RAISE EXCEPTION 'Current purpose and privacy authority required' USING ERRCODE='42501'; END IF;
                IF NOT isfinite((body->>'retention_deadline')::timestamptz) THEN
                    RAISE EXCEPTION 'Finite retention deadline required' USING ERRCODE='22023'; END IF;
                INSERT INTO {{policy}}.acquisition_assessment(id,revision,collection_id,actor_id,evidence_id,valid_from,expires_at,
                    state,reason,discover,acquire,retain,derive,privacy_class,classification_reason,privacy_review_id,
                    privacy_review_revision,purpose,retention_deadline,artifact_hash)
                VALUES((body->>'id')::uuid,expected+1,cid,aid,evidence,(body->>'valid_from')::timestamptz,
                    (body->>'expires_at')::timestamptz,body->>'state',reason_value,
                    coalesce(body->>{{vocabulary_discover}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_acquire}},'unknown')::{{policy}}.permission,
                    coalesce(body->>{{vocabulary_retain}},'unknown')::{{policy}}.permission,coalesce(body->>{{vocabulary_derive}},'unknown')::{{policy}}.permission,
                    body->>'privacy_class',body->>'classification_reason',(body->>'privacy_review_id')::uuid,
                    (body->>'privacy_review_revision')::bigint,body->>'purpose',(body->>'retention_deadline')::timestamptz,
                    body->>'artifact_hash');
            END IF;
            INSERT INTO {{policy}}.command_receipt(command_id,actor_id,collection_id,action,payload_hash,evidence_id,revision)
                VALUES(cmd,aid,cid,action_name,fingerprint,evidence,expected+1);
            RETURN QUERY SELECT expected+1,false;
        END $function$
