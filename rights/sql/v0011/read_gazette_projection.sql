CREATE OR REPLACE FUNCTION {{policy}}.read_gazette_projection(gid uuid, purpose_value text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        DECLARE meta record;inputs jsonb;result jsonb;g {{corpus}}.gazette_correspondence;
        BEGIN
            SELECT b.id,b.issue_collection_id,b.item_collection_id,b.issue_version_id INTO meta FROM {{corpus}}.gazette_correspondence d JOIN {{staging}}.gazette_item_binding b ON b.id=d.binding_id WHERE d.id=gid;
            IF meta.id IS NULL THEN RETURN NULL; END IF;
            inputs:={{policy}}.gazette_inputs(meta.id,meta.issue_collection_id,meta.item_collection_id,purpose_value,false);
            PERFORM {{policy}}.lock_gazette_dependencies(meta.issue_version_id);
            IF NOT {{policy}}.approved_version_current(meta.issue_version_id,purpose_value) OR NOT {{policy}}.gazette_correspondence_current(gid,purpose_value) THEN RETURN NULL; END IF;
            SELECT * INTO g FROM {{corpus}}.gazette_correspondence WHERE id=gid FOR SHARE;
            SELECT jsonb_build_object('correspondence_id',g.id,'revision',g.revision,'item_work_id',b.item_work_id,'item_expression_id',b.item_expression_id,
                'issue_version_id',v.id,'representation_id',g.representation_id,'notice_node_id',b.notice_node_id,'anchor_id',b.anchor_id,
                'content_hash',v.content_hash,'profile_hash',v.profile_hash,'ranges',g.payload->'ranges','projected_text',convert_from(substring(v.canonical_bytes FROM g.start_byte::integer+1 FOR (g.end_byte-g.start_byte)::integer),'UTF8'),'publication_eligible',false)
                INTO result FROM {{staging}}.gazette_item_binding b JOIN {{corpus}}.approved_version v ON v.id=b.issue_version_id WHERE b.id=g.binding_id;
            RETURN result;
        END $function$
