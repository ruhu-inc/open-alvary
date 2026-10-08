"""Prepare a pinned SQL resource revision from an isolated database at the previous head.

  init REVISION SIGNATURE...  capture each installed definition as prior_<name>.sql and
                              <name>.sql (edit the latter), copying literals/vocabulary pins
  pin REVISION                compile edited files in a rolled-back transaction, require the
                              normalized form, write manifest.json and print RESOURCE_HASH

Never writes to the database: every replacement is rolled back. Requires DATABASE_URL to name
an isolated *_test PostgreSQL database migrated to the revision before REVISION.
"""

import argparse
import json
import os
import re
from hashlib import sha256
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from rights.database import PolicyNamespace
from rights.sql_resources import bundle_hash, context, normalize_body, render

ROOT = Path(__file__).resolve().parents[1] / "rights" / "sql"
SIGNATURE = re.compile(r"[a-z_][a-z0-9_]*\([a-z0-9_, \[\]]*\)")


def digest(value):
    return sha256(value.encode()).hexdigest()


def previous_manifest(revision):
    earlier = sorted(p for p in ROOT.glob("v[0-9][0-9][0-9][0-9]/manifest.json") if p.parent.name < revision)
    if not earlier:
        raise SystemExit("No earlier manifest supplies literal and vocabulary pins")
    return json.loads(earlier[-1].read_text())


def installed(conn, tokens, signature):
    row = conn.execute(
        text("SELECT prosrc,pg_get_functiondef(oid) definition FROM pg_proc WHERE oid=to_regprocedure(:fn)"),
        {"fn": tokens["policy"] + "." + signature},
    ).one_or_none()
    if row is None:
        raise SystemExit(f"Function is not installed: {signature}")
    return normalize_body(row.prosrc, tokens), normalize_body(row.definition, tokens)


def filename(signature):
    return signature.split("(", 1)[0] + ".sql"


def init(conn, ns, revision, signatures):
    folder = ROOT / revision
    if (folder / "manifest.json").exists():
        raise SystemExit("Revision already pinned; applied resources are immutable")
    base = previous_manifest(revision)
    tokens = context(conn, ns, base["literals"])
    folder.mkdir(exist_ok=True)
    draft = {k: base[k] for k in ("vocabulary", "literals", "vocabulary_file", "vocabulary_hash")}
    draft |= {"revision": revision.removeprefix("v"), "functions": []}
    for signature in signatures:
        if not SIGNATURE.fullmatch(signature):
            raise SystemExit(f"Unexpected signature format: {signature}")
        _, definition = installed(conn, tokens, signature)
        for name in ("prior_" + filename(signature), filename(signature)):
            if (folder / name).exists():
                raise SystemExit(f"Refusing to overwrite {folder.name}/{name}")
            (folder / name).write_text(definition)
        draft["functions"].append({"signature": signature})
    (folder / "draft.json").write_text(json.dumps(draft, indent=2) + "\n")
    print(f"Edit rights/sql/{revision}/<name>.sql, then run: pin {revision}")


def pin(conn, ns, revision):
    folder = ROOT / revision
    draft_path = folder / "draft.json"
    if not draft_path.exists():
        raise SystemExit("Run init first; pinned revisions are immutable")
    manifest = json.loads(draft_path.read_text())
    tokens = context(conn, ns, manifest["literals"])
    try:
        conn.exec_driver_sql("SET LOCAL check_function_bodies=on")
        for entry in manifest["functions"]:
            signature = entry["signature"]
            prior_body, prior_definition = installed(conn, tokens, signature)
            prior_file = "prior_" + filename(signature)
            if (folder / prior_file).read_text() != prior_definition:
                raise SystemExit(f"{prior_file} differs from the installed definition")
            source = (folder / filename(signature)).read_text()
            conn.exec_driver_sql(render(source, tokens), execution_options={"no_parameters": True})
            body, definition = installed(conn, tokens, signature)
            if source != definition:
                (folder / filename(signature)).write_text(definition)
                raise SystemExit(f"Rewrote {filename(signature)} in normalized form; review it and rerun pin")
            entry.update(
                file=filename(signature),
                prior_file=prior_file,
                prior_body_hash=digest(prior_body),
                current_body_hash=digest(body),
                prior_definition_hash=digest(prior_definition),
                current_definition_hash=digest(definition),
            )
    finally:
        conn.rollback()
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    draft_path.unlink()
    print(f'RESOURCE_HASH = "{bundle_hash(revision)}"')


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["init", "pin"])
    parser.add_argument("revision", help="Directory name such as v0014")
    parser.add_argument("signatures", nargs="*", help="Installed policy function signatures (init only)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"v\d{4}", args.revision):
        raise SystemExit("Revision must look like v0014")
    if (args.action == "init") != bool(args.signatures):
        raise SystemExit("init needs signatures; pin takes none")
    raw = os.environ.get("DATABASE_URL")
    url = make_url(raw) if raw else None
    if url is None or url.drivername != "postgresql+psycopg" or not (url.database or "").endswith("_test"):
        raise SystemExit("Explicit isolated PostgreSQL _test DATABASE_URL required")
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
            if args.action == "init":
                init(conn, ns, args.revision, args.signatures)
                conn.rollback()
            else:
                pin(conn, ns, args.revision)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
