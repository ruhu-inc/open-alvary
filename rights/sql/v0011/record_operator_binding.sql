CREATE OR REPLACE FUNCTION {{policy}}.record_operator_binding()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$ BEGIN
            IF TG_OP='UPDATE' AND OLD.database_role_name IS NOT NULL AND
                (NEW.id,NEW.database_role,NEW.database_role_name,NEW.subject_issuer,NEW.subject_key)
                IS DISTINCT FROM
                (OLD.id,OLD.database_role,OLD.database_role_name,OLD.subject_issuer,OLD.subject_key) THEN
                RAISE EXCEPTION 'Operator identity cannot be rebound' USING ERRCODE='23514';
            END IF;
            IF NEW.database_role_name IS NOT NULL THEN
                INSERT INTO {{policy}}.operator_event(actor_id,administrator,event_kind,active,database_role,database_role_name,
                    subject_issuer,subject_key,authenticated_until,evidence_reference,evidence_hash,reason)
                VALUES(NEW.id,session_user,CASE WHEN TG_OP='INSERT' THEN 'registered' ELSE 'binding_updated' END,
                    NEW.active,NEW.database_role,NEW.database_role_name,
                    NEW.subject_issuer,NEW.subject_key,NEW.authenticated_until,NEW.binding_evidence_reference,
                    NEW.binding_evidence_hash,NEW.registration_reason);
            END IF;
            RETURN NEW;
        END $function$
