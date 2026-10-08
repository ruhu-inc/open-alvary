"""Encrypted PostgreSQL backups, isolated restore verification and explicit retention.

Backups are encrypted with OpenPGP public-key encryption (``gpg --recipient-file``), so the
backup host never holds a decryption key. Plaintext dumps exist only in a private temporary
directory and are removed before storage. PostgreSQL tools are used through stdin/stdout
only, so ``ALVARY_PG_TOOL_PREFIX`` (for example ``docker exec -i -e PGPASSWORD db``) can
supply a server-matched toolchain. Passwords travel through ``PGPASSWORD``, never argv.

Destination, recipient key, retention counts and alert command are operator decisions;
nothing here supplies a default for them. The independent denial-journal checkpoint is
recorded as unavailable until that authority exists; restored policy must stay closed.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

FORMAT = "open-alvary-backup-1"
NAME = re.compile(r"open-alvary-(\d{8}T\d{6}Z)\.dump\.gpg\Z")
ISOLATED_TARGET = re.compile(r".+_(restore|test)\Z")
TABLE_DATA = re.compile(r"^\d+; \d+ \d+ TABLE DATA (\S+) (\S+) ")


class BackupError(RuntimeError):
    """Safe failure: names the stage only, never URLs, credentials or tool output."""

    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(f"Backup operation failed at {stage}")


@dataclass(frozen=True)
class Database:
    """libpq connection split so the password never appears in a process argument list."""

    dsn: str
    password: str | None = field(repr=False)

    @classmethod
    def from_url(cls, url: str) -> "Database":
        parts = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
        if parts.scheme != "postgresql" or not parts.path.strip("/"):
            raise BackupError("configuration")
        host = parts.hostname or ""
        netloc = (unquote(parts.username) + "@" if parts.username else "") + host
        if parts.port:
            netloc += f":{parts.port}"
        password = unquote(parts.password) if parts.password else None
        return cls(urlunsplit(("postgresql", netloc, parts.path, parts.query, "")), password)

    @property
    def name(self) -> str:
        return urlsplit(self.dsn).path.strip("/")


@dataclass(frozen=True)
class Tools:
    prefix: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "Tools":
        return cls(tuple(shlex.split(os.environ.get("ALVARY_PG_TOOL_PREFIX", ""))))

    def run(self, stage, args, database=None, *, stdin=None, stdout=None, input_bytes=None) -> bytes:
        env = dict(os.environ)
        env.pop("PGPASSWORD", None)
        if database is not None and database.password is not None:
            env["PGPASSWORD"] = database.password
        try:
            completed = subprocess.run(
                [*self.prefix, *args],
                stdin=stdin,
                input=input_bytes,
                stdout=stdout if stdout is not None else subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                check=False,
            )
        except OSError:
            raise BackupError(stage) from None
        if completed.returncode != 0:
            raise BackupError(stage)
        return completed.stdout or b""

    def query(self, stage, database, sql, variables=None) -> list[list[str]]:
        args = ["psql", "--no-password", "--no-psqlrc", "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1"]
        for key, value in (variables or {}).items():
            args += ["-v", f"{key}={value}"]
        output = self.run(
            stage, [*args, "--dbname", database.dsn, "-f", "-"], database, input_bytes=sql.encode()
        )
        return [line.split("\t") for line in output.decode().splitlines() if line]


class DirectoryDestination:
    """A mounted off-host location; writes are atomic and never overwrite an existing object."""

    def __init__(self, root: Path):
        self.root = root
        if not root.is_dir():
            raise BackupError("destination")

    def put(self, name: str, source: Path) -> None:
        target = self.root / name
        if target.exists():
            raise BackupError("destination")
        partial = self.root / f".{name}.partial"
        try:
            with source.open("rb") as reader, partial.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(partial, target)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            partial.unlink(missing_ok=True)
            raise BackupError("destination") from None

    def get(self, name: str, target: Path) -> None:
        try:
            shutil.copyfile(self.root / name, target)
        except OSError:
            raise BackupError("destination") from None

    def names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if not p.name.startswith("."))

    def delete(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for block in iter(lambda: reader.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def gpg_home():
    """Throwaway keyring with a short path: agent sockets have a small path-length limit."""
    home = Path(tempfile.mkdtemp(prefix="oa-gpg-", dir="/tmp" if os.path.isdir("/tmp") else None))
    try:
        yield home
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True, check=False
        )
        shutil.rmtree(home, ignore_errors=True)


def gpg(stage, home: Path, *args) -> bytes:
    command = ["gpg", "--homedir", str(home), "--batch", "--no-tty", "--quiet", *args]
    try:
        completed = subprocess.run(command, capture_output=True, check=False)
    except OSError:
        raise BackupError(stage) from None
    if completed.returncode != 0:
        raise BackupError(stage)
    return completed.stdout


def recipient_fingerprints(home: Path, recipient_file: Path) -> list[str]:
    listing = gpg("encrypt", home, "--with-colons", "--show-keys", str(recipient_file)).decode()
    fingerprints = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
    if not fingerprints or "sec:" in listing:
        # A secret key on the backup host would defeat public-key-only custody.
        raise BackupError("encrypt")
    return fingerprints


def table_inventory(tools, plain: Path) -> list[str]:
    with plain.open("rb") as reader:
        listing = tools.run("validate", ["pg_restore", "--list"], stdin=reader).decode()
    tables = sorted({f"{m[1]}.{m[2]}" for m in map(TABLE_DATA.match, listing.splitlines()) if m})
    if not tables:
        raise BackupError("validate")
    return tables


REVISION_SQL = "SELECT version_num FROM alembic_version;"


def revision(tools, stage, database) -> str:
    rows = tools.query(stage, database, REVISION_SQL)
    if len(rows) != 1:
        raise BackupError(stage)
    return rows[0][0]


def create(database, recipient_file: Path, destination, tools, now: datetime) -> dict:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"open-alvary-{stamp}.dump.gpg"
    with tempfile.TemporaryDirectory(prefix="oa-backup-") as tmp, gpg_home() as home:
        work = Path(tmp)
        os.chmod(work, 0o700)
        fingerprints = recipient_fingerprints(home, recipient_file)
        before = revision(tools, "dump", database)
        plain = work / "database.dump"
        with plain.open("xb") as writer:
            tools.run(
                "dump",
                ["pg_dump", "--format=custom", "--no-password", "--dbname", database.dsn],
                database,
                stdout=writer,
            )
        if revision(tools, "dump", database) != before:
            raise BackupError("dump")
        tables = table_inventory(tools, plain)
        dump_hash, dump_bytes = sha256(plain), plain.stat().st_size
        encrypted = work / name
        gpg(
            "encrypt",
            home,
            "--trust-model",
            "always",
            "--recipient-file",
            str(recipient_file),
            "--output",
            str(encrypted),
            "--encrypt",
            str(plain),
        )
        plain.unlink()
        metadata = {
            "format": FORMAT,
            "backup": name,
            "created_at": now.astimezone(UTC).isoformat(),
            "alembic_revision": before,
            "dump_sha256": dump_hash,
            "dump_bytes": dump_bytes,
            "encrypted_sha256": sha256(encrypted),
            "encryption": {"method": "openpgp-public-key", "recipients": fingerprints},
            "tables": tables,
            # M4.2 supplies the independent denial-journal checkpoint; until then a restore
            # cannot prove denial continuity and must stay closed to serving.
            "journal_checkpoint": None,
        }
        sidecar = work / (name + ".json")
        sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        destination.put(name, encrypted)
        # The metadata sidecar is the completion marker, written only after the archive.
        destination.put(name + ".json", sidecar)
    return metadata


COUNT_SQL = """
SELECT t, CASE WHEN to_regclass(t) IS NULL THEN 'missing' ELSE (xpath('/row/c/text()',
    query_to_xml(format('SELECT count(*) AS c FROM %s', to_regclass(t)), false, true, '')))[1]::text END
