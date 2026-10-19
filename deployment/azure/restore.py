"""The database restore job (deployment/azure/README.md, phase 5), run inside the Azure network
because the new servers have no public address. Standard library only, on postgres:16-alpine.

1. The dump, the source's roles and its row counts are read from the transfer container
   (TRANSFER, an https://<account>.blob.core.windows.net/transfer/<run>/ address) with the job's
   managed identity.
2. The target must be empty: the job refuses a database that already has tables.
3. The roles a dump cannot carry are made again, so every grant in it has its role: each role the
   source had (but PostgreSQL's own and the administrator), without login. Only the access role
   (ACCESS_ROLE_NAME) logs in, with its password (ACCESS_ROLE_PASSWORD, from Key Vault). Then the
   memberships. Cloud SQL's own roles thus exist here as empty roles that no one can log in as.
4. pg_restore loads the dump with no owners: every object belongs to the server's administrator,
   as on Cloud SQL; the grants to the capability roles come back with it.
5. Each table's rows are counted and compared with the source's counts. The job fails on any
   difference.

Nothing of the data, and no password, is ever printed: only table names and counts.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

COUNTS = (
    "SELECT n.nspname || '.' || c.relname, (xpath('/row/c/text()', query_to_xml("
    "format('SELECT count(*) AS c FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text::bigint"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname NOT LIKE 'pg_toast%' ORDER BY 1"
)


def token() -> str:
    """A token for Azure Storage from the Container Apps managed identity."""
    query = {"resource": "https://storage.azure.com/", "api-version": "2019-08-01"}
    if os.environ.get("AZURE_CLIENT_ID"):
        query["client_id"] = os.environ["AZURE_CLIENT_ID"]
    request = urllib.request.Request(
        f"{os.environ['IDENTITY_ENDPOINT']}?{urllib.parse.urlencode(query)}",
        headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]},
    )
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.load(answer)["access_token"]


def fetch(name: str, target: Path, bearer: str) -> None:
    request = urllib.request.Request(
        os.environ["TRANSFER"].rstrip("/") + "/" + name,
        headers={"Authorization": f"Bearer {bearer}", "x-ms-version": "2023-11-03"},
    )
    with urllib.request.urlopen(request, timeout=600) as answer, target.open("wb") as out:
        while block := answer.read(1 << 20):
            out.write(block)


def libpq(url: str) -> str:
    """The libpq form of the service's SQLAlchemy URL (postgresql+psycopg:// → postgresql://)."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def psql(dsn: str, sql: str) -> str:
    done = subprocess.run(
        ["psql", "--no-psqlrc", "--set=ON_ERROR_STOP=1", "--tuples-only", "--no-align", "--field-separator=\t",
         "--dbname", dsn, "--command", sql],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        raise SystemExit(
            f"psql failed: {done.stderr.strip().splitlines()[-1] if done.stderr.strip() else 'no message'}"
        )
    return done.stdout


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def describe(dsn: str) -> tuple[dict, dict]:
    """The source's roles (but PostgreSQL's own and the administrator), their memberships, and each
    table's row count: what the restore needs beside the dump, and what it checks against."""
    roles = [
        {"name": name, "login": login == "t"}
        for name, login in (
            line.split("\t")
            for line in psql(
                dsn,
                "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%' AND rolname <> current_user ORDER BY 1",
            ).splitlines()
            if line
        )
    ]
    memberships = [
        {"role": role, "member": member, "member_is_administrator": mine == "t"}
        for role, member, mine in (
            line.split("\t")
            for line in psql(
                dsn,
                "SELECT r.rolname, m.rolname, m.rolname = current_user FROM pg_auth_members a"
                " JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member"
                " WHERE r.rolname NOT LIKE 'pg\\_%' AND m.rolname NOT LIKE 'pg\\_%' ORDER BY 1, 2",
            ).splitlines()
            if line
        )
    ]
    return {"roles": roles, "memberships": memberships}, counts(dsn)


def counts(dsn: str) -> dict[str, int]:
    return {
        table: int(count)
        for table, count in (line.split("\t") for line in psql(dsn, COUNTS).splitlines() if line)
    }


def restore(dsn: str, folder: Path) -> int:
    """Restore folder's database.dump, roles.json and counts.json into an empty target."""
    tables = "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
    if psql(dsn, tables).strip() != "0":
        print("The target already has tables: nothing restored.", file=sys.stderr)
        return 1
    roles = json.loads((folder / "roles.json").read_text())
    access = os.environ.get("ACCESS_ROLE_NAME", "open_alvary_access")
    existing = set(psql(dsn, "SELECT rolname FROM pg_roles").split())
    statements = []
    for role in roles["roles"]:
        if role["name"] in existing:
            continue
        if role["name"] == access:
            password = os.environ.get("ACCESS_ROLE_PASSWORD")
            if not password:
                raise SystemExit(f"{access} logs in: ACCESS_ROLE_PASSWORD is needed")
            literal = "'" + password.replace("'", "''") + "'"
            statements.append(
                f"CREATE ROLE {quoted(access)} LOGIN PASSWORD {literal} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            )
        else:
            statements.append(
                f"CREATE ROLE {quoted(role['name'])} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
            )
    administrator = psql(dsn, "SELECT current_user").strip()
    for grant in roles["memberships"]:
        member = administrator if grant["member_is_administrator"] else grant["member"]
        if grant["role"] != member:
            statements.append(f"GRANT {quoted(grant['role'])} TO {quoted(member)}")
    if statements:
        psql(dsn, "BEGIN; " + "; ".join(statements) + "; COMMIT;")
    print(f"Roles: {len(roles['roles'])} ensured, {len(roles['memberships'])} memberships")
    restored = subprocess.run(
        ["pg_restore", "--no-owner", "--exit-on-error", "--single-transaction", "--dbname", dsn, str(folder / "database.dump")],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if restored.returncode != 0:
        lines = restored.stderr.strip().splitlines()
        print("pg_restore failed:", lines[-1] if lines else "no message", file=sys.stderr)
        return 1
    source, target = json.loads((folder / "counts.json").read_text()), counts(dsn)
    differences = {
        t: (source.get(t), target.get(t))
        for t in sorted(set(source) | set(target))
        if source.get(t) != target.get(t)
    }
    print(json.dumps({"tables": len(target), "rows": sum(target.values()), "differences": differences}))
    return 1 if differences else 0


def main(argv: list[str]) -> int:
    """restore.py                   the job: from TRANSFER into $TARGET_URL_VARIABLE (DATABASE_URL)
    restore.py describe FOLDER     roles.json and counts.json of $SOURCE_URL, for the transfer
    restore.py local FOLDER        a restore from a local folder (rehearsals and tests)"""
    if argv[:1] == ["describe"] and len(argv) == 2:
        roles, rows = describe(libpq(os.environ["SOURCE_URL"]))
        (Path(argv[1]) / "roles.json").write_text(json.dumps(roles, indent=1))
        (Path(argv[1]) / "counts.json").write_text(json.dumps(rows, indent=1))
        print(f"{len(roles['roles'])} roles, {len(rows)} tables, {sum(rows.values())} rows")
        return 0
    dsn = libpq(os.environ[os.environ.get("TARGET_URL_VARIABLE", "DATABASE_URL")])
    if argv[:1] == ["local"] and len(argv) == 2:
        return restore(dsn, Path(argv[1]))
    if argv:
        print(main.__doc__, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as private:
        folder, bearer = Path(private), token()
        for name in ("database.dump", "roles.json", "counts.json"):
            fetch(name, folder / name, bearer)
        return restore(dsn, folder)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
