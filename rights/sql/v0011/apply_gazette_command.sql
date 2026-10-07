CREATE OR REPLACE FUNCTION {{policy}}.apply_gazette_command(command jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE operator_id uuid;cmd uuid;bid uuid;issue_cid text;item_cid text;inputs jsonb;fingerprint text;previous {{policy}}.gazette_receipt;
            r {{staging}}.gazette_item_review;h {{corpus}}.gazette_correspondence_head;range_value jsonb;payload jsonb;result jsonb;rev bigint;gid uuid;digest text;
        BEGIN
            operator_id:={{policy}}.current_actor();PERFORM 1 FROM {{policy}}.actor a WHERE a.id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {{policy}}.is_native_operator() THEN RAISE EXCEPTION 'Native scoped session required' USING ERRCODE='42501'; END IF;
            IF command IS NULL OR octet_length(command::text)>65536 OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                OR length(btrim(command->>'reason')) NOT BETWEEN 1 AND 2048 OR jsonb_typeof(command->'purpose') IS DISTINCT FROM 'string'
                OR length(btrim(command->>'purpose')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Bounded explicit gazette command required' USING ERRCODE='22023'; END IF;
            cmd:=(command->>'command_id')::uuid;bid:=(command->>'binding_id')::uuid;issue_cid:=command->>'issue_collection_id';item_cid:=command->>'item_collection_id';
            IF command->>'action'='record_gazette_item_review' THEN
                IF NOT {{policy}}.operator_keys(command,ARRAY['command_id','issue_collection_id','item_collection_id','binding_id','purpose','reason','action','review_id','binding_hash','issue_version_id','representation_id','record_set_hash','content_hash','profile_hash','ranges','item_rights_revision','item_verification_revision','expected_revision','evidence_id','identification_method','review_scope','requires_exception_review'],ARRAY[]::text[])
                    OR command->>'identification_method' IS DISTINCT FROM 'human_source_identification' OR command->>'review_scope' IS DISTINCT FROM 'collection_policy'
                    OR command->'requires_exception_review' IS DISTINCT FROM 'false'::jsonb THEN RAISE EXCEPTION 'Explicit human item identification required' USING ERRCODE='22023'; END IF;
                IF NOT {{policy}}.assigned(item_cid,'content') THEN RAISE EXCEPTION 'Current item content reviewer required' USING ERRCODE='42501'; END IF;
            ELSIF command->>'action'='approve_gazette_item_correspondence' THEN
                IF NOT {{policy}}.operator_keys(command,ARRAY['command_id','issue_collection_id','item_collection_id','binding_id','purpose','reason','action','review_id','review_hash','correspondence_id','expected_revision'],ARRAY[]::text[]) THEN RAISE EXCEPTION 'Invalid correspondence approval' USING ERRCODE='22023'; END IF;
                IF NOT {{policy}}.assigned(item_cid,'release') OR NOT {{policy}}.assigned(issue_cid,'release') THEN RAISE EXCEPTION 'Independent scoped release authority required' USING ERRCODE='42501'; END IF;
            ELSE RAISE EXCEPTION 'Unsupported gazette command' USING ERRCODE='22023'; END IF;
            inputs:={{policy}}.gazette_inputs(bid,issue_cid,item_cid,command->>'purpose',true);
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_f46bd8af98}}||bid::text,0));
            SELECT * INTO h FROM {{corpus}}.gazette_correspondence_head WHERE binding_id=bid FOR UPDATE;
            IF jsonb_typeof(command->'expected_revision') IS DISTINCT FROM 'number' OR command->>'expected_revision' !~ '^[0-9]{1,2}$'
                OR (command->>'expected_revision')::bigint NOT BETWEEN 0 AND 31 THEN RAISE EXCEPTION 'Bounded exact revision required' USING ERRCODE='22023'; END IF;
            IF command->>'action'='record_gazette_item_review' THEN
                IF inputs->>'binding_hash' IS DISTINCT FROM command->>'binding_hash' OR inputs->'version'->>'id' IS DISTINCT FROM command->>'issue_version_id'
                    OR inputs->'version'->>'content_hash' IS DISTINCT FROM command->>'content_hash' OR inputs->'version'->>'profile_hash' IS DISTINCT FROM command->>'profile_hash'
                    OR inputs->'representation'->>'id' IS DISTINCT FROM command->>'representation_id' OR inputs->'representation'->>'record_set_hash' IS DISTINCT FROM command->>'record_set_hash'
                    THEN RAISE EXCEPTION 'Exact identity/version/representation/profile required' USING ERRCODE='42501'; END IF;
                PERFORM {{policy}}.lock_synthetic_approval_policy(item_cid,(command->>'item_rights_revision')::bigint,(command->>'item_verification_revision')::bigint);
                IF NOT {{policy}}.gazette_item_policy_current(item_cid,(command->>'item_rights_revision')::bigint,(command->>'item_verification_revision')::bigint)
                    OR operator_id IS DISTINCT FROM (SELECT actor_id FROM {{policy}}.verification_policy WHERE collection_id=item_cid AND revision=(command->>'item_verification_revision')::bigint)
                    OR NOT EXISTS(SELECT 1 FROM {{policy}}.evidence WHERE id=(command->>'evidence_id')::uuid AND collection_id=item_cid) THEN RAISE EXCEPTION 'Exact current independent item reviews/evidence required' USING ERRCODE='42501'; END IF;
                IF jsonb_typeof(command->'ranges') IS DISTINCT FROM 'array' OR jsonb_array_length(command->'ranges')<>1 THEN RAISE EXCEPTION 'One complete notice range required' USING ERRCODE='22023'; END IF;
                range_value:=command->'ranges'->0;
                IF NOT {{policy}}.operator_keys(range_value,ARRAY['start_byte','end_byte','span_hash'],ARRAY[]::text[])
                    OR jsonb_typeof(range_value->'start_byte') IS DISTINCT FROM 'number' OR jsonb_typeof(range_value->'end_byte') IS DISTINCT FROM 'number'
                    OR range_value->>'start_byte' IS DISTINCT FROM inputs->'notice'->>'start_byte' OR range_value->>'end_byte' IS DISTINCT FROM inputs->'notice'->>'end_byte'
                    OR range_value->>'span_hash' IS DISTINCT FROM inputs->'notice'->>'span_hash' THEN RAISE EXCEPTION 'Exact complete issue-owned notice interval required' USING ERRCODE='23514'; END IF;
            ELSE
                SELECT * INTO r FROM {{staging}}.gazette_item_review WHERE id=(command->>'review_id')::uuid FOR SHARE;
                IF r.id IS NULL OR r.binding_id<>bid OR r.state<>'active' OR r.review_hash IS DISTINCT FROM command->>'review_hash'
                    OR r.representation_id IS DISTINCT FROM inputs->'representation'->>'id' OR r.record_set_hash IS DISTINCT FROM inputs->'representation'->>'record_set_hash'
                    OR r.expected_revision<>(command->>'expected_revision')::bigint OR r.payload->>'binding_hash' IS DISTINCT FROM inputs->>'binding_hash'
                    OR r.actor_id IS DISTINCT FROM (SELECT actor_id FROM {{policy}}.verification_policy WHERE collection_id=item_cid AND revision=r.item_verification_revision)
                    THEN RAISE EXCEPTION 'Exact current reviewed correspondence required' USING ERRCODE='42501'; END IF;
                PERFORM {{policy}}.lock_synthetic_approval_policy(item_cid,r.item_rights_revision,r.item_verification_revision);
                IF NOT {{policy}}.gazette_item_policy_current(item_cid,r.item_rights_revision,r.item_verification_revision) THEN RAISE EXCEPTION 'Current item retention and approval required' USING ERRCODE='42501'; END IF;
                range_value:=r.payload->'ranges'->0;
            END IF;
            fingerprint:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_7f5aac8ff7}}||cmd::text,0));
            SELECT * INTO previous FROM {{policy}}.gazette_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>operator_id THEN RAISE EXCEPTION 'Receipt belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.fingerprint<>fingerprint THEN RAISE EXCEPTION 'Changed correspondence intent' USING ERRCODE='40001'; END IF;
                IF command->>'action'='record_gazette_item_review' THEN
                    IF NOT EXISTS(SELECT 1 FROM {{staging}}.gazette_item_review WHERE command_id=cmd AND state='active') THEN RAISE EXCEPTION 'Held review cannot resume' USING ERRCODE='42501'; END IF;
                ELSIF NOT {{policy}}.gazette_correspondence_current((previous.result->>'correspondence_id')::uuid,command->>'purpose') THEN RAISE EXCEPTION 'Held correspondence cannot resume' USING ERRCODE='42501'; END IF;
                RETURN previous.result||jsonb_build_object('replayed',true);
            END IF;
            IF coalesce(h.revision,0)<>(command->>'expected_revision')::bigint THEN RAISE EXCEPTION 'Stale correspondence head' USING ERRCODE='40001'; END IF;
            IF command->>'action'='record_gazette_item_review' THEN
                digest:=fingerprint;
                PERFORM {{policy}}.reserve_assembly_capacity((inputs->'binding'->>'artifact_id')::uuid,issue_cid,octet_length(command::text),1);
                INSERT INTO {{staging}}.gazette_item_review(id,command_id,binding_id,artifact_id,issue_collection_id,item_collection_id,actor_id,review_hash,representation_id,record_set_hash,item_rights_revision,item_verification_revision,expected_revision,payload)
                    VALUES((command->>'review_id')::uuid,cmd,bid,(inputs->'binding'->>'artifact_id')::uuid,issue_cid,item_cid,operator_id,digest,command->>'representation_id',command->>'record_set_hash',(command->>'item_rights_revision')::bigint,(command->>'item_verification_revision')::bigint,(command->>'expected_revision')::bigint,command);
                result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'review_hash',digest);
            ELSE
                rev:=coalesce(h.revision,0)+1;gid:=(command->>'correspondence_id')::uuid;
                payload:=jsonb_build_object('binding_hash',inputs->>'binding_hash','review_hash',r.review_hash,'ranges',r.payload->'ranges','profile_hash',inputs->'version'->>'profile_hash','content_hash',inputs->'version'->>'content_hash');
                PERFORM {{policy}}.reserve_assembly_capacity((inputs->'binding'->>'artifact_id')::uuid,issue_cid,octet_length(payload::text),1);
                INSERT INTO {{corpus}}.gazette_correspondence(id,binding_id,artifact_id,review_id,issue_collection_id,item_collection_id,revision,parent_id,representation_id,start_byte,end_byte,span_hash,payload,payload_hash,actor_id)
                    VALUES(gid,bid,(inputs->'binding'->>'artifact_id')::uuid,r.id,issue_cid,item_cid,rev,h.correspondence_id,r.representation_id,(range_value->>'start_byte')::bigint,(range_value->>'end_byte')::bigint,range_value->>'span_hash',payload,encode(sha256(convert_to(payload::text,'UTF8')),'hex'),operator_id);
                INSERT INTO {{corpus}}.gazette_correspondence_head(binding_id,correspondence_id,revision) VALUES(bid,gid,rev) ON CONFLICT(binding_id) DO UPDATE SET correspondence_id=excluded.correspondence_id,revision=excluded.revision;
                result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'correspondence_id',gid,'revision',rev);
            END IF;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(operator_id,item_cid,CASE WHEN r.id IS NULL THEN (command->>'evidence_id')::uuid ELSE (r.payload->>'evidence_id')::uuid END,CASE WHEN rev IS NULL THEN 'gazette_item_identification' ELSE 'gazette_item_correspondence' END,CASE WHEN rev IS NULL THEN command->>'review_id' ELSE bid::text END,coalesce(rev,1),command->>'reason');
            INSERT INTO {{policy}}.gazette_receipt(command_id,actor_id,fingerprint,binding_id,result) VALUES(cmd,operator_id,fingerprint,bid,result);
            RETURN result;
        END $function$
