CREATE OR REPLACE FUNCTION {{policy}}.apply_assembly_command_m32(envelope jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE operator_id uuid; cid text; cmd uuid; action_name text; fingerprint text; previous {{policy}}.assembly_receipt;
            artifact {{staging}}.staged_artifact; run {{staging}}.synthetic_run; head {{staging}}.snapshot_head;
            item jsonb; selection jsonb; resolution jsonb; body jsonb; normalized jsonb:='[]'; rejected jsonb;
            profile_hash text; output_hash text; body_hash text; selected_hashes jsonb; bytes bigint:=0; expected bigint; object_id uuid;
            region_count integer; candidate_count integer; count_value integer; rev bigint; aid uuid;
            scanned integer:=0; held integer:=0; required integer:=0; erased integer:=0; last_id uuid;
            target text; cause_value text; row_item record; assessment {{policy}}.acquisition_assessment; cursor_id uuid;
            batch_limit integer; erase_due boolean; result jsonb;
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR jsonb_typeof(envelope)<>'object' THEN
                RAISE EXCEPTION 'Bounded assembly command required' USING ERRCODE='22023'; END IF;
            cmd:=(envelope->>'command_id')::uuid; cid:=envelope->>'collection_id'; action_name:=envelope->>'action';
            IF cmd IS NULL OR cid IS NULL OR action_name IS NULL OR jsonb_typeof(envelope->'reason') IS DISTINCT FROM 'string'
                OR length(btrim(envelope->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Invalid assembly command' USING ERRCODE='22023'; END IF;
            operator_id:={{policy}}.current_actor(); PERFORM 1 FROM {{policy}}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {{policy}}.is_native_operator() OR NOT {{policy}}.assigned(cid,{{vocabulary_acquire}})
                OR (action_name='record_snapshot' AND NOT {{policy}}.assigned(cid,'content')) THEN
                RAISE EXCEPTION 'Native scoped assembly authority required' USING ERRCODE='42501'; END IF;
            IF action_name='record_synthetic_run' THEN
                IF NOT {{policy}}.operator_keys(envelope,ARRAY['command_id','collection_id','reason','action','run_id','artifact_id',
                    'assessment_id','assessment_revision','artifact_hash','purpose','profile_key','candidates'])
                    OR NOT {{policy}}.assembly_local_key(envelope->'profile_key',false) OR jsonb_typeof(envelope->'candidates') IS DISTINCT FROM 'array'
                    OR jsonb_array_length(envelope->'candidates') NOT BETWEEN 1 AND 50 THEN RAISE EXCEPTION 'Invalid synthetic run' USING ERRCODE='22023'; END IF;
                artifact:={{policy}}.lock_assembly_input((envelope->>'artifact_id')::uuid,cid,envelope->>'purpose',true);
                IF (artifact.assessment_id,artifact.assessment_revision,artifact.artifact_hash) IS DISTINCT FROM
                    ((envelope->>'assessment_id')::uuid,(envelope->>'assessment_revision')::bigint,envelope->>'artifact_hash') THEN
                    RAISE EXCEPTION 'Exact source lineage required' USING ERRCODE='42501'; END IF;
            ELSIF action_name='record_snapshot' THEN
                IF NOT {{policy}}.operator_keys(envelope,ARRAY['command_id','collection_id','reason','action','snapshot_id','run_id','purpose','expected_revision','payload'],ARRAY['parent_hash']) THEN
                    RAISE EXCEPTION 'Invalid snapshot command' USING ERRCODE='22023'; END IF;
                SELECT r.* INTO run FROM {{staging}}.synthetic_run r WHERE r.id=(envelope->>'run_id')::uuid;
                IF run.id IS NULL THEN RAISE EXCEPTION 'Scoped run required' USING ERRCODE='42501'; END IF;
                artifact:={{policy}}.lock_assembly_input(run.artifact_id,cid,envelope->>'purpose',true);
                IF run.purpose IS DISTINCT FROM envelope->>'purpose' OR EXISTS(SELECT 1 FROM {{staging}}.adapter_candidate WHERE run_id=run.id AND state<>'active') THEN
                    RAISE EXCEPTION 'Active unchanged candidate lineage required' USING ERRCODE='42501'; END IF;
            ELSIF action_name='reconcile_assembly' THEN
                IF NOT {{policy}}.operator_keys(envelope,ARRAY['command_id','collection_id','reason','action'],ARRAY['after','limit','erase_due'])
                    OR (envelope ? 'limit' AND (jsonb_typeof(envelope->'limit') IS DISTINCT FROM 'number' OR envelope->>'limit' !~ '^[0-9]{1,3}$'))
                    OR (envelope ? 'erase_due' AND jsonb_typeof(envelope->'erase_due') IS DISTINCT FROM 'boolean') THEN RAISE EXCEPTION 'Invalid assembly reconciliation' USING ERRCODE='22023'; END IF;
                batch_limit:=coalesce((envelope->>'limit')::integer,50); cursor_id:=(envelope->>'after')::uuid; erase_due:=coalesce((envelope->>'erase_due')::boolean,false);
                IF batch_limit NOT BETWEEN 1 AND 100 OR NOT EXISTS(SELECT 1 FROM {{policy}}.synthetic_staging_limit WHERE collection_id=cid) THEN
                    RAISE EXCEPTION 'Declared bounded scope required' USING ERRCODE='22023'; END IF;
            ELSE RAISE EXCEPTION 'Unsupported assembly command' USING ERRCODE='22023'; END IF;
            fingerprint:=encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_9f84896138}}||cmd::text,0));
            SELECT * INTO previous FROM {{policy}}.assembly_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>operator_id THEN RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Assembly retry changed' USING ERRCODE='40001'; END IF;
                RETURN previous.result || jsonb_build_object('replayed',true);
            END IF;
            IF action_name='record_synthetic_run' THEN
                object_id:=(envelope->>'run_id')::uuid;
                IF object_id IS NULL THEN RAISE EXCEPTION 'Run identity required' USING ERRCODE='22023'; END IF;
                IF (SELECT count(*) FROM {{staging}}.synthetic_run WHERE artifact_id=artifact.id)>=4 THEN RAISE EXCEPTION 'Synthetic run bound exceeded' USING ERRCODE='54000'; END IF;
                FOR item IN SELECT * FROM jsonb_array_elements(envelope->'candidates') LOOP
                    IF NOT {{policy}}.assembly_payload_valid(item) OR item->>'id' IS NULL THEN RAISE EXCEPTION 'Invalid synthetic candidate' USING ERRCODE='22023'; END IF;
                    item:=jsonb_build_object('parent_local_id',NULL,'content_state','present','content_reason',NULL,'language',NULL,'direction','unknown','quality_state','unassessed','warnings','[]'::jsonb,'cell',NULL)||item;
                    IF item->>'block_type'='table_cell' THEN item:=jsonb_set(item,'{cell}',jsonb_build_object('row_span',1,'column_span',1)||item->'cell'); END IF;
                    normalized:=normalized||jsonb_build_array(item); bytes:=bytes+octet_length(item::text);
                END LOOP;
                PERFORM {{policy}}.reserve_assembly_capacity(artifact.id,cid,bytes,jsonb_array_length(normalized));
                profile_hash:=encode(sha256(convert_to(jsonb_build_object('producer','original_synthetic','version','synthetic-candidates-1','profile_key',envelope->>'profile_key')::text,'UTF8')),'hex');
                output_hash:=encode(sha256(convert_to(normalized::text,'UTF8')),'hex');
                INSERT INTO {{staging}}.synthetic_run(id,artifact_id,collection_id,assessment_id,assessment_revision,artifact_hash,purpose,
                    retention_deadline,profile_key,profile_hash,output_hash,actor_id)
                    VALUES(object_id,artifact.id,cid,artifact.assessment_id,artifact.assessment_revision,artifact.artifact_hash,artifact.purpose,
                        artifact.retention_deadline,envelope->>'profile_key',profile_hash,output_hash,operator_id);
                FOR item IN SELECT * FROM jsonb_array_elements(normalized) LOOP
                    INSERT INTO {{staging}}.adapter_candidate(id,run_id,artifact_id,adapter_local_id,region_key,parent_local_id,artifact_class,block_type,payload,payload_hash,text_hash,payload_bytes)
                        VALUES((item->>'id')::uuid,object_id,artifact.id,item->>'adapter_local_id',item->>'region_key',item->>'parent_local_id',item->>'artifact_class',item->>'block_type',item,
                            encode(sha256(convert_to(item::text,'UTF8')),'hex'),encode(sha256(convert_to(item->>'text','UTF8')),'hex'),octet_length(item::text));
                END LOOP;
                IF EXISTS(SELECT 1 FROM {{staging}}.adapter_candidate c WHERE c.run_id=object_id AND c.parent_local_id IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM {{staging}}.adapter_candidate parent WHERE parent.run_id=c.run_id AND parent.adapter_local_id=c.parent_local_id)) THEN RAISE EXCEPTION 'Missing local parent' USING ERRCODE='23514'; END IF;
                IF EXISTS(WITH RECURSIVE walk AS (
                    SELECT c.adapter_local_id,c.parent_local_id,ARRAY[c.adapter_local_id] path,false cycle FROM {{staging}}.adapter_candidate c WHERE c.run_id=object_id
                    UNION ALL SELECT parent.adapter_local_id,parent.parent_local_id,w.path||parent.adapter_local_id,parent.adapter_local_id=ANY(w.path)
                        FROM walk w JOIN {{staging}}.adapter_candidate parent ON parent.run_id=object_id AND parent.adapter_local_id=w.parent_local_id WHERE NOT w.cycle)
                    SELECT 1 FROM walk WHERE cycle) THEN RAISE EXCEPTION 'Local hierarchy cycle' USING ERRCODE='23514'; END IF;
                rev:=1;
            ELSIF action_name='record_snapshot' THEN
                object_id:=(envelope->>'snapshot_id')::uuid; expected:=(envelope->>'expected_revision')::bigint; body:=envelope->'payload';
                IF object_id IS NULL OR expected IS NULL OR expected NOT BETWEEN 0 AND 31 OR jsonb_typeof(envelope->'expected_revision')<>'number'
                    OR envelope->>'expected_revision' !~ '^[0-9]{1,2}$' OR NOT {{policy}}.operator_keys(body,ARRAY['selections','order','reason'],ARRAY['resolutions'])
                    OR jsonb_typeof(body->'selections') IS DISTINCT FROM 'array' OR jsonb_array_length(body->'selections') NOT BETWEEN 1 AND 50
                    OR jsonb_typeof(body->'order') IS DISTINCT FROM 'array' OR jsonb_array_length(body->'order') NOT BETWEEN 1 AND 50
                    OR jsonb_typeof(body->'reason') IS DISTINCT FROM 'string' OR length(btrim(body->>'reason')) NOT BETWEEN 1 AND 2048
                    OR jsonb_typeof(coalesce(body->'resolutions','[]'::jsonb)) IS DISTINCT FROM 'array' OR jsonb_array_length(coalesce(body->'resolutions','[]'::jsonb))>50 THEN
                    RAISE EXCEPTION 'Bounded snapshot proposal required' USING ERRCODE='22023'; END IF;
                SELECT h.* INTO head FROM {{staging}}.snapshot_head h WHERE h.id=object_id FOR UPDATE;
                IF coalesce(head.revision,0)<>expected OR (head.id IS NOT NULL AND head.run_id<>run.id)
                    OR (expected=0 AND envelope->>'parent_hash' IS NOT NULL)
                    OR (expected>0 AND NOT EXISTS(SELECT 1 FROM {{staging}}.staging_snapshot t WHERE t.id=object_id AND t.revision=expected
                        AND t.snapshot_hash=envelope->>'parent_hash' AND t.state='active')) THEN RAISE EXCEPTION 'Snapshot parent conflict' USING ERRCODE='40001'; END IF;
                body:=jsonb_build_object('resolutions','[]'::jsonb)||body;
                rev:=expected+1; body_hash:=encode(sha256(convert_to(body::text,'UTF8')),'hex');
                SELECT jsonb_agg(jsonb_build_object('candidate_id',c.id,'payload_hash',c.payload_hash) ORDER BY o.ord)
                    INTO selected_hashes FROM jsonb_array_elements_text(body->'order') WITH ORDINALITY o(id,ord)
                    JOIN {{staging}}.adapter_candidate c ON c.id=o.id::uuid AND c.run_id=run.id;
                output_hash:=encode(sha256(convert_to(jsonb_build_object('profile','assembly-proposal-1','snapshot_id',object_id,
                    'revision',rev,'parent_hash',envelope->>'parent_hash','run_id',run.id,'run_output_hash',run.output_hash,
                    'producer',run.producer,'producer_profile_hash',run.profile_hash,'artifact_id',run.artifact_id,
                    'artifact_hash',run.artifact_hash,'collection_id',run.collection_id,'assessment_id',run.assessment_id,
                    'assessment_revision',run.assessment_revision,'purpose',run.purpose,'retention_epoch',extract(epoch FROM run.retention_deadline),
                    'editor',operator_id,'selected_candidate_hashes',selected_hashes,'payload',body)::text,'UTF8')),'hex');
                PERFORM {{policy}}.reserve_assembly_capacity(artifact.id,cid,octet_length(body::text),1);
                INSERT INTO {{staging}}.staging_snapshot(id,revision,run_id,artifact_id,parent_hash,payload,payload_hash,snapshot_hash,payload_bytes,actor_id)
                    VALUES(object_id,rev,run.id,artifact.id,envelope->>'parent_hash',body,body_hash,output_hash,octet_length(body::text),operator_id);
                SELECT count(DISTINCT region_key) INTO region_count FROM {{staging}}.adapter_candidate WHERE run_id=run.id;
                IF jsonb_array_length(body->'selections')<>region_count OR jsonb_array_length(body->'order')<>region_count THEN RAISE EXCEPTION 'Complete explicit region selection required' USING ERRCODE='23514'; END IF;
                FOR selection IN SELECT * FROM jsonb_array_elements(body->'selections') LOOP
                    IF NOT {{policy}}.operator_keys(selection,ARRAY['region_key','candidate_id']) THEN RAISE EXCEPTION 'Invalid selection' USING ERRCODE='22023'; END IF;
                    INSERT INTO {{staging}}.snapshot_selection(snapshot_id,revision,run_id,region_key,candidate_id,position)
                        SELECT object_id,rev,run.id,selection->>'region_key',(selection->>'candidate_id')::uuid,ord::integer
                            FROM jsonb_array_elements_text(body->'order') WITH ORDINALITY o(id,ord) WHERE id=selection->>'candidate_id';
                END LOOP;
                IF (SELECT count(*) FROM {{staging}}.snapshot_selection WHERE snapshot_id=object_id AND revision=rev)<>region_count THEN RAISE EXCEPTION 'Duplicate or missing order selection' USING ERRCODE='23514'; END IF;
                FOR resolution IN SELECT * FROM jsonb_array_elements(body->'resolutions') LOOP
                    IF NOT {{policy}}.operator_keys(resolution,ARRAY['region_key','candidate_id','rejected_candidate_ids','reason'])
                        OR jsonb_typeof(resolution->'rejected_candidate_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(resolution->'rejected_candidate_ids') NOT BETWEEN 1 AND 49
                        OR jsonb_typeof(resolution->'reason') IS DISTINCT FROM 'string' OR length(btrim(resolution->>'reason')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Explicit conflict resolution required' USING ERRCODE='22023'; END IF;
                    SELECT count(*) INTO candidate_count FROM {{staging}}.adapter_candidate WHERE run_id=run.id AND region_key=resolution->>'region_key';
                    IF candidate_count<=1 OR jsonb_array_length(resolution->'rejected_candidate_ids')<>candidate_count-1 THEN RAISE EXCEPTION 'All alternatives require resolution' USING ERRCODE='23514'; END IF;
                    FOR rejected IN SELECT * FROM jsonb_array_elements(resolution->'rejected_candidate_ids') LOOP
                        INSERT INTO {{staging}}.snapshot_resolution(snapshot_id,revision,run_id,region_key,selected_candidate_id,rejected_candidate_id)
                            VALUES(object_id,rev,run.id,resolution->>'region_key',(resolution->>'candidate_id')::uuid,(rejected#>>'{}')::uuid);
                    END LOOP;
                END LOOP;
                SELECT count(*) INTO candidate_count FROM {{staging}}.adapter_candidate WHERE run_id=run.id;
                IF (SELECT count(*) FROM {{staging}}.snapshot_resolution WHERE snapshot_id=object_id AND revision=rev)<>candidate_count-region_count THEN RAISE EXCEPTION 'Unresolved overlapping candidates' USING ERRCODE='23514'; END IF;
                INSERT INTO {{staging}}.snapshot_head(id,revision,run_id) VALUES(object_id,rev,run.id)
                    ON CONFLICT(id) DO UPDATE SET revision=excluded.revision;
            ELSE
                FOR row_item IN SELECT id,collection_id,assessment_id,assessment_revision,purpose,artifact_hash,state,retention_deadline
                    FROM {{staging}}.staged_artifact WHERE collection_id=cid AND (cursor_id IS NULL OR id>cursor_id) ORDER BY id LIMIT batch_limit FOR UPDATE
                LOOP
                    scanned:=scanned+1; last_id:=row_item.id;
                    PERFORM {{policy}}.lock_acquisition_authority(row_item.assessment_id,row_item.assessment_revision,cid);
                    SELECT a.* INTO assessment FROM {{policy}}.acquisition_assessment a WHERE a.id=row_item.assessment_id AND a.revision=row_item.assessment_revision AND a.collection_id=cid;
                    target:=NULL; cause_value:=NULL;
                    IF row_item.state='erased' THEN target:='erased'; cause_value:='raw_parent_state';
                    ELSIF row_item.state='erasure_required' OR row_item.retention_deadline<=statement_timestamp() THEN target:='erasure_required'; cause_value:='retention_due';
                    ELSIF EXISTS(SELECT 1 FROM {{policy}}.privacy_review v WHERE v.id=assessment.privacy_review_id AND v.collection_id=cid
                        AND v.revision>=assessment.privacy_review_revision AND v.clearance='rejected' AND v.valid_from<=statement_timestamp()) THEN target:='erasure_required'; cause_value:='privacy_rejected';
                    ELSIF row_item.state='held_pending_reassessment' OR NOT {{policy}}.assembly_input_current(row_item.id,row_item.purpose) THEN target:='held'; cause_value:='derivation_not_current'; END IF;
                    IF target IS NOT NULL THEN
                        count_value:={{policy}}.transition_assembly(row_item.id,target,cause_value);
                        IF target='held' THEN held:=held+count_value; ELSIF target='erasure_required' THEN required:=required+count_value; ELSE erased:=erased+count_value; END IF;
                    END IF;
                    IF erase_due AND (target='erasure_required' OR EXISTS(SELECT 1 FROM {{staging}}.adapter_candidate WHERE artifact_id=row_item.id AND state='erasure_required')
                        OR EXISTS(SELECT 1 FROM {{staging}}.staging_snapshot WHERE artifact_id=row_item.id AND state='erasure_required')
                        OR EXISTS(SELECT 1 FROM {{staging}}.canonical_proposal WHERE artifact_id=row_item.id AND state='erasure_required')) THEN
                        erased:=erased+{{policy}}.transition_assembly(row_item.id,'erased','due_erasure');
                    END IF;
                    held:=held+{{policy}}.hold_stale_canonical(row_item.id);
                END LOOP;
            END IF;
            IF action_name<>'reconcile_assembly' THEN
                SELECT a.evidence_id INTO aid FROM {{policy}}.acquisition_assessment a WHERE a.id=artifact.assessment_id AND a.revision=artifact.assessment_revision;
                INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                    VALUES(operator_id,cid,aid,CASE action_name WHEN 'record_synthetic_run' THEN 'synthetic_run' ELSE 'staging_snapshot' END,object_id::text,rev,envelope->>'reason');
            END IF;
            result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'object_id',object_id,'revision',rev,'output_hash',output_hash,
                'scanned',scanned,'held',held,'erasure_required',required,'erased',erased,'next_after',CASE WHEN scanned=batch_limit THEN last_id ELSE NULL END);
            INSERT INTO {{policy}}.assembly_receipt(command_id,actor_id,collection_id,action,payload_hash,result) VALUES(cmd,operator_id,cid,action_name,fingerprint,result);
            RETURN result;
        END $function$
