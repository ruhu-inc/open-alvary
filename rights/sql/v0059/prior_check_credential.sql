CREATE OR REPLACE FUNCTION {{policy}}.check_credential(request jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    now_at timestamptz := statement_timestamp();
    key_row {{accounts}}.api_key;
    grant_row {{accounts}}.client_grant;
    organisation_row {{accounts}}.organisation;
    person_state text;
    member_role text;
    credential text;
    access jsonb;
BEGIN
    IF request->>'kind' = 'key' THEN
        IF coalesce(request->>'key_id', '') !~ '^[0-9a-f]{32}$'
            OR coalesce(request->>'secret_hash', '') !~ '^[0-9a-f]{64}$' THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        SELECT * INTO key_row FROM {{accounts}}.api_key WHERE key_id = request->>'key_id';
        IF NOT FOUND OR key_row.secret_hash <> decode(request->>'secret_hash', 'hex')
            OR key_row.revoked_at IS NOT NULL OR key_row.expires_at <= now_at THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        credential := 'key_' || key_row.key_id;
        SELECT * INTO organisation_row FROM {{accounts}}.organisation WHERE id = key_row.organisation_id;
        IF key_row.kind = 'personal' THEN
            SELECT p.state, m.role INTO person_state, member_role
                FROM {{accounts}}.membership m JOIN {{accounts}}.person p ON p.id = m.person_id
                WHERE m.id = key_row.holder_membership;
            IF NOT FOUND OR member_role = 'viewer' THEN
                RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
            END IF;
        END IF;
        UPDATE {{accounts}}.api_key SET last_used_at = now_at
            WHERE key_id = key_row.key_id
                AND (last_used_at IS NULL OR last_used_at < now_at - interval '1 minute');
    ELSIF request->>'kind' = 'grant' THEN
        SELECT * INTO grant_row FROM {{accounts}}.client_grant WHERE id = request->>'grant_id';
        IF NOT FOUND OR grant_row.revoked_at IS NOT NULL OR grant_row.resource <> request->>'resource'
            OR NOT EXISTS (SELECT 1 FROM {{accounts}}.identity i
                WHERE i.person_id = grant_row.person_id AND i.issuer = request->>'issuer'
                    AND i.subject = request->>'subject') THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        credential := grant_row.id;
        SELECT * INTO organisation_row FROM {{accounts}}.organisation WHERE id = grant_row.organisation_id;
        SELECT p.state, m.role INTO person_state, member_role
            FROM {{accounts}}.membership m JOIN {{accounts}}.person p ON p.id = m.person_id
            WHERE m.id = grant_row.membership_id;
        IF NOT FOUND OR member_role = 'viewer' THEN
            RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
        END IF;
        UPDATE {{accounts}}.client_grant SET last_used_at = now_at
            WHERE id = grant_row.id
                AND (last_used_at IS NULL OR last_used_at < now_at - interval '1 minute');
    ELSE
        RETURN jsonb_build_object('ok', false, 'code', 'invalid_credentials');
    END IF;
    IF person_state IS NOT NULL AND person_state <> 'active' THEN
        RETURN jsonb_build_object('ok', false, 'code', 'account_inactive');
    END IF;
    IF organisation_row.state IN ('suspended','deleted') THEN
        RETURN jsonb_build_object('ok', false, 'code', 'organisation_suspended');
    END IF;
    access := {{policy}}.accounts_access(organisation_row.id);
    IF access->>'state' <> 'active' THEN
        RETURN jsonb_build_object('ok', false, 'code',
            CASE WHEN access->>'state' IN ('none','scheduled') THEN 'entitlement_required'
                ELSE 'entitlement_lapsed' END);
    END IF;
    RETURN jsonb_build_object('ok', true, 'organisation', organisation_row.id, 'credential', credential,
        'tier', access->'tier', 'scope', access->'scope', 'ends_at', access->'ends_at');
END
$function$
