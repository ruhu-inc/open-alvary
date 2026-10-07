CREATE OR REPLACE FUNCTION {{policy}}.staging_live_bytes_m33(cid text)
 RETURNS bigint
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
            SELECT coalesce((SELECT sum(octet_length(private_bytes)) FROM {{staging}}.staged_artifact WHERE collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(c.payload::text)) FROM {{staging}}.adapter_candidate c JOIN {{staging}}.synthetic_run r ON r.id=c.run_id WHERE r.collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(t.payload::text)) FROM {{staging}}.staging_snapshot t JOIN {{staging}}.synthetic_run r ON r.id=t.run_id WHERE r.collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(c.payload::text)+octet_length(c.canonical_bytes)) FROM {{staging}}.canonical_proposal c WHERE c.collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(payload::text)) FROM {{staging}}.synthetic_expression_binding WHERE collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(payload::text)) FROM {{staging}}.snapshot_review WHERE collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(payload::text)+octet_length(canonical_bytes)) FROM {{corpus}}.approved_version WHERE collection_id=cid),0)
                +coalesce((SELECT sum(octet_length(r.payload::text)) FROM {{corpus}}.representation_revision r JOIN {{corpus}}.approved_version v ON v.id=r.version_id WHERE v.collection_id=cid),0)
        $function$
