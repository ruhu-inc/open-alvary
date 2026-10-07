CREATE OR REPLACE FUNCTION {{policy}}.reconcile_staging(envelope jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE operator_id uuid; cid text; cmd uuid; cursor_id uuid; batch_limit integer; erase_due boolean;
            fingerprint text; previous {{policy}}.lifecycle_receipt; item record; assessment {{policy}}.acquisition_assessment;
            privacy {{policy}}.privacy_review; ah bigint; ph bigint; ch bigint; target text; cause_value text;
            scanned integer:=0; held integer:=0; required integer:=0; erased integer:=0; last_id uuid; result jsonb;
        BEGIN
            IF envelope IS NULL OR octet_length(envelope::text)>65536 OR NOT {{policy}}.operator_keys(envelope,
                ARRAY['command_id','collection_id','reason'],ARRAY['after','limit','erase_due'])
                OR jsonb_typeof(envelope->'reason') IS DISTINCT FROM 'string'
                OR length(btrim(envelope->>'reason')) NOT BETWEEN 1 AND 2048
                OR (envelope ? 'limit' AND jsonb_typeof(envelope->'limit') IS DISTINCT FROM 'number')
                OR (envelope ? 'erase_due' AND jsonb_typeof(envelope->'erase_due') IS DISTINCT FROM 'boolean') THEN
                RAISE EXCEPTION 'Invalid lifecycle request' USING ERRCODE='22023'; END IF;
            cmd:=(envelope->>'command_id')::uuid; cid:=envelope->>'collection_id'; cursor_id:=(envelope->>'after')::uuid;
            batch_limit:=coalesce((envelope->>'limit')::integer,50); erase_due:=coalesce((envelope->>'erase_due')::boolean,false);
            IF cmd IS NULL OR cid IS NULL OR batch_limit NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION 'Bounded lifecycle request required' USING ERRCODE='22023'; END IF;
            operator_id:={{policy}}.current_actor();
            PERFORM 1 FROM {{policy}}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {{policy}}.is_native_operator() OR NOT {{policy}}.assigned(cid,{{vocabulary_acquire}}) THEN
                RAISE EXCEPTION 'Native acquisition assignment required' USING ERRCODE='42501'; END IF;
            -- Conservative holds and erasure do not acquire or serve data: permitted while migration is fenced.
            IF NOT EXISTS(SELECT 1 FROM {{policy}}.synthetic_staging_limit WHERE collection_id=cid) THEN
                RAISE EXCEPTION 'Explicit synthetic scope required' USING ERRCODE='42501'; END IF;
            fingerprint:=encode(sha256(convert_to(envelope::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_5d06a72edb}}||cmd::text,0));
            SELECT * INTO previous FROM {{policy}}.lifecycle_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>operator_id THEN RAISE EXCEPTION 'Request belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Lifecycle retry changed' USING ERRCODE='40001'; END IF;
                RETURN previous.result || jsonb_build_object('replayed',true);
            END IF;
            FOR item IN SELECT id,collection_id,assessment_id,assessment_revision,purpose,artifact_hash,retention_deadline,state
                FROM {{staging}}.staged_artifact WHERE collection_id=cid AND (cursor_id IS NULL OR id>cursor_id)
                ORDER BY id LIMIT batch_limit FOR UPDATE
            LOOP
                scanned:=scanned+1; last_id:=item.id;
                IF item.state='erased' THEN CONTINUE; END IF;
                PERFORM {{policy}}.lock_acquisition_authority(item.assessment_id,item.assessment_revision,cid);
                SELECT a.* INTO assessment FROM {{policy}}.acquisition_assessment a
                    WHERE a.id=item.assessment_id AND a.revision=item.assessment_revision AND a.collection_id=cid;
                SELECT v.* INTO privacy FROM {{policy}}.privacy_review v WHERE v.id=assessment.privacy_review_id
                    AND v.revision=assessment.privacy_review_revision AND v.collection_id=cid;
                SELECT h.revision INTO ah FROM {{policy}}.revision_head h WHERE h.kind='acquisition' AND h.scope_id=assessment.id::text;
                SELECT h.revision INTO ph FROM {{policy}}.revision_head h WHERE h.kind='privacy' AND h.scope_id=privacy.id::text;
                SELECT h.revision INTO ch FROM {{policy}}.revision_head h WHERE h.kind='controller' AND h.scope_id=privacy.controller_id::text;
                target:=NULL; cause_value:=NULL;
                IF item.state='erasure_required' THEN target:='erasure_required'; cause_value:='erasure_pending';
                ELSIF item.retention_deadline<=statement_timestamp() THEN target:='erasure_required'; cause_value:='retention_due';
                ELSIF EXISTS(SELECT 1 FROM {{policy}}.privacy_review v WHERE v.id=assessment.privacy_review_id
                    AND v.collection_id=cid AND v.revision>=assessment.privacy_review_revision
                    AND v.clearance='rejected' AND v.valid_from<=statement_timestamp()) THEN
                    target:='erasure_required'; cause_value:='privacy_rejected';
                ELSIF item.state IN ('staged','processing','validated') AND NOT
                    {{policy}}.private_operation_allowed(item.assessment_id,item.assessment_revision,cid,item.purpose,item.artifact_hash,{{vocabulary_retain}}) THEN
                    target:='held_pending_reassessment'; cause_value:='authority_not_current';
                END IF;
                IF target IS NOT NULL AND target<>item.state THEN
                    PERFORM {{policy}}.record_staging_transition(item.id,target,cause_value,cmd,ah,ph,ch);
                    IF target='held_pending_reassessment' THEN held:=held+1; ELSE required:=required+1; END IF;
                END IF;
                IF target='erasure_required' AND erase_due THEN
                    PERFORM {{policy}}.record_staging_transition(item.id,'erased','due_erasure',cmd,ah,ph,ch);
                    erased:=erased+1;
                END IF;
            END LOOP;
            result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'scanned',scanned,
                'held',held,'erasure_required',required,'erased',erased,'next_after',CASE WHEN scanned=batch_limit THEN last_id ELSE NULL END);
            INSERT INTO {{policy}}.lifecycle_receipt(command_id,actor_id,collection_id,payload_hash,reason,result)
                VALUES(cmd,operator_id,cid,fingerprint,envelope->>'reason',result);
            RETURN result;
        END $function$
