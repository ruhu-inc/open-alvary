CREATE OR REPLACE FUNCTION {{policy}}.bound_identity_immutable()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF TG_OP='UPDATE' AND to_jsonb(NEW) IS NOT DISTINCT FROM to_jsonb(OLD) THEN RETURN NEW; END IF;
            IF EXISTS(SELECT 1 FROM {{staging}}.synthetic_expression_binding WHERE
                (TG_TABLE_NAME='identity_works' AND work_id=OLD.id) OR
                (TG_TABLE_NAME='identity_expressions' AND expression_id=OLD.id) OR
                (TG_TABLE_NAME='identity_manifestations' AND manifestation_id=OLD.id)) OR EXISTS(SELECT 1 FROM {{staging}}.gazette_item_binding WHERE (TG_TABLE_NAME='identity_works' AND item_work_id=OLD.id) OR (TG_TABLE_NAME='identity_expressions' AND item_expression_id=OLD.id) OR (TG_TABLE_NAME='identity_manifestations' AND manifestation_id=OLD.id)) THEN
                RAISE EXCEPTION 'Frozen synthetic identity requires forward correction' USING ERRCODE='55000'; END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF;RETURN NEW;
        END $function$