FROM jsonb_array_elements_text(:'tables'::jsonb) t ORDER BY t;
"""
EMPTY_SQL = """
SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
    AND c.relkind IN ('r','p','v','m','S','f');
"""


def restore_verify(name, destination, identity_file: Path, target, tools) -> dict:
    """Restore into an empty isolated database and verify; never targets a serving database."""
    if not NAME.fullmatch(name) or not ISOLATED_TARGET.fullmatch(target.name):
        raise BackupError("configuration")
    with tempfile.TemporaryDirectory(prefix="oa-restore-") as tmp, gpg_home() as home:
        work = Path(tmp)
        os.chmod(work, 0o700)
        sidecar, encrypted = work / "metadata.json", work / "backup.gpg"
        destination.get(name + ".json", sidecar)
        destination.get(name, encrypted)
        metadata = json.loads(sidecar.read_text())
        if metadata.get("format") != FORMAT or metadata.get("backup") != name:
            raise BackupError("metadata")
        if sha256(encrypted) != metadata["encrypted_sha256"]:
            raise BackupError("integrity")
        gpg("decrypt", home, "--import", str(identity_file))
        plain = work / "database.dump"
        gpg("decrypt", home, "--output", str(plain), "--decrypt", str(encrypted))
        if sha256(plain) != metadata["dump_sha256"] or table_inventory(tools, plain) != metadata["tables"]:
            raise BackupError("integrity")
        if tools.query("restore", target, EMPTY_SQL) != [["0"]]:
            raise BackupError("restore")
        with plain.open("rb") as reader:
            tools.run(
                "restore",
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-privileges",
                    "--no-password",
                    "--dbname",
                    target.dsn,
                ],
                target,
                stdin=reader,
            )
        plain.unlink()
    if revision(tools, "verify", target) != metadata["alembic_revision"]:
        raise BackupError("verify")
    counts = dict(
        map(tuple, tools.query("verify", target, COUNT_SQL, {"tables": json.dumps(metadata["tables"])}))
    )
    if set(counts) != set(metadata["tables"]) or "missing" in counts.values():
        raise BackupError("verify")
    return {
        "backup": name,
        "alembic_revision": metadata["alembic_revision"],
        "tables": len(counts),
        "rows": sum(int(v) for v in counts.values()),
        # Ownership/grants are deliberately not restored: capability roles must be
        # reprovisioned and policy reconciled before any serving epoch opens.
        "serving": "closed",
        "journal_checkpoint": metadata["journal_checkpoint"],
    }


def retained(names: list[str], keep_daily: int, keep_weekly: int) -> set[str]:
    """Newest backup per day for the newest keep_daily days, likewise per ISO week."""
    stamps = sorted(
        ((datetime.strptime(m[1], "%Y%m%dT%H%M%SZ"), n) for n in names if (m := NAME.fullmatch(n))),
        reverse=True,
    )
    keep, days, weeks = set(), [], []
    for moment, name in stamps:
        day, week = moment.date(), moment.isocalendar()[:2]
        if day not in days and len(days) < keep_daily:
            days.append(day)
            keep.add(name)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.append(week)
            keep.add(name)
    if stamps:
        keep.add(stamps[0][1])
    return keep


def purge(destination, keep_daily: int, keep_weekly: int) -> dict:
    if keep_daily < 1 or keep_weekly < 0:
        raise BackupError("configuration")
    names = destination.names()
    complete = [n for n in names if NAME.fullmatch(n) and n + ".json" in names]
    keep = retained(complete, keep_daily, keep_weekly)
    removed = [n for n in complete if n not in keep]
    for name in removed:
        # Remove the completion marker first so a partial purge never looks complete.
        destination.delete(name + ".json")
        destination.delete(name)
    return {"kept": sorted(keep), "removed": sorted(removed)}


def alert(stage: str) -> None:
    command = os.environ.get("ALVARY_BACKUP_ALERT_COMMAND")
    if not command:
        print("WARNING: no ALVARY_BACKUP_ALERT_COMMAND configured; failure not delivered", file=sys.stderr)
        return
    try:
        subprocess.run([*shlex.split(command), "backup-failed", stage], timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        print("WARNING: backup alert command failed", file=sys.stderr)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise BackupError("configuration")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Encrypted backup, isolated restore verification, retention")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create", help="Dump ALVARY_BACKUP_DATABASE_URL, encrypt and store")
    make.add_argument("--recipient-file", type=Path, required=True, help="Public OpenPGP key only")
    make.add_argument("--destination", type=Path, required=True)
    check = commands.add_parser("restore-verify", help="Restore into ALVARY_RESTORE_DATABASE_URL and verify")
    check.add_argument("backup")
    check.add_argument("--destination", type=Path, required=True)
    check.add_argument(
        "--identity-file", type=Path, required=True, help="Private key, held off the backup host"
    )
    prune = commands.add_parser("purge", help="Apply an explicit retention decision")
    prune.add_argument("--destination", type=Path, required=True)
    prune.add_argument("--keep-daily", type=int, required=True)
    prune.add_argument("--keep-weekly", type=int, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        destination = DirectoryDestination(args.destination)
        tools = Tools.from_environment()
        if args.command == "create":
            database = Database.from_url(required_env("ALVARY_BACKUP_DATABASE_URL"))
            result = create(database, args.recipient_file, destination, tools, datetime.now(UTC))
            result = {k: result[k] for k in ("backup", "created_at", "alembic_revision", "encrypted_sha256")}
        elif args.command == "restore-verify":
            target = Database.from_url(required_env("ALVARY_RESTORE_DATABASE_URL"))
            result = restore_verify(args.backup, destination, args.identity_file, target, tools)
        else:
            result = purge(destination, args.keep_daily, args.keep_weekly)
    except BackupError as error:
        if args.command != "purge":
            alert(error.stage)
        print(json.dumps({"status": "failed", "stage": error.stage}))
        return 1
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
