CREATE OR REPLACE FUNCTION {{policy}}.validate_approved_tree()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
            IF (SELECT count(*) FROM {{corpus}}.version_node WHERE version_id=NEW.version_id)>200 OR EXISTS(
                WITH RECURSIVE ancestors AS (
                    SELECT id,parent_id,ARRAY[id] AS seen,false AS cycle FROM {{corpus}}.version_node WHERE id=NEW.id
                    UNION ALL SELECT n.id,n.parent_id,a.seen||n.id,n.id=ANY(a.seen) FROM ancestors a JOIN {{corpus}}.version_node n ON n.id=a.parent_id
                        WHERE NOT a.cycle AND cardinality(a.seen)<=200
                ) SELECT 1 FROM ancestors WHERE cycle OR cardinality(seen)>200) THEN
                RAISE EXCEPTION 'Bounded acyclic version tree required' USING ERRCODE='23514'; END IF;
            RETURN NULL;
        END $function$
