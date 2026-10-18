"""Capability roles hold the same privileges whenever they were provisioned, before migrating or after.

A migration that grants only to roles that already exist (guarded by to_regrole) needs a matching
bundle in rights/sql/provision, and one that renames a function aside keeps the grants roles already
hold on it. Both make privileges depend on the order; this test compares provisioning at every
revision since 0003 with provisioning after migrating to head, so a new migration joins it unasked."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from postgres_support import database, migrate, validated_test_url
from sqlalchemy import text

from rights.database import PolicyNamespace, provision_roles

pytestmark = pytest.mark.postgres

KINDS = ("guard", "serving", "acquisition", "review", "release")
SCHEMAS = ("policy", "staging", "corpus", "accounts", "changes")

# Effective privileges: a NULL ACL means the owner's defaults, which acldefault spells out.
HELD = """
SELECT 'table' AS what, n.nspname AS schema, c.relname AS name, a.grantee, a.privilege_type, a.is_grantable
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,
    acldefault(CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char", c.relowner))) a
WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r','p','v','m','f','S')
UNION ALL
SELECT 'column', n.nspname, c.relname || '.' || t.attname, a.grantee, a.privilege_type, a.is_grantable
FROM pg_attribute t JOIN pg_class c ON c.oid=t.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
CROSS JOIN LATERAL aclexplode(t.attacl) a
WHERE n.nspname = ANY(:schemas) AND t.attnum > 0 AND NOT t.attisdropped
UNION ALL
SELECT 'schema', n.nspname, '', a.grantee, a.privilege_type, a.is_grantable
FROM pg_namespace n CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
WHERE n.nspname = ANY(:schemas)
UNION ALL
SELECT 'function', n.nspname, p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
    a.grantee, a.privilege_type, a.is_grantable
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
WHERE n.nspname = ANY(:schemas)
UNION ALL
SELECT 'owner', n.nspname, c.relname, c.relowner, 'OWNER', false
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname = ANY(:schemas)
UNION ALL
SELECT 'owner', n.nspname, p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
    p.proowner, 'OWNER', false
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname = ANY(:schemas)
"""


def held(conn, ns):
    """What each capability role kind holds, keyed by role kind, schema kind and unqualified name."""
    schemas = {ns.base: "base", **{ns.schema(k): k for k in SCHEMAS}}
    roles = {conn.scalar(text("SELECT to_regrole(:r)::oid"), {"r": ns.role(k)}): k for k in KINDS}

    def unqualified(name):
        # Function arguments name types by schema; longest first, since the base prefixes the rest.
        for schema in sorted(schemas, key=len, reverse=True):
            name = name.replace(schema + ".", schemas[schema] + ".")
        return name

    return {
        (roles[r.grantee], r.what, schemas[r.schema], unqualified(r.name), r.privilege_type, r.is_grantable)
        for r in conn.execute(text(HELD), {"schemas": list(schemas)})
        if r.grantee in roles
    }


def revisions():
    """Every revision roles could be provisioned at before migrating on: 0003, which creates the
    capabilities, up to the one before head."""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    head = script.get_current_head()
    return sorted(r.revision for r in script.walk_revisions() if "0003" <= r.revision != head)


@contextmanager
def provisioned(url, revision):
    """A fresh database at head whose roles were provisioned at the given revision."""
    with database(url, "test_" + uuid4().hex) as (engine, _):
        migrate(engine, revision)
        with engine.begin() as conn:
            ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
            roles = provision_roles(conn, ns)
        try:
            migrate(engine)
            with engine.connect() as conn:
                yield held(conn, ns)
        finally:
            with engine.begin() as conn:
                quote = conn.dialect.identifier_preparer.quote_identifier
                for kind in ("policy", "staging", "corpus"):
                    conn.execute(text(f"DROP SCHEMA {quote(ns.schema(kind))} CASCADE"))
                for role in roles.values():
                    conn.execute(text(f"DROP OWNED BY {quote(role)}"))
                    conn.execute(text(f"DROP ROLE {quote(role)}"))


@pytest.fixture(scope="module")
def migrated_first():
    with provisioned(validated_test_url(), "head") as privileges:
        return privileges


@pytest.mark.fresh_migrations
@pytest.mark.parametrize("revision", revisions())
def test_capability_roles_hold_the_same_privileges_whenever_they_were_provisioned(migrated_first, revision):
    with provisioned(validated_test_url(), revision) as provisioned_first:
        pass
    # The comparison sees the grants both orders share, not two empty sets.
    assert ("serving", "schema", "changes", "", "USAGE", False) in migrated_first
    assert {
        "only when migrated first": migrated_first - provisioned_first,
        "only when provisioned first": provisioned_first - migrated_first,
    } == {"only when migrated first": set(), "only when provisioned first": set()}
