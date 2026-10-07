CREATE OR REPLACE FUNCTION {{policy}}.gazette_correspondence_current(gid uuid, purpose_value text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{corpus}}.gazette_correspondence g JOIN {{corpus}}.gazette_correspondence_head h ON h.correspondence_id=g.id AND h.binding_id=g.binding_id AND h.revision=g.revision
                JOIN {{staging}}.gazette_item_binding b ON b.id=g.binding_id JOIN {{staging}}.gazette_item_review r ON r.id=g.review_id
                JOIN {{corpus}}.representation_head rh ON rh.version_id=b.issue_version_id AND rh.rep_id=g.representation_id
                JOIN {{corpus}}.representation_revision rep ON rep.id=rh.rep_id AND rep.record_set_hash=r.record_set_hash
                WHERE g.id=gid AND g.state='active' AND b.state='active' AND r.state='active' AND rep.state='active'
                AND {{policy}}.gazette_identity_current(b.id) AND {{policy}}.approved_version_current_m33(b.issue_version_id,purpose_value)
                AND {{policy}}.gazette_item_policy_current(b.item_collection_id,r.item_rights_revision,r.item_verification_revision))
        $function$
