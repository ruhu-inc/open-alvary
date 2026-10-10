"""Encrypted PostgreSQL backups, isolated restore verification and explicit retention.

Backups are encrypted with OpenPGP public-key encryption (``gpg --recipient-file``), so the
backup host never holds a decryption key. Plaintext dumps exist only in a private temporary
directory and are removed before storage. PostgreSQL tools are used through stdin/stdout
only, so ``ALVARY_PG_TOOL_PREFIX`` (for example ``docker exec -i -e PGPASSWORD db``) can
supply a server-matched toolchain. Passwords travel through ``PGPASSWORD``, never argv.

Destination, recipient key, retention counts and alert command are operator decisions;
nothing here supplies a default for them. Metadata records the delivered denial-journal
checkpoint; restored policy stays closed until journal replay and reconciliation.
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
        owns_partial = False
        try:
            with source.open("rb") as reader, partial.open("xb") as writer:
                owns_partial = True
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            # Publish exclusively: a stale exists() check must never let a second
            # writer replace an archive that finished while it was copying.
            os.link(partial, target)
            partial.unlink()
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            if owns_partial:
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


class RcloneDestination:
    """Object storage through rclone (Cloudflare R2 by default deployment).

    Credentials come only from rclone's environment configuration (RCLONE_CONFIG_<REMOTE>_*),
    never argv or files written here. Uploads never overwrite: an existing name is refused
    before upload and ``--immutable`` makes rclone refuse a concurrent different object.
    """

    REMOTE = re.compile(r"[A-Za-z0-9_-]+:[A-Za-z0-9._/-]+\Z")

    def __init__(self, remote: str, binary: str = "rclone"):
        if not self.REMOTE.fullmatch(remote) or ".." in remote:
            raise BackupError("destination")
        self.remote = remote.rstrip("/")
        self.binary = binary

    def _run(self, *args, empty_on_missing=False) -> bytes:
        try:
            completed = subprocess.run([self.binary, *args], capture_output=True, check=False, timeout=3600)
        except (OSError, subprocess.SubprocessError):
            raise BackupError("destination") from None
        if empty_on_missing and completed.returncode == 3:
            return b""  # rclone: directory not found, i.e. nothing stored under this prefix yet
        if completed.returncode != 0:
            raise BackupError("destination")
        return completed.stdout

    def _object(self, name: str) -> str:
        if "/" in name or name.startswith(".") or not name:
            raise BackupError("destination")
        return f"{self.remote}/{name}"

    def put(self, name: str, source: Path) -> None:
        if name in self.names():
            raise BackupError("destination")
        self._run("copyto", "--immutable", "--s3-no-check-bucket", str(source), self._object(name))

    def get(self, name: str, target: Path) -> None:
        self._run("copyto", self._object(name), str(target))

    def names(self) -> list[str]:
        listing = self._run("lsf", "--files-only", self.remote, empty_on_missing=True).decode()
        return sorted(line for line in listing.splitlines() if line and not line.startswith("."))

    def delete(self, name: str) -> None:
        self._run("deletefile", self._object(name))


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


REVISION_SQL = 'SELECT version_num FROM :"base".alembic_version;'
JOURNAL_PRESENT_SQL = "SELECT to_regclass(format('%I.denial_delivery', :'policy')) IS NOT NULL;"
# Read before the dump starts, so it is a lower bound: recovery replays every journal event
# after this sequence, and replay is idempotent for events the dump already contains.
CHECKPOINT_SQL = """
SELECT coalesce(max(journal_sequence),0), coalesce((SELECT journal_chain FROM :"policy".denial_delivery
    WHERE journal_sequence IS NOT NULL ORDER BY journal_sequence DESC LIMIT 1),''),
    count(*) FILTER (WHERE durability='pending')
FROM :"policy".denial_delivery;
"""


def schemas(base: str) -> dict:
    """Same naming rule as rights.database.PolicyNamespace, without importing it."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", base):
        raise BackupError("configuration")
    return {"base": base, "policy": f"{base}_policy" if base.startswith("test_") else "policy"}


def checkpoint(tools, database, base):
    names = schemas(base)
    if tools.query("dump", database, JOURNAL_PRESENT_SQL, names) == [["f"]]:
        return None
    rows = tools.query("dump", database, CHECKPOINT_SQL, names)
    if len(rows) != 1:
        raise BackupError("dump")
    sequence, chain, pending = rows[0]
    return {
        "journal_sequence": int(sequence),
        "journal_chain": chain or None,
        "pending_denials": int(pending),
    }


def revision(tools, stage, database, base) -> str:
    rows = tools.query(stage, database, REVISION_SQL, schemas(base))
    if len(rows) != 1:
        raise BackupError(stage)
    return rows[0][0]


