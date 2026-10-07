CREATE OR REPLACE FUNCTION {{policy}}.approved_version_current_m33(vid text, purpose_value text)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT EXISTS(SELECT 1 FROM {{corpus}}.approved_version v JOIN {{corpus}}.representation_head h ON h.version_id=v.id
                JOIN {{corpus}}.representation_revision r ON r.id=h.rep_id JOIN {{staging}}.snapshot_review review ON review.id=r.review_id
                WHERE v.id=vid AND v.state='active' AND r.state='active' AND {{policy}}.binding_current(v.binding_id)
                AND {{policy}}.assembly_input_current(v.artifact_id,purpose_value) AND {{policy}}.approval_policy_current(v.collection_id,v.rights_revision,v.verification_revision)
                AND review.verification_revision=v.verification_revision)
        $function$
