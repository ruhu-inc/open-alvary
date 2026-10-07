CREATE OR REPLACE FUNCTION {{policy}}.store_canonical_proposal(command jsonb, expected_bytes bytea, expected_projection jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE inputs jsonb; compiled jsonb; bytes bytea; content_hash text; payload jsonb; fingerprint text;
            previous {{policy}}.assembly_receipt; pid uuid; cmd uuid; actor uuid; snap record; run {{staging}}.synthetic_run; result jsonb;
        BEGIN
            inputs:={{policy}}.canonical_inputs(command);
            cmd:=(command->>'command_id')::uuid; pid:=(command->>'proposal_id')::uuid; actor:={{policy}}.current_actor();
            fingerprint:=encode(sha256(convert_to(command::text,'UTF8')),'hex');
            PERFORM pg_advisory_xact_lock(hashtextextended({{role_literal_9f84896138}}||cmd::text,0));
            SELECT * INTO previous FROM {{policy}}.assembly_receipt WHERE command_id=cmd;
            IF previous.command_id IS NOT NULL THEN
                IF previous.actor_id<>actor THEN RAISE EXCEPTION 'Command belongs to another actor' USING ERRCODE='42501'; END IF;
                IF previous.payload_hash<>fingerprint THEN RAISE EXCEPTION 'Canonical retry changed' USING ERRCODE='40001'; END IF;
                IF NOT {{policy}}.canonical_current(pid,command->>'purpose') THEN RAISE EXCEPTION 'Held canonical retry cannot resume' USING ERRCODE='42501'; END IF;
                RETURN previous.result||jsonb_build_object('replayed',true);
            END IF;
            compiled:={{policy}}.oa_compile(inputs,command->'plan'); bytes:=convert_to(compiled->>'text','UTF8');
            IF expected_bytes IS DISTINCT FROM bytes OR expected_projection IS DISTINCT FROM compiled->'projection' THEN RAISE EXCEPTION 'Independent serializer disagreement' USING ERRCODE='23514'; END IF;
            content_hash:=encode(sha256(bytes),'hex'); payload:=jsonb_build_object('plan',command->'plan','projection',compiled->'projection');
            IF octet_length(payload::text)>262144 THEN RAISE EXCEPTION 'Canonical metadata bound exceeded' USING ERRCODE='54000'; END IF;
            SELECT t.id,t.revision,t.snapshot_hash,t.run_id,t.artifact_id INTO snap FROM {{staging}}.staging_snapshot t WHERE t.id=(command->>'snapshot_id')::uuid AND t.revision=(command->>'snapshot_revision')::bigint;
            SELECT r.* INTO run FROM {{staging}}.synthetic_run r WHERE r.id=snap.run_id;
            PERFORM {{policy}}.reserve_assembly_capacity(snap.artifact_id,run.collection_id,octet_length(payload::text)+octet_length(bytes),1);
            INSERT INTO {{staging}}.canonical_proposal(id,snapshot_id,snapshot_revision,snapshot_hash,run_id,artifact_id,collection_id,serialization_profile_id,profile_hash,
                canonical_bytes,payload,content_hash,payload_hash,size_bytes,payload_bytes,actor_id)
                VALUES(pid,snap.id,snap.revision,snap.snapshot_hash,snap.run_id,snap.artifact_id,run.collection_id,'OA-text-1','c7b0dc81d39406fb54870906ae273309d1994c056c444a84038645865015d589',bytes,payload,content_hash,
                    encode(sha256(convert_to(payload::text,'UTF8')),'hex'),octet_length(bytes),octet_length(payload::text),actor);
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                SELECT actor,run.collection_id,a.evidence_id,'canonical_proposal',pid::text,1,command->>'reason'
                    FROM {{policy}}.acquisition_assessment a WHERE a.id=run.assessment_id AND a.revision=run.assessment_revision;
            result:=jsonb_build_object('command_id',cmd,'status','applied','replayed',false,'object_id',pid,'revision',1,'output_hash',content_hash,
                'scanned',0,'held',0,'erasure_required',0,'erased',0,'next_after',NULL);
            INSERT INTO {{policy}}.assembly_receipt(command_id,actor_id,collection_id,action,payload_hash,result)
                VALUES(cmd,actor,run.collection_id,'record_canonical_proposal',fingerprint,result);
            RETURN result;
        END $function$
