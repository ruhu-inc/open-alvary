CREATE OR REPLACE FUNCTION {{policy}}.apply_synthetic_approval_m33(command jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE operator_id uuid;cid text;cmd uuid;fingerprint text;old {{policy}}.approval_receipt;result jsonb;source jsonb;proposal jsonb;structure jsonb;
            binding {{staging}}.synthetic_expression_binding;review {{staging}}.snapshot_review;verification {{policy}}.verification_policy;
            prior {{corpus}}.approved_version;head {{corpus}}.version_head;rep_head {{corpus}}.representation_head;committed {{policy}}.approval_commit;
            vid text;rid text;rev bigint;sig text;bytes bytea;node jsonb;table_value jsonb;cell jsonb;marker jsonb;id_value jsonb;
            node_ids jsonb:='{}';anchor_ids jsonb:='{}';node_id text;parent_id text;anchor_id text;table_id uuid;cell_id uuid;position_value integer;
            record_set jsonb;review_digest text;
        BEGIN
            IF command IS NULL OR octet_length(command::text)>65536 THEN RAISE EXCEPTION 'Bounded private approval required' USING ERRCODE='22023'; END IF;
            cid:=command->>'collection_id';cmd:=(command->>'command_id')::uuid;operator_id:={{policy}}.current_actor();
            PERFORM 1 FROM {{policy}}.actor WHERE id=operator_id FOR SHARE;
            IF operator_id IS NULL OR NOT {{policy}}.is_native_operator() THEN RAISE EXCEPTION 'Native synthetic session required' USING ERRCODE='42501'; END IF;
            IF jsonb_typeof(command->'reason') IS DISTINCT FROM 'string' OR length(btrim(command->>'reason')) NOT BETWEEN 1 AND 2048
                OR jsonb_typeof(command->'purpose') IS DISTINCT FROM 'string' OR length(btrim(command->>'purpose')) NOT BETWEEN 1 AND 2048 THEN RAISE EXCEPTION 'Explicit bounded purpose/reason required' USING ERRCODE='22023'; END IF;
            IF command->>'action'='record_snapshot_review' THEN
                IF NOT {{policy}}.operator_keys(command,ARRAY['command_id','collection_id','purpose','reason','action','review_id','binding_id','binding_hash','proposal_id','snapshot_hash','profile_hash','content_hash','verification_revision','evidence_id','reviewed_candidate_ids','comparison_method','review_scope','requires_exception_review'],ARRAY[]::text[])
                    OR command->>'comparison_method' IS DISTINCT FROM 'human_source_comparison' OR command->>'review_scope' IS DISTINCT FROM 'collection_policy'
                    OR command->'requires_exception_review' IS DISTINCT FROM 'false'::jsonb THEN RAISE EXCEPTION 'Unsupported approval scope' USING ERRCODE='22023'; END IF;
                IF NOT {{policy}}.assigned(cid,'content') THEN RAISE EXCEPTION 'Scoped content comparison required' USING ERRCODE='42501'; END IF;
                source:={{policy}}.approval_source((command->>'proposal_id')::uuid,cid,command->>'purpose',true);proposal:=source->'proposal';
                SELECT * INTO binding FROM {{staging}}.synthetic_expression_binding WHERE id=(command->>'binding_id')::uuid FOR SHARE;
                IF binding.artifact_id IS DISTINCT FROM (proposal->>'artifact_id')::uuid OR binding.collection_id IS DISTINCT FROM cid
                    OR NOT {{policy}}.binding_current(binding.id) OR {{policy}}.synthetic_binding_hash(binding.id) IS DISTINCT FROM command->>'binding_hash'
                    OR proposal->>'snapshot_hash' IS DISTINCT FROM command->>'snapshot_hash' OR proposal->>'profile_hash' IS DISTINCT FROM command->>'profile_hash'
                    OR proposal->>'content_hash' IS DISTINCT FROM command->>'content_hash' THEN RAISE EXCEPTION 'Exact identity/snapshot/profile/content comparison required' USING ERRCODE='42501'; END IF;
                PERFORM 1 FROM {{policy}}.revision_head WHERE kind='verification' AND scope_id=cid FOR SHARE;
                SELECT * INTO verification FROM {{policy}}.verification_policy WHERE collection_id=cid AND revision=(command->>'verification_revision')::bigint;
                IF verification.actor_id IS DISTINCT FROM operator_id OR verification.state<>'approved' OR verification.valid_from>statement_timestamp() OR verification.expires_at<=statement_timestamp()
                    OR NOT EXISTS(SELECT 1 FROM {{policy}}.revision_head WHERE kind='verification' AND scope_id=cid AND revision=verification.revision)
                    OR (SELECT count(*) FROM {{policy}}.verification_material WHERE collection_id=cid AND revision=verification.revision)<>6
                    OR NOT EXISTS(SELECT 1 FROM {{policy}}.evidence WHERE id=(command->>'evidence_id')::uuid AND collection_id=cid)
                    THEN RAISE EXCEPTION 'Current exact collection content policy and evidence required' USING ERRCODE='42501'; END IF;
                IF jsonb_typeof(command->'reviewed_candidate_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(command->'reviewed_candidate_ids') NOT BETWEEN 1 AND 50
                    OR jsonb_array_length(command->'reviewed_candidate_ids')<>jsonb_array_length(source->'inputs'->'order')
                    OR (SELECT count(DISTINCT x) FROM jsonb_array_elements_text(command->'reviewed_candidate_ids') x)<>jsonb_array_length(command->'reviewed_candidate_ids')
                    OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(source->'inputs'->'order') x WHERE NOT(command->'reviewed_candidate_ids' ? x))
                    THEN RAISE EXCEPTION 'Every selected leaf and exclusion requires source comparison' USING ERRCODE='23514'; END IF;
            ELSIF command->>'action'='approve_synthetic_version' THEN
                IF NOT {{policy}}.operator_keys(command,ARRAY['command_id','collection_id','purpose','reason','action','review_id','review_hash','rights_revision','verification_revision','version_id','representation_id','mode'],ARRAY['prior_version_id','expected_rep_revision'])
                    OR coalesce(command->>'mode','') NOT IN ('new_version','representation_revision')
                    OR command->>'version_id' !~ '^ver_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                    OR command->>'representation_id' !~ '^prp_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                    THEN RAISE EXCEPTION 'Invalid private approval command' USING ERRCODE='22023'; END IF;
                IF NOT {{policy}}.assigned(cid,'release') THEN RAISE EXCEPTION 'Scoped release maintainer required' USING ERRCODE='42501'; END IF;
                SELECT id,proposal_id,artifact_id INTO review.id,review.proposal_id,review.artifact_id FROM {{staging}}.snapshot_review WHERE id=(command->>'review_id')::uuid;
                source:={{policy}}.approval_source(review.proposal_id,cid,command->>'purpose',true);proposal:=source->'proposal';structure:=source->'structure';
                SELECT * INTO review FROM {{staging}}.snapshot_review WHERE id=review.id FOR SHARE;
                SELECT * INTO binding FROM {{staging}}.synthetic_expression_binding WHERE id=review.binding_id FOR SHARE;
                IF review.state IS DISTINCT FROM 'active' OR review.collection_id IS DISTINCT FROM cid OR review.review_hash IS DISTINCT FROM command->>'review_hash'
                    OR review.snapshot_hash IS DISTINCT FROM proposal->>'snapshot_hash' OR review.profile_hash IS DISTINCT FROM proposal->>'profile_hash'
                    OR review.content_hash IS DISTINCT FROM proposal->>'content_hash' OR NOT {{policy}}.binding_current(binding.id)
                    OR review.payload->>'binding_hash' IS DISTINCT FROM {{policy}}.synthetic_binding_hash(binding.id)
                    OR review.verification_revision IS DISTINCT FROM (command->>'verification_revision')::bigint THEN RAISE EXCEPTION 'Current exact reviewed snapshot required' USING ERRCODE='42501'; END IF;
                PERFORM {{policy}}.lock_synthetic_approval_policy(cid,(command->>'rights_revision')::bigint,(command->>'verification_revision')::bigint);
                IF review.actor_id IS DISTINCT FROM (SELECT actor_id FROM {{policy}}.verification_policy WHERE collection_id=cid AND revision=review.verification_revision) THEN RAISE EXCEPTION 'Exact accountable content operator_id required' USING ERRCODE='42501'; END IF;
                -- Expression lock serializes first mint as well as revisions; no absent-row race.
                PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_91e81414d2}}||binding.expression_id,0));
            ELSE RAISE EXCEPTION 'Unsupported private approval action' USING ERRCODE='22023'; END IF;
            fingerprint:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_28fcec2be8}}||cmd::text,0));
            SELECT * INTO old FROM {{policy}}.approval_receipt WHERE command_id=cmd;
            IF old.command_id IS NOT NULL THEN
                IF old.actor_id<>operator_id THEN RAISE EXCEPTION 'Private receipt belongs to another operator_id' USING ERRCODE='42501'; END IF;
                IF old.fingerprint<>fingerprint THEN RAISE EXCEPTION 'Changed approval retry' USING ERRCODE='40001'; END IF;
                IF command->>'action'='approve_synthetic_version' AND NOT {{policy}}.approved_version_current(old.result->>'version_id',command->>'purpose') THEN RAISE EXCEPTION 'Held version retry cannot resume' USING ERRCODE='42501'; END IF;
                RETURN old.result||jsonb_build_object('replayed',true);
            END IF;
            IF command->>'action'='record_snapshot_review' THEN
                review_digest:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
                PERFORM {{policy}}.reserve_assembly_capacity(binding.artifact_id,cid,octet_length(command::text),1);
                INSERT INTO {{staging}}.snapshot_review(id,command_id,binding_id,expression_id,artifact_id,collection_id,proposal_id,snapshot_hash,profile_hash,content_hash,
                    verification_revision,actor_id,evidence_id,review_hash,payload)
                    VALUES((command->>'review_id')::uuid,cmd,binding.id,binding.expression_id,binding.artifact_id,cid,(proposal->>'id')::uuid,proposal->>'snapshot_hash',proposal->>'profile_hash',proposal->>'content_hash',verification.revision,operator_id,(command->>'evidence_id')::uuid,review_digest,command);
                INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                    VALUES(operator_id,cid,(command->>'evidence_id')::uuid,'snapshot_source_compared',command->>'review_id',1,command->>'reason');
                result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'review_hash',review_digest);
            ELSE
                SELECT * INTO committed FROM {{policy}}.approval_commit WHERE expression_id=binding.expression_id AND snapshot_hash=review.snapshot_hash;
                IF committed.expression_id IS NOT NULL THEN
                    IF committed.rights_revision<>(command->>'rights_revision')::bigint OR committed.verification_revision<>review.verification_revision
                        OR NOT {{policy}}.approved_version_current(committed.version_id,command->>'purpose') THEN RAISE EXCEPTION 'Existing frozen approval requires forward review' USING ERRCODE='42501'; END IF;
                    result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',true,'version_id',committed.version_id,'representation_id',committed.rep_id,
                        'revision',(SELECT revision FROM {{corpus}}.representation_revision WHERE id=committed.rep_id),'content_hash',review.content_hash);
                ELSE
                    bytes:=(proposal->>'canonical_bytes')::bytea;
                    -- Span/node/order/grid identity excludes header associations and minted IDs.
                    SELECT jsonb_agg(t-ARRAY['headers']) INTO id_value FROM jsonb_array_elements(structure->'tables') t;
                    sig:=encode(sha256(convert_to(jsonb_build_object('nodes',structure->'nodes','order',structure->'order','grids',coalesce(id_value,'[]'))::text,'UTF8')),'hex');
                    SELECT * INTO head FROM {{corpus}}.version_head WHERE expression_id=binding.expression_id FOR UPDATE;
                    IF head.version_id IS DISTINCT FROM command->>'prior_version_id' THEN RAISE EXCEPTION 'Stale expression head' USING ERRCODE='40001'; END IF;
                    IF head.version_id IS NOT NULL THEN
                        SELECT * INTO prior FROM {{corpus}}.approved_version WHERE id=head.version_id;
                        SELECT * INTO rep_head FROM {{corpus}}.representation_head WHERE version_id=head.version_id FOR UPDATE;
                        IF rep_head.revision IS DISTINCT FROM coalesce((command->>'expected_rep_revision')::bigint,0) THEN RAISE EXCEPTION 'Stale representation head' USING ERRCODE='40001'; END IF;
                    ELSIF coalesce((command->>'expected_rep_revision')::bigint,0)<>0 THEN RAISE EXCEPTION 'Initial representation revision must be zero' USING ERRCODE='40001'; END IF;
                    rid:=command->>'representation_id';
                    IF command->>'mode'='representation_revision' THEN
                        IF prior.id IS NULL OR prior.id IS DISTINCT FROM command->>'version_id' OR prior.artifact_id<>binding.artifact_id
                            OR prior.content_hash<>review.content_hash OR prior.signature_hash<>sig OR NOT {{policy}}.approved_version_current(prior.id,command->>'purpose')
                            THEN RAISE EXCEPTION 'Representation revision must preserve exact bytes nodes order and source' USING ERRCODE='23514'; END IF;
                        vid:=prior.id;rev:=rep_head.revision+1;
                        SELECT jsonb_object_agg(local_key,id) INTO node_ids FROM {{corpus}}.version_node WHERE version_id=vid;
                    ELSE
                        IF prior.id IS NOT NULL AND prior.content_hash=review.content_hash AND prior.signature_hash=sig THEN RAISE EXCEPTION 'Unchanged canonical identity requires representation revision' USING ERRCODE='23514'; END IF;
                        vid:=command->>'version_id';rev:=1;
                        PERFORM {{policy}}.reserve_assembly_capacity(binding.artifact_id,cid,octet_length(bytes)+octet_length(jsonb_build_object('signature_hash',sig)::text),1);
                        INSERT INTO {{corpus}}.approved_version(id,expression_id,binding_id,artifact_id,collection_id,review_id,rights_revision,verification_revision,snapshot_hash,profile_hash,content_hash,signature_hash,canonical_bytes,payload,size_bytes,prior_version_id)
                            VALUES(vid,binding.expression_id,binding.id,binding.artifact_id,cid,review.id,(command->>'rights_revision')::bigint,review.verification_revision,review.snapshot_hash,review.profile_hash,review.content_hash,sig,bytes,jsonb_build_object('signature_hash',sig),octet_length(bytes),head.version_id);
                        FOR node IN SELECT * FROM jsonb_array_elements(structure->'nodes') LOOP
                            node_ids:=jsonb_set(node_ids,ARRAY[node->>'key'],to_jsonb('nod_'||gen_random_uuid()::text));
                        END LOOP;
                        FOR node IN SELECT * FROM jsonb_array_elements(structure->'nodes') LOOP
                            node_id:=node_ids->>(node->>'key');parent_id:=node_ids->>(node->>'parent');
                            IF node->>'end'=node->>'start' THEN node:=node||jsonb_build_object('start',NULL,'end',NULL,'unavailable_reason','empty_source_content'); END IF;
                            INSERT INTO {{corpus}}.version_node(id,version_id,local_key,parent_id,node_kind,candidate_id,start_byte,end_byte,span_hash,unavailable_reason)
                                VALUES(node_id,vid,node->>'key',parent_id,node->>'kind',(node->>'candidate_id')::uuid,(node->>'start')::bigint,(node->>'end')::bigint,
                                    CASE WHEN node->>'start' IS NOT NULL THEN encode(sha256(substring(bytes FROM (node->>'start')::integer+1 FOR (node->>'end')::integer-(node->>'start')::integer)),'hex') END,node->>'unavailable_reason');
                            IF node->>'start' IS NOT NULL THEN
                                IF node->>'candidate_id' IS NOT NULL THEN
                                    INSERT INTO {{corpus}}.node_alignment(node_id,version_id,candidate_id,input_artifact_hash,source_candidate_hash,profile_hash,source_end,target_start,target_end,operations)
                                    SELECT node_id,vid,(node->>'candidate_id')::uuid,binding.artifact_hash,n->>'source_hash',review.profile_hash,(n->>'source_bytes')::bigint,
                                        (node->>'start')::bigint,(node->>'end')::bigint,ARRAY(SELECT jsonb_array_elements_text(n->'operations'))
                                        FROM jsonb_array_elements(structure->'normalizations') n WHERE n->>'candidate_id'=node->>'candidate_id';
                                END IF;
                                INSERT INTO {{corpus}}.approved_anchor(id,version_id,node_id,start_byte,end_byte,span_hash,profile_hash,locator)
                                    SELECT 'anc_'||gen_random_uuid()::text,vid,node_id,start_byte,end_byte,span_hash,review.profile_hash,'node:'||node_id FROM {{corpus}}.version_node WHERE id=node_id
                                    ON CONFLICT(version_id,start_byte,end_byte,profile_hash) DO NOTHING;
                            END IF;
                        END LOOP;
                    END IF;
                    SELECT coalesce(jsonb_object_agg(n.local_key,a.id),'{}') INTO anchor_ids FROM {{corpus}}.version_node n JOIN {{corpus}}.approved_anchor a ON a.version_id=n.version_id AND a.start_byte=n.start_byte AND a.end_byte=n.end_byte AND a.profile_hash=review.profile_hash WHERE n.version_id=vid;
                    record_set:=structure||jsonb_build_object('node_ids',node_ids,'anchor_ids',anchor_ids,'profile_hash',review.profile_hash,'snapshot_hash',review.snapshot_hash,
                        'input_artifact_hash',binding.artifact_hash,'geometry_quality','unavailable','geometry_reason','synthetic_fixture_has_no_physical_geometry','publication_eligible',false);
                    IF octet_length(record_set::text)>262144 THEN RAISE EXCEPTION 'Approved record set bound exceeded' USING ERRCODE='54000'; END IF;
                    PERFORM {{policy}}.reserve_assembly_capacity(binding.artifact_id,cid,octet_length(record_set::text),1);
                    INSERT INTO {{corpus}}.representation_revision(id,version_id,artifact_id,expression_id,revision,parent_rep_id,review_id,snapshot_hash,profile_hash,record_set_hash,payload)
                        VALUES(rid,vid,binding.artifact_id,binding.expression_id,rev,CASE WHEN rev>1 THEN rep_head.rep_id END,review.id,review.snapshot_hash,review.profile_hash,encode(sha256(convert_to(record_set::text,'UTF8')),'hex'),record_set);
                    INSERT INTO {{corpus}}.document_order(rep_id,version_id,position,node_id) SELECT rid,vid,o.ord-1,node_ids->>(o.id#>>'{}') FROM jsonb_array_elements(structure->'order') WITH ORDINALITY o(id,ord);
                    INSERT INTO {{corpus}}.unavailable_mapping(rep_id,version_id,node_id) SELECT rid,vid,id FROM {{corpus}}.version_node WHERE version_id=vid;
                    FOR table_value IN SELECT * FROM jsonb_array_elements(structure->'tables') LOOP
                        table_id:=gen_random_uuid();
                        INSERT INTO {{corpus}}.approved_table(id,rep_id,version_id,node_id,table_local_id,row_count,column_count)
                            VALUES(table_id,rid,vid,node_ids->>(table_value->>'key'),table_value->>'table_local_id',(table_value->>'rows')::integer,(table_value->>'columns')::integer);
                        FOR cell IN SELECT * FROM jsonb_array_elements(table_value->'cells') LOOP
                            cell_id:=gen_random_uuid();
                            INSERT INTO {{corpus}}.approved_cell(id,table_id,rep_id,row_index,column_index,row_span,column_span,role,content_state)
                                VALUES(cell_id,table_id,rid,(cell->>'row')::integer,(cell->>'column')::integer,(cell->>'row_span')::integer,(cell->>'column_span')::integer,
                                    CASE WHEN EXISTS(SELECT 1 FROM jsonb_array_elements_text(cell->'candidates') x WHERE table_value->'headers' ? x) THEN 'header' ELSE 'unknown' END,cell->>'content_state');
                            INSERT INTO {{corpus}}.cell_node(cell_id,rep_id,node_id,version_id,position) SELECT cell_id,rid,node_ids->>(o.id#>>'{}'),vid,o.ord-1 FROM jsonb_array_elements(cell->'candidates') WITH ORDINALITY o(id,ord);
                        END LOOP;
                    END LOOP;
                    FOR marker IN SELECT * FROM jsonb_array_elements(structure->'markers') LOOP
                        INSERT INTO {{corpus}}.footnote_reference(id,rep_id,version_id,target_node_id,marker_start,marker_end,marker_hash)
                            VALUES(gen_random_uuid(),rid,vid,node_ids->>('foot:'||coalesce(marker->>'scope','document')||':'||(marker->>'footnote_id')),(marker->>'start')::bigint,(marker->>'end')::bigint,marker->>'marker_hash');
                    END LOOP;
                    INSERT INTO {{corpus}}.representation_head(version_id,rep_id,revision) VALUES(vid,rid,rev) ON CONFLICT(version_id) DO UPDATE SET rep_id=excluded.rep_id,revision=excluded.revision;
                    INSERT INTO {{corpus}}.version_head(expression_id,version_id,representation_id) VALUES(binding.expression_id,vid,rid) ON CONFLICT(expression_id) DO UPDATE SET version_id=excluded.version_id,representation_id=excluded.representation_id;
                    INSERT INTO {{policy}}.approval_commit(expression_id,snapshot_hash,version_id,rep_id,actor_id,review_id,rights_revision,verification_revision)
                        VALUES(binding.expression_id,review.snapshot_hash,vid,rid,operator_id,review.id,(command->>'rights_revision')::bigint,review.verification_revision);
                    INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                        VALUES(operator_id,cid,review.evidence_id,'synthetic_version_approved',vid,rev,command->>'reason');
                    result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'version_id',vid,'representation_id',rid,'revision',rev,'content_hash',review.content_hash);
                END IF;
            END IF;
            INSERT INTO {{policy}}.approval_receipt(command_id,actor_id,collection_id,fingerprint,result) VALUES(cmd,operator_id,cid,fingerprint,result);
            RETURN result;
        END $function$
