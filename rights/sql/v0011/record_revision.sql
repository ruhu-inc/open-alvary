CREATE OR REPLACE FUNCTION {{policy}}.record_revision()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE k text := TG_ARGV[0]; scope text; previous bigint; cap text := TG_ARGV[1];
        BEGIN
            IF NEW.actor_id IS DISTINCT FROM {{policy}}.current_actor() OR NOT {{policy}}.assigned(NEW.collection_id,cap) THEN
                RAISE EXCEPTION 'Actor or collection authority mismatch' USING ERRCODE='42501';
            END IF;
            IF k IN ('collection','verification') THEN scope := NEW.collection_id;
            ELSIF k='override' THEN scope := NEW.version_id;
            ELSE scope := NEW.id::text; END IF;
            INSERT INTO {{policy}}.revision_head VALUES(k,scope,0) ON CONFLICT DO NOTHING;
            SELECT revision INTO previous FROM {{policy}}.revision_head
                WHERE kind=k AND scope_id=scope FOR UPDATE;
            IF NEW.revision <> previous+1 THEN
                RAISE EXCEPTION 'Stale policy revision' USING ERRCODE='40001';
            END IF;
            IF k NOT IN ('collection','verification','override') AND previous>0 THEN
                EXECUTE format('SELECT collection_id FROM %I.%I WHERE id=$1 AND revision=$2',
                    TG_TABLE_SCHEMA,TG_TABLE_NAME) INTO scope USING NEW.id,previous;
                IF scope IS DISTINCT FROM NEW.collection_id THEN
                    RAISE EXCEPTION 'Revision cannot change collection' USING ERRCODE='23514';
                END IF;
                scope := NEW.id::text;
            END IF;
            IF NEW.state='approved' AND NEW.valid_from>statement_timestamp() THEN
                RAISE EXCEPTION 'Future review cannot approve' USING ERRCODE='23514';
            END IF;
            UPDATE {{policy}}.revision_head SET revision=NEW.revision WHERE kind=k AND scope_id=scope;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(NEW.actor_id,NEW.collection_id,NEW.evidence_id,k,scope,NEW.revision,NEW.reason);
            RETURN NEW;
        END $function$
