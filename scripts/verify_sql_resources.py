"""Verify reviewed SQL revision pins and installed definitions without reading corpus rows."""

import ast
import json
import os
import re
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from rights.capabilities import PROVISION_HASH, selected_grants
from rights.database import PolicyNamespace
from rights.sql_resources import bundle_hash, context, normalize_body

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def verify(conn):
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    pinned = 0
    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.py")):
        tree = ast.parse(migration.read_text())
        pins = [
            ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RESOURCE_HASH" for t in n.targets)
        ]
        for pin in pins:
            pinned += 1
            if bundle_hash("v" + migration.name[:4]) != pin:
                raise ValueError("SQL revision resource pin differs")
    # Later immutable bundles override only the reviewed signatures they replace.
    installed = {}
    bundles = sorted(r.name for r in files("rights").joinpath("sql").iterdir() if r.name[:1] == "v")
    for revision in bundles:
        root = files("rights").joinpath("sql", revision)
        if not root.joinpath("manifest.json").is_file():
            continue
        manifest = json.loads(root.joinpath("manifest.json").read_text())
        vocabulary_bytes = files("schema").joinpath(manifest["vocabulary_file"]).read_bytes()
        vocabulary = json.loads(vocabulary_bytes)
        if sha256(vocabulary_bytes).hexdigest() != manifest["vocabulary_hash"]:
            raise ValueError("Pinned SQL vocabulary differs")
        tokens = context(conn, ns, manifest["literals"], vocabulary)
        for entry in manifest["functions"] + manifest.get("new_functions", []):
            installed[entry["signature"]] = (entry, tokens)
    for signature, (entry, tokens) in installed.items():
        definition = conn.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
            {"signature": tokens["policy"] + "." + signature},
        )
        if (
            definition is None
            or sha256(normalize_body(definition, tokens).encode()).hexdigest()
            != entry["current_definition_hash"]
        ):
            raise ValueError("Installed SQL definition differs from reviewed revision")
    verify_authority(conn, ns, installed)
    print(
        f"PASS: {pinned} immutable SQL resource bundles and pinned grants; {len(installed)} installed "
        "definitions, bodies, owners and EXECUTE grantees match"
    )


def verify_authority(conn, ns, installed):
    """Every private policy function is pinned, uniformly owned and never PUBLIC-executable."""
    if bundle_hash("provision") != PROVISION_HASH:
        raise ValueError("Capability grant bundles differ from the reviewed pin")
    roles = {kind: ns.role(kind) for kind in ("guard", "serving", "acquisition", "review", "release")}
    rows = conn.execute(
        text("""
        SELECT p.proname || '(' || coalesce((SELECT string_agg(format_type(t, NULL), ', ' ORDER BY i)
                FROM unnest(p.proargtypes::oid[]) WITH ORDINALITY u(t, i)), '') || ')' AS signature,
            pg_catalog.pg_get_userbyid(p.proowner) AS owner,
            ARRAY(SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee) END
                FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                WHERE a.privilege_type='EXECUTE') AS grantees
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=:policy
        """),
        {"policy": ns.schema("policy")},
    ).all()
    if {r.signature for r in rows} != set(installed):
        raise ValueError("Policy functions differ from the reviewed SQL inventory")
    provisioned = conn.scalar(text("SELECT to_regrole(:guard) IS NOT NULL"), {"guard": roles["guard"]})
    owners = {r.owner for r in rows}
    if len(owners) != 1 or (provisioned and owners != {roles["guard"]}):
        raise ValueError("Policy function ownership differs from the guard boundary")
    allowed = owners | (set(roles.values()) if provisioned else set())
    if any("PUBLIC" in r.grantees or set(r.grantees) - allowed for r in rows):
        raise ValueError("Policy function EXECUTE grant outside the capability roles")
    disabled = conn.scalar(
        text("""
        SELECT count(*) FROM pg_catalog.pg_trigger g JOIN pg_catalog.pg_class c ON c.oid=g.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE NOT g.tgisinternal AND g.tgenabled='D' AND n.nspname = ANY(:schemas)
        """),
        {"schemas": [ns.base, *(ns.schema(k) for k in ("policy", "staging", "corpus", "changes"))]},
    )
    if disabled:
        raise ValueError("A policy, journal, fence or append-only trigger is disabled")
    if provisioned:
        verify_capability_grants(conn, ns, roles, rows)


GRANT = re.compile(r"GRANT (.+?) ON (FUNCTION )?(.+) TO (.+?);?$", re.S)


def split_objects(objects):
    """Split a comma-separated object list whose function signatures contain commas."""
    parts, depth, current = [], 0, ""
    for char in objects:
        depth += char == "("
        depth -= char == ")"
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    return [*parts, current.strip()]


TYPE_ALIASES = {
    "timestamptz": "timestampwithtimezone",
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "bool": "boolean",
    "varchar": "charactervarying",
}


def bare(name):
    """Unqualified object name, whitespace/quotes removed and type aliases canonical, for comparison."""
    name = name.replace('"', "").replace(" ", "")
    head, _, rest = name.partition("(")
    if rest:
        types = [TYPE_ALIASES.get(t, t) for t in rest.rstrip(")").split(",")] if rest != ")" else []
        rest = ",".join(types) + ")"
    return head.rsplit(".", 1)[-1] + ("(" + rest if rest else "")


def verify_capability_grants(conn, ns, roles, functions):
    """Runtime roles hold exactly the function and table privileges the pinned bundles grant."""
    grants, _ = selected_grants(conn, ns)
    runtime = {roles[k] for k in ("serving", "acquisition", "review", "release")}
    expected_functions, expected_tables = set(), set()
    for statement in (s.strip() for g in grants for s in g.split(";\n") if s.strip()):
        match = GRANT.match(statement.strip().rstrip(";"))
        if not match or " SCHEMA " in f" {match[3]} " or match[3].startswith("ALL TABLES"):
            continue
        grantees = {g.strip().replace('"', "") for g in match[4].split(",")} & runtime
        for target in split_objects(match[3]):
            for grantee in grantees:
                if match[2]:
                    expected_functions.add((bare(target), grantee))
                else:
                    for privilege in match[1].split(","):
                        expected_tables.add((bare(target), privilege.strip().upper(), grantee))
    actual_functions = {
        (r.signature.replace(" ", ""), grantee)
        for r in functions
        for grantee in r.grantees
        if grantee in runtime
    }
    if actual_functions - expected_functions:
        raise ValueError("A runtime role holds an unreviewed function grant")
    actual_tables = {
        (row.relname, row.privilege, row.grantee)
        for row in conn.execute(
            text("""
            SELECT c.relname, a.privilege_type AS privilege, pg_catalog.pg_get_userbyid(a.grantee) AS grantee
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL aclexplode(c.relacl) a
            WHERE n.nspname = ANY(:schemas) AND c.relacl IS NOT NULL
            """),
            {"schemas": [ns.base, *(ns.schema(k) for k in ("policy", "staging", "corpus", "changes"))]},
        )
        if row.grantee in runtime
    }
    if actual_tables - expected_tables:
        raise ValueError("A runtime role holds an unreviewed table privilege")


if __name__ == "__main__":
    raw = os.environ.get("DATABASE_URL")
    url = make_url(raw) if raw else None
    if url is None or url.drivername != "postgresql+psycopg" or not url.database.endswith("_test"):
        raise SystemExit("Explicit isolated PostgreSQL _test DATABASE_URL required")
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            verify(conn)
    finally:
        engine.dispose()
