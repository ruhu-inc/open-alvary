"""The database restore job (deployment/azure/README.md, phase 5; deployment/gcp-terraform/README.md,
phase 5), run inside the cloud's network because the new servers have no public address. Standard
library only, on postgres:16-alpine. The same job serves Azure and Google Cloud: only where the
transfer files are kept differs.

1. The dump, the source's roles and its row counts are read from the transfer place (TRANSFER) with
   the job's own identity: an https://<account>.blob.core.windows.net/transfer/<run>/ address with
   the Container Apps managed identity, or a gs://<bucket>/<run>/ place with the Cloud Run job's
   service account, from Google's metadata server.
2. The target must be empty: the job refuses a database that already has tables.
3. The roles a dump cannot carry are made again, so every grant in it has its role: each role the
   source had (but PostgreSQL's own and the administrator), without login. Only the access role
   (ACCESS_ROLE_NAME) logs in, with its password (ACCESS_ROLE_PASSWORD, from the vault). Then the
   memberships. One cloud's own roles (Cloud SQL's cloudsqlsuperuser, Azure's azure_pg_admin) thus
   exist in the other as empty roles that no one can log in as. A membership already in place is
   left as it is, and so is one in a role the target's platform made and the administrator may not
   grant: the platform keeps its own roles.
4. pg_restore loads the dump with no owners: every object belongs to the server's administrator,
   as on Cloud SQL; the grants to the capability roles come back with it.
5. Each table's rows are counted and compared with the source's counts. The job fails on any
   difference.

Run with the argument dump, the job does the reverse for its own database: pg_dump, the roles and
the counts, written to TRANSFER, for a move away from a cloud whose servers no one outside can reach.

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
FILES = ("database.dump", "roles.json", "counts.json")
GOOGLE_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
GOOGLE_STORAGE = "https://storage.googleapis.com"


def place() -> str:
    return os.environ["TRANSFER"]


def on_google() -> bool:
    return place().startswith("gs://")


def google_object(name: str) -> tuple[str, str]:
    """The bucket and the object's full name for a file in a gs://<bucket>/<run>/ place."""
    bucket, _, prefix = place()[len("gs://") :].partition("/")
    prefix = prefix.strip("/")
    return bucket, (f"{prefix}/{name}" if prefix else name)


def token() -> str:
    """A token for the transfer place's store: from Google's metadata server on Cloud Run, or from the
    Container Apps managed identity."""
    if on_google():
        request = urllib.request.Request(GOOGLE_TOKEN, headers={"Metadata-Flavor": "Google"})
    else:
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
    if on_google():
        bucket, key = google_object(name)
        url = f"{GOOGLE_STORAGE}/storage/v1/b/{bucket}/o/{urllib.parse.quote(key, safe='')}?alt=media"
        headers = {"Authorization": f"Bearer {bearer}"}
    else:
        url = place().rstrip("/") + "/" + name
        headers = {"Authorization": f"Bearer {bearer}", "x-ms-version": "2023-11-03"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=600) as answer:
        with target.open("wb") as out:
            while block := answer.read(1 << 20):
                out.write(block)


def upload(name: str, source: Path, bearer: str) -> None:
    """One file to the transfer place, replacing what is there (a dump is at most a few GB: one request,
    within both stores' single-request limits)."""
    size = str(source.stat().st_size)
    if on_google():
        bucket, key = google_object(name)
        url = f"{GOOGLE_STORAGE}/upload/storage/v1/b/{bucket}/o?uploadType=media&name={urllib.parse.quote(key, safe='')}"
        method = "POST"
        headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/octet-stream"}
    else:
        url = place().rstrip("/") + "/" + name
        method = "PUT"
        headers = {
            "Authorization": f"Bearer {bearer}",
            "x-ms-version": "2023-11-03",
            "x-ms-blob-type": "BlockBlob",
        }
    with source.open("rb") as body:
        request = urllib.request.Request(
            url, data=body, method=method, headers={**headers, "Content-Length": size}
        )
        with urllib.request.urlopen(request, timeout=3600) as answer:
            answer.read()


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


def rows(dsn: str, sql: str) -> list[list[str]]:
    return [line.split("\t") for line in psql(dsn, sql).splitlines() if line]


def describe(dsn: str) -> tuple[dict, dict]:
    """The source's roles (but PostgreSQL's own and the administrator), their memberships, and each
    table's row count: what the restore needs beside the dump, and what it checks against."""
    roles = [
        {"name": name, "login": login == "t"}
        for name, login in rows(
            dsn,
            "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%' AND rolname <> current_user ORDER BY 1",
        )
    ]
    memberships = [
        {"role": role, "member": member, "member_is_administrator": mine == "t"}
        for role, member, mine in rows(
            dsn,
            "SELECT r.rolname, m.rolname, m.rolname = current_user FROM pg_auth_members a"
            " JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member"
            " WHERE r.rolname NOT LIKE 'pg\\_%' AND m.rolname NOT LIKE 'pg\\_%' ORDER BY 1, 2",
        )
    ]
    return {"roles": roles, "memberships": memberships}, counts(dsn)


