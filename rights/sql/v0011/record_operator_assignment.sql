CREATE OR REPLACE FUNCTION {{policy}}.record_operator_assignment()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE subject {{policy}}.actor; assignment {{policy}}.assignment;
        BEGIN
            IF TG_OP='INSERT' THEN assignment:=NEW; ELSE assignment:=OLD; END IF;
            SELECT * INTO subject FROM {{policy}}.actor WHERE id=assignment.actor_id
                AND database_role_name IS NOT NULL FOR UPDATE;
            IF subject.id IS NOT NULL THEN
                INSERT INTO {{policy}}.operator_event(actor_id,administrator,event_kind,collection_id,capability,
                    active,database_role,database_role_name,subject_issuer,subject_key,authenticated_until,
                    evidence_reference,evidence_hash,reason)
                VALUES(subject.id,session_user,CASE WHEN TG_OP='INSERT' THEN 'assignment_added' ELSE 'assignment_removed' END,
                    assignment.collection_id,assignment.capability,subject.active,subject.database_role,
                    subject.database_role_name,subject.subject_issuer,subject.subject_key,subject.authenticated_until,
                    subject.binding_evidence_reference,subject.binding_evidence_hash,'Collection assignment changed by maintenance');
            END IF;
            RETURN NULL;
        END $function$
