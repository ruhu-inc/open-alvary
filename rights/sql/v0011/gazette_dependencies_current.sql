CREATE OR REPLACE FUNCTION {{policy}}.gazette_dependencies_current(vid text, purpose_value text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT NOT EXISTS(SELECT 1 FROM {{staging}}.gazette_item_binding b LEFT JOIN {{corpus}}.gazette_correspondence_head h ON h.binding_id=b.id
                WHERE b.issue_version_id=vid AND (h.correspondence_id IS NULL OR NOT {{policy}}.gazette_correspondence_current(h.correspondence_id,purpose_value)))
        $function$