def create(
    database, recipient_file: Path, destination, tools, now: datetime, base="public", kind="policy"
) -> dict:
    """kind 'review' backs up the review workbench database: no migration revision or journal."""
    if kind not in {"policy", "review"}:
        raise BackupError("configuration")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"open-alvary-{stamp}.dump.gpg"
    with tempfile.TemporaryDirectory(prefix="oa-backup-") as tmp, gpg_home() as home:
        work = Path(tmp)
        os.chmod(work, 0o700)
        fingerprints = recipient_fingerprints(home, recipient_file)
        before = revision(tools, "dump", database, base) if kind == "policy" else None
        journal = checkpoint(tools, database, base) if kind == "policy" else None
        plain = work / "database.dump"
        with plain.open("xb") as writer:
            tools.run(
                "dump",
                ["pg_dump", "--format=custom", "--no-password", "--dbname", database.dsn],
                database,
                stdout=writer,
            )
        if kind == "policy" and revision(tools, "dump", database, base) != before:
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
            "kind": kind,
            "backup": name,
            "created_at": now.astimezone(UTC).isoformat(),
            "alembic_revision": before,
            "dump_sha256": dump_hash,
            "dump_bytes": dump_bytes,
            "encrypted_sha256": sha256(encrypted),
            "encryption": {"method": "openpgp-public-key", "recipients": fingerprints},
            "tables": tables,
            # Delivered-journal lower bound (None before denial delivery exists). Recovery must
            # still replay the journal and reconcile pending denials before any serving epoch.
            "journal_checkpoint": journal,
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


def restore_verify(name, destination, identity_file: Path, target, tools, base="public") -> dict:
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
    if (
        metadata.get("kind", "policy") == "policy"
        and revision(tools, "verify", target, base) != metadata["alembic_revision"]
    ):
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
        "kind": metadata.get("kind", "policy"),
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


def alert(event: str, stage: str) -> None:
    command = os.environ.get("ALVARY_BACKUP_ALERT_COMMAND")
    if not command:
        print("WARNING: no ALVARY_BACKUP_ALERT_COMMAND configured; failure not delivered", file=sys.stderr)
        return
    try:
        subprocess.run([*shlex.split(command), event, stage], timeout=30, check=False)
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
    make_where = make.add_mutually_exclusive_group(required=True)
    make_where.add_argument("--destination", type=Path, help="Mounted directory")
    make_where.add_argument("--remote", help="rclone remote such as r2:bucket/prefix")
    check = commands.add_parser("restore-verify", help="Restore into ALVARY_RESTORE_DATABASE_URL and verify")
    check.add_argument("backup")
    check_where = check.add_mutually_exclusive_group(required=True)
    check_where.add_argument("--destination", type=Path, help="Mounted directory")
    check_where.add_argument("--remote", help="rclone remote such as r2:bucket/prefix")
    check.add_argument(
        "--identity-file", type=Path, required=True, help="Private key, held off the backup host"
    )
    prune = commands.add_parser("purge", help="Apply an explicit retention decision")
    prune_where = prune.add_mutually_exclusive_group(required=True)
    prune_where.add_argument("--destination", type=Path, help="Mounted directory")
    prune_where.add_argument("--remote", help="rclone remote such as r2:bucket/prefix")
    prune.add_argument("--keep-daily", type=int, required=True)
    prune.add_argument("--keep-weekly", type=int, required=True)
    for command in (make, check):
        command.add_argument("--schema", default="public", help="Base schema of the application tables")
    make.add_argument(
        "--kind",
        choices=["policy", "review"],
        default="policy",
        help="review: the review workbench database (store it under its own remote prefix)",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        destination = (
            RcloneDestination(args.remote) if args.remote else DirectoryDestination(args.destination)
        )
        tools = Tools.from_environment()
        if args.command == "create":
            database = Database.from_url(required_env("ALVARY_BACKUP_DATABASE_URL"))
            result = create(
                database, args.recipient_file, destination, tools, datetime.now(UTC), args.schema, args.kind
            )
            result = {
                k: result[k] for k in ("backup", "kind", "created_at", "alembic_revision", "encrypted_sha256")
            }
        elif args.command == "restore-verify":
            target = Database.from_url(required_env("ALVARY_RESTORE_DATABASE_URL"))
            result = restore_verify(args.backup, destination, args.identity_file, target, tools, args.schema)
        else:
            result = purge(destination, args.keep_daily, args.keep_weekly)
    except BackupError as error:
        stage = error.stage
    except Exception:
        stage = "unexpected"  # never render the exception: it may contain paths or parameters
    else:
        print(json.dumps({"status": "ok", **result}, sort_keys=True))
        return 0
    alert(f"{args.command}-failed", stage)
    print(json.dumps({"status": "failed", "stage": stage}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
