"""Maintenance preflight for existing guard roles; never a runtime authentication route."""

from sqlalchemy import text


def assert_guard_boundary(conn, namespace):
    names = [namespace.role(k) for k in ("guard", "serving", "acquisition", "review", "release")]
    roles = conn.execute(
        text(
            "SELECT oid,rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=ANY(:names)"
        ),
        {"names": names},
    ).all()
    guard = next((r for r in roles if r.rolname == names[0]), None)
    if guard is None:
        return
    unsafe = len(roles) != 5 or any(any(r[2:]) for r in roles)
    inherited = conn.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE NOT rolsuper AND rolname<>session_user::text AND oid<>:guard AND pg_has_role(oid,:guard,'MEMBER'))"
        ),
        {"guard": guard.oid},
    )
    ownership = conn.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema' AND has_schema_privilege(:guard,oid,'CREATE')) OR EXISTS(SELECT 1 FROM pg_class WHERE relowner=:guard AND relkind IN ('r','p'))"
        ),
        {"guard": guard.oid},
    )
    policy = conn.dialect.identifier_preparer.quote_identifier(namespace.schema("policy"))
    reader_alias_grants = conn.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_proc f CROSS JOIN LATERAL aclexplode(coalesce(f.proacl,acldefault('f',f.proowner))) a WHERE f.oid=to_regprocedure(:signature) AND a.privilege_type='EXECUTE' AND a.grantee<>f.proowner)"
        ),
        {"signature": policy + ".read_approved_synthetic_version_m33(text,text)"},
    )
    if unsafe or inherited or ownership or reader_alias_grants:
        raise RuntimeError("Existing guard authority requires boundary review")
