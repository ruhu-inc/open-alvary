"""The database logins the running system signs in with, each able to do what its workloads do and no more.

The owner's login (open_alvary) keeps the schema, and only the migrations and these commands use it:

  open_alvary_service    the public service's console and the credential gate of its keyed API: the console's
                         accounts, through column grants that leave out key hashes. Until the catalogue's
                         login is in use it serves the catalogue too, so it also reads all that one does.
                         It writes neither the catalogue nor the change log.
  open_alvary_catalogue  the public service's catalogue: the website's data path, downloads, sitemaps and
                         what the keyed API reads of the catalogue. It reads what readers may see, writes
                         admission, bytes and traffic only through their functions, and cannot reach the
                         accounts schema at all.
  open_alvary_jobs       the worker, daily and terms jobs: monitors, digests, webhooks, seals, purges and the
                         change log's sweep. It never sees a key hash, a session's token or a sign-in flow's
                         secrets, though it removes the expired ones.
  open_alvary_retrieval  the retrieval and indexer jobs: reads what is public and writes the section index
                         and the generations' state. It cannot reach the accounts schema at all.
  open_alvary_publish    the publish job: writes the catalogue and the change log, not accounts.

None may create or change a table, make a temporary object, grant a role or read the private policy and
staging schemas; each may run only the functions named here. Grants are made to the login directly, as the workbench's access role has
them (accounts.access_admin), and hold whether or not the capability roles of rights.database exist on the
database. A login is never a member of a role, so `SET ROLE` takes it nowhere.

  python -m rights.logins provision   make or repair them: each login's password comes from its URL in
                                      ALVARY_LOGIN_<NAME>_URL. One transaction; safe to run again.
  python -m rights.logins grants      bring existing logins' grants up to date, passwords untouched (the
                                      migrate job's last step, so a new table reaches them first)
  python -m rights.logins check       sign in as each login and prove what it can and cannot do; changes nothing
  python -m rights.logins remove      take them away again, once nothing signs in with them

All but check run as the owner, DATABASE_URL. A table added by a migration reaches no login until it is
named here: tests/test_postgres_logins.py fails until someone has decided who may read it.

Only names and counts are ever printed, never a URL or password.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from rights.database import PolicyNamespace

# What a login reads of a table and can change in it: a grant's privilege list, as PostgreSQL writes it.
READ = "SELECT"
WRITE = "SELECT, INSERT, UPDATE"
FULL = "SELECT, INSERT, UPDATE, DELETE"

# The catalogue: public records, full texts and rights, which only the code's rights gate filters.
CATALOGUE_TABLES = (
    "languages",
    "jurisdictions",
    "authorities",
    "sources",
    "rights",
    "versions",
    "structure",
    "citations",
    "amendments",
    "translations",
)
# The change log and the search index as readers see them, all worked out from what is public.
PUBLISHED = (
    "change_event",
    "coverage_state",
    "document_unit",
    "feed_state",
    "index_generation",
    "index_job",
    "published_state",
    "rights_revision",
    "text_index_state",
    "text_unit",
)
# Reviewed facts about the catalogue, in tables of their own; the change log records all but the last.
RECORDED_FACTS = ("citation", "judicial_treatment", "legal_event", "membership", "membership_suspension")
FACTS = (*RECORDED_FACTS, "currentness_profile")

# api_key without secret_hash; what is left says which keys exist, never what they are.
KEY_COLUMNS = (
    "key_id, kind, organisation_id, holder_membership, holder_person, created_by, name, last_four,"
    " created_at, expires_at, last_used_at, revoked_at, revoked_reason, rotated_from"
)
# The section index, kept in step by whoever changes what is public, with the generations told what is new.
INDEX_WRITES = {
    "text_unit": FULL,
    "document_unit": FULL,
    "text_index_state": "SELECT, UPDATE",
    "index_generation": READ,
    "index_job": "SELECT, INSERT",
}
# The change log's own tables. Entries are only ever added (a trigger refuses the rest), but for the purge.
LOG_WRITES = {
    "change_event": "SELECT, INSERT",
    "coverage_state": WRITE,
    "event_state": WRITE,
    "feed_state": "SELECT, UPDATE",
    "link_state": WRITE,
    "membership_state": FULL,
    "published_state": WRITE,
    "rights_revision": "SELECT, INSERT",
    "suspension_state": FULL,
    "treatment_state": WRITE,
}


def _reading(tables: tuple[str, ...]) -> dict[str, str]:
    return dict.fromkeys(tables, READ)


@dataclass(frozen=True)
class Login:
    role: str
    # Schema kind ("base" is the catalogue's) → table → the privileges this login holds on it.
    tables: dict[str, dict[str, str]]
    # Functions by schema kind and signature. Each is the only way to what it does.
    functions: tuple[str, ...] = ()
    # Privileges on each generation's own vectors table (changes.vectors_*), which appear at run time.
    vectors: str | None = None
    # What must stay out of reach even though the rest of the table is granted: (schema kind, table, column).
    never_reads: tuple[tuple[str, str, str], ...] = field(default=())

    @property
    def environment(self) -> str:
        return f"ALVARY_LOGIN_{self.role.removeprefix('open_alvary_').upper()}_URL"


KEY_HASH = ("accounts", "api_key", "secret_hash")
SESSION_TOKEN = ("accounts", "console_session", "token_hash")
SESSION_CSRF = ("accounts", "console_session", "csrf")
FLOW_SECRETS = tuple(
    ("accounts", "sign_in_flow", column)
    for column in ("state_hash", "browser_hash", "nonce", "verifier", "session_hash")
)
SEALED_SECRET = ("accounts", "webhook_key", "secret_sealed")
UNSUBSCRIBE_TOKEN = ("accounts", "unsubscribe_token", "token_hash")
PROVIDER_SUBJECT = ("accounts", "identity", "subject")

# What a reader may be shown: the catalogue, whose rights gate is in the code, and the change log and the search
# index, worked out from what is public. Nothing of anyone's account.
READERS_SEE = {"base": _reading(CATALOGUE_TABLES), "changes": _reading(PUBLISHED), "corpus": _reading(FACTS)}
# What an anonymous request writes, each only through its function: admission's leases and buckets, the byte
# allowance on texts, and the day's traffic counts.
ANONYMOUS_WRITES = (
    "policy.admit(jsonb)",
    "policy.begin_emission(uuid)",
    "policy.record_traffic(jsonb)",
    "policy.release_admission(uuid)",
    "policy.release_bytes(uuid)",
    "policy.reserve_bytes(jsonb)",
    "policy.settle_bytes(uuid, bigint, text)",
    "policy.traffic_salt(bytea)",
)

# The public service's catalogue: the website's data path, the downloads, the sitemaps and the old anonymous API,
# and what the keyed API and MCP read of the catalogue once their credential has passed. A flaw in any of them
# reaches no account: this login holds no table of the accounts schema, and no function that answers about one.
CATALOGUE = Login(
    "open_alvary_catalogue",
    READERS_SEE,
    ANONYMOUS_WRITES,
    vectors=READ,
    never_reads=(KEY_HASH, SESSION_TOKEN, SEALED_SECRET, PROVIDER_SUBJECT),
)

# The rest of the public service: the console, and the credential gate of the keyed API and MCP. Until the
# catalogue's login is in use, the same process serves the catalogue on this one, so it holds all that does.
SERVICE = Login(
    "open_alvary_service",
    {
        **READERS_SEE,
        "accounts": {
            "access_request": WRITE,
            "actor": FULL,
            "api_key": f"SELECT ({KEY_COLUMNS}), INSERT, UPDATE (expires_at, revoked_at, revoked_reason, rotated_from)",
            "audit_event": "SELECT, INSERT",
            "client_grant": "SELECT, INSERT, UPDATE (revoked_at, revoked_reason)",
            # A session is looked up by its token's hash, the only copy held of it.
            "console_session": "SELECT, INSERT, UPDATE (last_seen_at, authenticated_at, second_factor_at, revoked_at)",
            "digest": READ,
            "entitlement": WRITE,
            "entitlement_event": "INSERT",
            "identity": "SELECT, INSERT, UPDATE (last_used_at), DELETE",
            "inbox_item": READ,
            "inbox_read": "SELECT, INSERT, DELETE",
            "invitation": WRITE,
            "membership": FULL,
            "notification_pref": FULL,
            "organisation": WRITE,
            "person": WRITE,
            "policy_acceptance": "SELECT, INSERT",
            "sign_in_flow": "SELECT, INSERT, DELETE",
            "tier": READ,
            "unsubscribe_token": "SELECT, INSERT, DELETE",
            "usage_day": READ,
            "verified_client": READ,
            "watch": FULL,
            "watch_member": FULL,
            "webhook_delivery": "SELECT, INSERT",
            "webhook_endpoint": FULL,
            "webhook_endpoint_watch": "SELECT, INSERT, DELETE",
            # The webhook test signs with the secret, sealed with a key this service also holds.
            "webhook_key": "SELECT, INSERT, UPDATE (retires_at), DELETE",
        },
    },
    (
        "policy.accounts_access(text)",
        "policy.check_credential(jsonb)",
        "policy.record_usage(jsonb)",
        *ANONYMOUS_WRITES,
    ),
    vectors=READ,
    never_reads=(KEY_HASH,),
)

JOBS = Login(
    "open_alvary_jobs",
    {
        "base": _reading(CATALOGUE_TABLES),
        "changes": {**LOG_WRITES, **INDEX_WRITES},
        "corpus": _reading(RECORDED_FACTS),
        "accounts": {
            "access_request": READ,
            "actor": READ,
            "api_key": "SELECT (revoked_at), DELETE",
            "audit_event": "SELECT, INSERT, DELETE",
            "audit_seal": "SELECT, INSERT",
            "client_grant": "SELECT (revoked_at), DELETE",
            "console_session": "SELECT (expires_at, revoked_at), DELETE",
            "digest": "SELECT, INSERT, DELETE",
            "entitlement": READ,
            # Closing an account deletes it at the provider first: this is the link to do that.
            "identity": "SELECT (id, person_id, issuer, subject)",
            "inbox_item": "SELECT, INSERT, DELETE",
            "invitation": "SELECT (ended_at, expires_at), DELETE",
            "matcher_checkpoint": FULL,
            "membership": READ,
            "notification_pref": READ,
            "organisation": READ,
            "person": "SELECT, DELETE",
            "sign_in_flow": "SELECT (expires_at), DELETE",
            "tier": READ,
            "traffic_day": FULL,
            "traffic_record_day": READ,
            "traffic_record_month": READ,
            "traffic_top": "SELECT, INSERT, DELETE",
            "traffic_visitors": "SELECT, DELETE",
            "unsubscribe_token": "SELECT (expires_at), INSERT, DELETE",
            "usage_day": "SELECT (day), DELETE",
            "watch": "SELECT, UPDATE",
            "watch_member": READ,
            "webhook_delivery": FULL,
            "webhook_endpoint": "SELECT, UPDATE",
            "webhook_endpoint_watch": READ,
            "webhook_key": READ,  # deliveries are signed with it
        },
    },
    (
        "policy.accounts_access(text)",
        "policy.allocate_publication_revision()",
        "accounts.traffic_months(date, integer, integer)",
    ),
    vectors=None,
    never_reads=(KEY_HASH, SESSION_TOKEN, SESSION_CSRF, *FLOW_SECRETS, UNSUBSCRIBE_TOKEN),
)

RETRIEVAL = Login(
    "open_alvary_retrieval",
    {
        "base": _reading(CATALOGUE_TABLES),
        "changes": {
            **INDEX_WRITES,
            "change_event": READ,
            "engine_state": WRITE,
            "feed_state": READ,
            "index_generation": WRITE,
            "index_job": WRITE,
        },
    },
    vectors="SELECT, INSERT, DELETE",
    never_reads=(KEY_HASH, SESSION_TOKEN, SEALED_SECRET, PROVIDER_SUBJECT),
)

PUBLISH = Login(
    "open_alvary_publish",
    {
        "base": dict.fromkeys(CATALOGUE_TABLES, WRITE),
        "changes": {**LOG_WRITES, **INDEX_WRITES},
        "corpus": _reading(RECORDED_FACTS),
    },
    ("policy.allocate_publication_revision()",),
    never_reads=(KEY_HASH, SESSION_TOKEN, SEALED_SECRET, PROVIDER_SUBJECT),
)

LOGINS = {login.role: login for login in (SERVICE, CATALOGUE, JOBS, RETRIEVAL, PUBLISH)}

# Tables no login reaches: the migrations' own, the identity shadow (operators' commands) and the capability
# model's private and maintenance tables. A new table belongs in a plan above or here.
NO_LOGIN = {
    # The approved texts' private projection, reached only through the capability roles' functions.
    "corpus": {
        "approved_anchor",
        "approved_cell",
        "approved_table",
        "approved_version",
        "cell_node",
        "document_order",
        "eligible_metadata",
        "footnote_reference",
        "gazette_correspondence",
        "gazette_correspondence_head",
        "metadata_projection",
        "node_alignment",
        "representation_head",
        "representation_revision",
        "unavailable_mapping",
        "version_head",
        "version_node",
    },
    "base": {
        "alembic_version",
        "identity_collections",
        "identity_expressions",
        "identity_legacy_snapshots",
        "identity_legacy_sources",
        "identity_legacy_versions",
        "identity_manifestations",
        "identity_migration_state",
        "identity_observations",
        "identity_work_manifestations",
        "identity_works",
        "legacy_search_migration_state",
    },
    # A month's table is reached through its parent. So is each generation's vectors table, by plan: below.
    "accounts": {"traffic_record_day_*"},
    "changes": {"vectors_*"},
}

# Where a function running as its owner looks for a name it does not qualify: nowhere else, in this order.
SAFE_SEARCH_PATH = "search_path=pg_catalog, pg_temp"

# A job that cannot get on fails and can be run again; it does not hold others up behind it.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '30s'"


def _quote(conn: Connection, name: str) -> str:
    return conn.dialect.identifier_preparer.quote_identifier(name)


def _schema(namespace: PolicyNamespace, kind: str) -> str:
    return namespace.base if kind == "base" else namespace.schema(kind)


def _vector_tables(conn: Connection, namespace: PolicyNamespace) -> list[str]:
    """Each generation's vectors tables, without a partition's: those are reached through their parents."""
    return list(
        conn.scalars(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') AND c.relname LIKE 'vectors\\_%'"
                " AND NOT c.relispartition ORDER BY 1"
            ),
            {"schema": namespace.schema("changes")},
        )
    )


def _revoke_what_it_holds(conn: Connection, role: str) -> None:
    """Whatever the login holds, gone in the same transaction as the grants that replace it, so that a
    repair leaves nothing behind and no one sees a moment between the two."""
    quoted = _quote(conn, role)
    held = conn.scalars(
        text(
            """
            SELECT 'TABLE ' || c.oid::regclass::text FROM pg_class c
            CROSS JOIN LATERAL aclexplode(c.relacl) x
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND x.grantee = CAST(:role AS oid)
            UNION
            SELECT 'TABLE ' || a.attrelid::regclass::text FROM pg_attribute a
            CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE x.grantee = CAST(:role AS oid)
            UNION
            SELECT 'SEQUENCE ' || c.oid::regclass::text FROM pg_class c
            CROSS JOIN LATERAL aclexplode(c.relacl) x
            WHERE c.relkind = 'S' AND x.grantee = CAST(:role AS oid)
            UNION
            SELECT 'FUNCTION ' || p.oid::regprocedure::text FROM pg_proc p
            CROSS JOIN LATERAL aclexplode(p.proacl) x WHERE x.grantee = CAST(:role AS oid)
            UNION
            SELECT 'SCHEMA ' || quote_ident(n.nspname) FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(n.nspacl) x WHERE x.grantee = CAST(:role AS oid)
            UNION
            SELECT 'DATABASE ' || quote_ident(d.datname) FROM pg_database d
            CROSS JOIN LATERAL aclexplode(d.datacl) x
            WHERE d.datname = current_database() AND x.grantee = CAST(:role AS oid)
            """
        ),
        {"role": conn.scalar(text("SELECT oid FROM pg_roles WHERE rolname = :r"), {"r": role})},
    ).all()
    for found in held:
        conn.exec_driver_sql(
            f"REVOKE ALL ON {found} FROM {quoted}", execution_options={"no_parameters": True}
        )


def apply_grants(conn: Connection, namespace: PolicyNamespace, login: Login) -> int:
    """The login's grants and nothing else; returns how many statements that took."""
    role = _quote(conn, login.role)
    _revoke_what_it_holds(conn, login.role)
    statements = []
    functions = [function.split(".", 1) for function in login.functions]
    for kind in dict.fromkeys([*login.tables, *(kind for kind, _ in functions)]):
        schema = _quote(conn, _schema(namespace, kind))
        statements.append(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        for table, privileges in login.tables.get(kind, {}).items():
            statements.append(f"GRANT {privileges} ON {schema}.{_quote(conn, table)} TO {role}")
    for kind, function in functions:
        schema = _quote(conn, _schema(namespace, kind))
        statements.append(f"GRANT EXECUTE ON FUNCTION {schema}.{function} TO {role}")
    if login.vectors:
        schema = _quote(conn, namespace.schema("changes"))
        for table in _vector_tables(conn, namespace):
            statements.append(f"GRANT {login.vectors} ON {schema}.{_quote(conn, table)} TO {role}")
    for statement in statements:
        conn.exec_driver_sql(statement, execution_options={"no_parameters": True})
    return len(statements)


def _password_clause(conn: Connection, role: str, url: str) -> str:
    """The password of the login's URL, hashed here so that its letters reach no statement or log. Generated
    letters and digits only (Terraform's random_password)."""
    parsed = make_url(url)
    if parsed.username != role:
        raise SystemExit(f"The URL given for {role} signs in as someone else")
    if not re.fullmatch(r"[A-Za-z0-9]{32,128}", parsed.password or ""):
        raise SystemExit(f"The password for {role} must be generated: 32 to 128 letters and digits")
    hashed = conn.connection.driver_connection.pgconn.encrypt_password(
        parsed.password.encode(), role.encode(), b"scram-sha-256"
    )
    return conn.scalar(text("SELECT quote_literal(:v)"), {"v": hashed.decode()})


def _ensure_role(conn: Connection, role: str, url: str) -> None:
    quoted = _quote(conn, role)
    password = _password_clause(conn, role, url)
    # Made with no attribute beyond signing in, which needs no superuser to say; afterwards only its password
    # is set again. problems() refuses a login that has come to hold anything more.
    exists = conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": role})
    statement = (
        f"ALTER ROLE {quoted} LOGIN PASSWORD {password}"
        if exists
        else f"CREATE ROLE {quoted} LOGIN NOINHERIT PASSWORD {password}"
    )
    conn.exec_driver_sql(statement, execution_options={"no_parameters": True})


def _close_to_public(conn: Connection, namespace: PolicyNamespace) -> None:
    """What a login would otherwise hold through being anyone. Every database lets everyone make temporary
    objects, which nothing here uses; and one that came from an older PostgreSQL may let everyone create in the
    catalogue's schema. Taken away where the owner keeps them, as the database's or the schema's owner; otherwise
    problems() says so and stops. A restore without privileges reopens functions, and the database's own grants."""
    database = _quote(conn, conn.scalar(text("SELECT current_database()")))
    conn.exec_driver_sql(f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC")
    months = _quote(conn, namespace.schema("accounts"))
    conn.exec_driver_sql(
        f"REVOKE ALL ON FUNCTION {months}.traffic_months(date, integer, integer) FROM PUBLIC"
    )
    schema = _quote(conn, namespace.base)
    owner_is_mine = conn.scalar(
        text("SELECT pg_has_role(current_user, nspowner, 'USAGE') FROM pg_namespace WHERE nspname = :s"),
        {"s": namespace.base},
    )
    if owner_is_mine:
        conn.exec_driver_sql(f"REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC")


def _reaches_accounts(login: Login) -> bool:
    return "accounts" in login.tables or any(f.startswith("accounts.") for f in login.functions)


def _runs_as_owner(namespace: PolicyNamespace, login: Login) -> set[tuple[str, str]]:
    """Of functions that run with their owner's rights, those it may run: its own, and the one every session calls."""
    allowed = {
        (_schema(namespace, kind), function) for kind, function in (f.split(".", 1) for f in login.functions)
    }
    allowed.add((namespace.base, "identity_check_legacy_authority(boolean)"))
    return allowed


def problems(conn: Connection, namespace: PolicyNamespace, login: Login) -> list[str]:
    """What this login should not be able to do but can, by the catalogs."""
    role = login.role
    found = []
    row = conn.execute(
        text(
            "SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls"
            " FROM pg_roles WHERE rolname = :r"
        ),
        {"r": role},
    ).one()
    if any(row[1:]):
        found.append(f"{role} holds a role attribute beyond signing in")
    if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member = :o)"), {"o": row.oid}):
        found.append(f"{role} is a member of a role")
    owned = conn.scalar(
        text(
            "SELECT (SELECT count(*) FROM pg_class WHERE relowner = :o)"
            " + (SELECT count(*) FROM pg_proc WHERE proowner = :o)"
            " + (SELECT count(*) FROM pg_namespace WHERE nspowner = :o)"
        ),
        {"o": row.oid},
    )
    if owned:
        found.append(f"{role} owns {owned} objects")
    can_create = conn.scalars(
        text(
            "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"
            " AND has_schema_privilege(:r, oid, 'CREATE')"
        ),
        {"r": role},
    ).all()
    if can_create:
        found.append(f"{role} can create in {', '.join(can_create)}")
    if conn.scalar(text("SELECT has_database_privilege(:r, current_database(), 'CREATE')"), {"r": role}):
        found.append(f"{role} can create schemas")
    if conn.scalar(text("SELECT has_database_privilege(:r, current_database(), 'TEMPORARY')"), {"r": role}):
        found.append(f"{role} can make temporary objects")
    staging = namespace.schema("staging")
    if conn.scalar(text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": role, "s": staging}):
        found.append(f"{role} can reach {staging}")
    accounts = namespace.schema("accounts")
    if not _reaches_accounts(login) and conn.scalar(
        text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": role, "s": accounts}
    ):
        found.append(f"{role} can reach {accounts}")
    private_tables = conn.scalars(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = ANY(:s) AND c.relkind IN ('r', 'p', 'v')"
            " AND has_table_privilege(:r, c.oid, 'SELECT, INSERT, UPDATE, DELETE')"
        ),
        {"r": role, "s": [namespace.schema("policy"), staging]},
    ).all()
    if private_tables:
        found.append(f"{role} holds a private table: {', '.join(private_tables)}")
    for kind, table, column in login.never_reads:
        qualified = f"{_quote(conn, _schema(namespace, kind))}.{_quote(conn, table)}"
        if conn.scalar(
            text("SELECT has_column_privilege(:r, CAST(:t AS regclass), :c, 'SELECT')"),
            {"r": role, "t": qualified, "c": column},
        ):
            found.append(f"{role} can read {table}.{column}")
    allowed = _runs_as_owner(namespace, login)
    executable = conn.execute(
        text(
            "SELECT n.nspname, p.proname || '(' || oidvectortypes(p.proargtypes) || ')', p.proconfig"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.prosecdef AND n.nspname !~ '^pg_' AND has_function_privilege(:r, p.oid, 'EXECUTE')"
        ),
        {"r": role},
    ).all()
    for schema, name, settings in executable:
        if (schema, name) not in allowed:
            found.append(f"{role} can run {schema}.{name}")
        if SAFE_SEARCH_PATH not in (settings or ()):
            found.append(f"{role} can run {schema}.{name}, which does not fix its search path")
    return found


def _namespace(conn: Connection) -> PolicyNamespace:
    return PolicyNamespace(conn.scalar(text("SELECT current_schema()")))


def _refuse(found: list[str]) -> None:
    if found:
        raise SystemExit("Refused, nothing changed:\n" + "\n".join(f"- {line}" for line in found))


def provision(engine: Engine, urls: dict[str, str]) -> dict:
    """Make or repair the logins with URLs, in one transaction that checks itself before it commits."""
    with engine.begin() as conn:
        conn.execute(text(LOCK_TIMEOUT))
        namespace = _namespace(conn)
        _close_to_public(conn, namespace)
        done = {}
        for role, login in LOGINS.items():
            if role not in urls:
                continue
            _ensure_role(conn, role, urls[role])
            if not conn.scalar(
                text("SELECT has_database_privilege(:r, current_database(), 'CONNECT')"), {"r": role}
            ):
                database = _quote(conn, conn.scalar(text("SELECT current_database()")))
                conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {database} TO {_quote(conn, role)}")
            done[role] = apply_grants(conn, namespace, login)
            _refuse(problems(conn, namespace, login))
    return done


def grants(engine: Engine) -> dict:
    """The grants of whichever logins exist, passwords untouched: this is the migrate job's last step, and
    before the logins are first made it has nothing to do."""
    with engine.begin() as conn:
        conn.execute(text(LOCK_TIMEOUT))
        namespace = _namespace(conn)
        _close_to_public(conn, namespace)
        done = {}
        for role, login in LOGINS.items():
            if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": role}):
                done[role] = apply_grants(conn, namespace, login)
                _refuse(problems(conn, namespace, login))
    return done


def remove(engine: Engine) -> list[str]:
    """The way all the way back: each login that nothing is signed in with goes, with its grants. The owner
    made them, so it may drop them, without being able to act as them."""
    with engine.begin() as conn:
        signed_in = conn.scalars(
            text("SELECT DISTINCT usename::text FROM pg_stat_activity WHERE usename::text = ANY(:r)"),
            {"r": list(LOGINS)},
        ).all()
        if signed_in:
            raise SystemExit(f"Still signed in: {', '.join(sorted(signed_in))}. Move them back first.")
        removed = []
        for role in LOGINS:
            if conn.scalar(text("SELECT to_regrole(:r)"), {"r": role}) is not None:
                _revoke_what_it_holds(conn, role)
                conn.exec_driver_sql(f"DROP ROLE {_quote(conn, role)}")
                removed.append(role)
    return removed


# Each must be refused, for every login. Every attempt runs inside a transaction that is rolled back.
FORBIDDEN = (
    "CREATE TABLE {base}.intruder (id int)",
    "CREATE SCHEMA intruder",
    "CREATE TEMPORARY TABLE intruder (id int)",
    "CREATE ROLE intruder",
    "ALTER TABLE {base}.sources ADD COLUMN intruder text",
    "DROP TABLE {base}.sources",
    "TRUNCATE {base}.sources",
    "GRANT pg_read_all_data TO CURRENT_USER",
    "SET ROLE open_alvary",
)
# And these, of what each login holds not.
FORBIDDEN_READS = ("SELECT * FROM {policy}.collection_decision", "SELECT * FROM {staging}.staged_artifact")


def _attempt(conn: Connection, statement: str) -> bool:
    """Whether the statement went through; its effect goes with the savepoint either way."""
    savepoint = conn.begin_nested()
    try:
        conn.exec_driver_sql(statement, execution_options={"no_parameters": True})
    except DBAPIError:
        return False
    else:
        return True
    finally:
        savepoint.rollback()


def check_login(engine: Engine, login: Login) -> list[str]:
    """Sign in as the login and try what it should and should not be able to do. Reads ask for no rows; the
    transaction is rolled back at the end whatever happens."""
    found = []
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            if conn.scalar(text("SELECT session_user")) != login.role:
                return [f"Signed in as someone other than {login.role}"]
            namespace = _namespace(conn)
            tokens = {
                "base": _quote(conn, namespace.base),
                "policy": _quote(conn, namespace.schema("policy")),
                "staging": _quote(conn, namespace.schema("staging")),
            }
            for kind, tables in login.tables.items():
                schema = _quote(conn, _schema(namespace, kind))
                for table, privileges in tables.items():
                    read = re.search(r"\bSELECT(?: \(([^)]*)\))?", privileges)
                    if read is None:
                        continue
                    columns = read.group(1) or "*"
                    if not _attempt(conn, f"SELECT {columns} FROM {schema}.{_quote(conn, table)} LIMIT 0"):
                        found.append(f"{login.role} cannot read {table}")
            for statement in (*FORBIDDEN, *FORBIDDEN_READS):
                if _attempt(conn, statement.format(**tokens)):
                    found.append(f"{login.role} could: {statement.split(' {')[0]}")
            for kind, table, column in login.never_reads:
                schema = _quote(conn, _schema(namespace, kind))
                if _attempt(conn, f"SELECT {column} FROM {schema}.{_quote(conn, table)} LIMIT 0"):
                    found.append(f"{login.role} can read {table}.{column}")
            found += _beyond_its_plan(conn, namespace, login)
        finally:
            transaction.rollback()
    return found


# What can be held of a table, and how to ask: at the table or any of its columns, or at the table alone.
TABLE_PRIVILEGES = {
    "SELECT": "has_any_column_privilege",
    "INSERT": "has_any_column_privilege",
    "UPDATE": "has_any_column_privilege",
    "REFERENCES": "has_any_column_privilege",
    "DELETE": "has_table_privilege",
    "TRUNCATE": "has_table_privilege",
    "TRIGGER": "has_table_privilege",
}


def _beyond_its_plan(conn: Connection, namespace: PolicyNamespace, login: Login) -> list[str]:
    """Asked of the catalogs as the signed-in login, so that a grant to PUBLIC counts too: what it holds of every
    table and view in the system's schemas beyond its plan, an extension's own aside; which functions running as
    their owner it can run beyond its own; and whether it can run each of its own."""
    role = login.role
    schemas = {
        kind: _schema(namespace, kind)
        for kind in ("base", "accounts", "changes", "corpus", "policy", "staging")
    }
    planned = {
        (schemas[kind], table): set(re.findall(r"\b[A-Z]+\b", privileges))  # columns are in lower case
        for kind, tables in login.tables.items()
        for table, privileges in tables.items()
    }
    vectors = set(re.findall(r"\b[A-Z]+\b", login.vectors or ""))
    asked = ", ".join(f"{function}(c.oid, '{name}')" for name, function in TABLE_PRIVILEGES.items())
    found = []
    for schema, table, *held in conn.execute(
        text(
            f"SELECT n.nspname, c.relname, {asked} FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = ANY(:s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT EXISTS ("
            "  SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e'"
            " ) ORDER BY 1, 2"
        ),
        {"s": sorted(set(schemas.values()))},
    ):
        may = planned.get((schema, table))
        if may is None and schema == schemas["changes"] and table.startswith("vectors_"):
            may = vectors
        for name, holds in zip(TABLE_PRIVILEGES, held, strict=True):
            if holds and name not in (may or ()):
                found.append(f"{role} can {name.lower()} {schema}.{table}")
    allowed = _runs_as_owner(namespace, login)
    for schema, name in conn.execute(
        text(
            "SELECT n.nspname, p.proname || '(' || oidvectortypes(p.proargtypes) || ')'"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.prosecdef AND n.nspname !~ '^pg_' AND has_function_privilege(p.oid, 'EXECUTE') ORDER BY 1, 2"
        )
    ):
        if (schema, name) not in allowed:
            found.append(f"{role} can run {schema}.{name}")
    for function in login.functions:
        kind, signature = function.split(".", 1)
        name, arguments = signature.split("(", 1)
        qualified = f"{_quote(conn, _schema(namespace, kind))}.{_quote(conn, name)}({arguments}"
        if not conn.scalar(
            text("SELECT has_function_privilege(to_regprocedure(:f), 'EXECUTE')"), {"f": qualified}
        ):
            found.append(f"{role} cannot run {function}")
    return found


def capability_roles(engine: Engine) -> int:
    """How many of rights.database's roles this database has: these logins hold with or without them, and it
    is worth knowing which, since a restore does not carry them over."""
    with engine.connect() as conn:
        namespace = _namespace(conn)
        names = [namespace.role(kind) for kind in ("guard", "serving", "acquisition", "review", "release")]
        return conn.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname = ANY(:n)"), {"n": names})


def login_urls() -> dict[str, str]:
    return {
        role: os.environ[login.environment]
        for role, login in LOGINS.items()
        if os.environ.get(login.environment)
    }


def main(argv=None) -> int:
    from api.store import engine_for

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["provision", "grants", "check", "remove"])
    command = parser.parse_args(argv).command
    if command == "provision":
        engine = engine_for()
        done = provision(engine, login_urls())
        print(json.dumps({"provisioned": done, "capability_roles": capability_roles(engine)}))
        return 0
    if command == "grants":
        print(json.dumps({"granted": grants(engine_for())}))
        return 0
    if command == "remove":
        print(json.dumps({"removed": remove(engine_for())}))
        return 0
    found = []
    urls = login_urls()
    if not urls:
        found.append("No login's URL is set, so nothing was checked")
    for role, url in urls.items():
        engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
        try:
            found += check_login(engine, LOGINS[role])
        finally:
            engine.dispose()
    print(json.dumps({"checked": sorted(urls), "problems": found}, indent=2))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