def counts(dsn: str) -> dict[str, int]:
    return {table: int(count) for table, count in rows(dsn, COUNTS)}


def target_roles(dsn: str) -> dict:
    """What the target server already has: its roles, the ones its administrator may grant, the
    memberships in place, and the administrator's name."""
    return {
        "existing": {name for (name,) in rows(dsn, "SELECT rolname FROM pg_roles")},
        "grantable": {
            name
            for (name,) in rows(
                dsn,
                "SELECT rolname FROM pg_roles WHERE pg_has_role(current_user, oid, 'MEMBER WITH ADMIN OPTION')",
            )
        },
        "memberships": {
            (role, member)
            for role, member in rows(
                dsn,
                "SELECT r.rolname, m.rolname FROM pg_auth_members a"
                " JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member",
            )
        },
        "administrator": psql(dsn, "SELECT current_user").strip(),
    }


def role_statements(roles: dict, target: dict, access: str, password: str | None) -> tuple[list[str], int]:
    """The statements that make the source's roles and memberships again in the target, and how many
    memberships are left as they are: those already in place, and those in a role the target's
    platform made that its administrator may not grant. A role made here is the administrator's to
    grant (PostgreSQL 16 gives its creator that right)."""
    statements = []
    for role in roles["roles"]:
        if role["name"] in target["existing"]:
            continue
        if role["name"] == access:
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
    left = 0
    for grant in roles["memberships"]:
        member = target["administrator"] if grant["member_is_administrator"] else grant["member"]
        if grant["role"] == member:
            continue
        platforms = grant["role"] in target["existing"] and grant["role"] not in target["grantable"]
        if (grant["role"], member) in target["memberships"] or platforms:
            left += 1
            continue
        statements.append(f"GRANT {quoted(grant['role'])} TO {quoted(member)}")
    return statements, left


def restore(dsn: str, folder: Path) -> int:
    """Restore folder's database.dump, roles.json and counts.json into an empty target."""
    tables = "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
    if psql(dsn, tables).strip() != "0":
        print("The target already has tables: nothing restored.", file=sys.stderr)
        return 1
    roles = json.loads((folder / "roles.json").read_text())
    access = os.environ.get("ACCESS_ROLE_NAME", "open_alvary_access")
    statements, left = role_statements(
        roles, target_roles(dsn), access, os.environ.get("ACCESS_ROLE_PASSWORD")
    )
    if statements:
        psql(dsn, "BEGIN; " + "; ".join(statements) + "; COMMIT;")
    print(
        f"Roles: {len(roles['roles'])} ensured, {len(roles['memberships'])} memberships ({left} left as they were)"
    )
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


def dump(dsn: str, folder: Path) -> int:
    """folder's database.dump, roles.json and counts.json, from the database at dsn (writes frozen)."""
    done = subprocess.run(
        ["pg_dump", "--format=custom", "--no-password", "--file", str(folder / "database.dump"), "--dbname", dsn],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        print("pg_dump failed:", lines[-1] if lines else "no message", file=sys.stderr)
        return 1
    roles, tally = describe(dsn)
    (folder / "roles.json").write_text(json.dumps(roles, indent=1))
    (folder / "counts.json").write_text(json.dumps(tally, indent=1))
    print(f"{len(roles['roles'])} roles, {len(tally)} tables, {sum(tally.values())} rows")
    return 0


def main(argv: list[str]) -> int:
    """restore.py                   the job: from TRANSFER into $TARGET_URL_VARIABLE (DATABASE_URL)
    restore.py dump              the job: that database out to TRANSFER (a move away)
    restore.py describe FOLDER     roles.json and counts.json of $SOURCE_URL, for the transfer
    restore.py local FOLDER        a restore from a local folder (rehearsals and tests)"""
    if argv[:1] == ["describe"] and len(argv) == 2:
        roles, tally = describe(libpq(os.environ["SOURCE_URL"]))
        (Path(argv[1]) / "roles.json").write_text(json.dumps(roles, indent=1))
        (Path(argv[1]) / "counts.json").write_text(json.dumps(tally, indent=1))
        print(f"{len(roles['roles'])} roles, {len(tally)} tables, {sum(tally.values())} rows")
        return 0
    dsn = libpq(os.environ[os.environ.get("TARGET_URL_VARIABLE", "DATABASE_URL")])
    if argv[:1] == ["local"] and len(argv) == 2:
        return restore(dsn, Path(argv[1]))
    if argv not in ([], ["dump"]):
        print(main.__doc__, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as private:
        folder = Path(private)
        if argv == ["dump"]:
            if dump(dsn, folder):
                return 1
            bearer = token()
            for name in FILES:
                upload(name, folder / name, bearer)
            print(f"Written to {place()}")
            return 0
        bearer = token()
        for name in FILES:
            fetch(name, folder / name, bearer)
        return restore(dsn, folder)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
