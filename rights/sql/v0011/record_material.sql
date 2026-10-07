CREATE OR REPLACE FUNCTION {{policy}}.record_material()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE v {{policy}}.verification_policy;
        BEGIN
            SELECT * INTO v FROM {{policy}}.verification_policy
                WHERE collection_id=NEW.collection_id AND revision=NEW.revision;
            IF v.actor_id IS DISTINCT FROM {{policy}}.current_actor() OR NOT {{policy}}.assigned(NEW.collection_id,'content') THEN
                RAISE EXCEPTION 'Content reviewer authority required' USING ERRCODE='42501';
            END IF;
            PERFORM 1 FROM {{policy}}.revision_head WHERE kind='verification' AND scope_id=NEW.collection_id
                AND revision=NEW.revision FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Stale verification policy' USING ERRCODE='40001'; END IF;
            INSERT INTO {{policy}}.audit_event(actor_id,collection_id,evidence_id,kind,scope_id,revision,reason)
                VALUES(v.actor_id,NEW.collection_id,NEW.competence_evidence_id,'verification_material',
                    NEW.collection_id||':'||NEW.material_class,NEW.revision,'Material coverage recorded');
            RETURN NEW;
        END $function$
