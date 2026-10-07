"""Verify reviewed SQL revision pins and installed definitions without reading corpus rows."""

import ast
import json
import os
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from rights.database import PolicyNamespace
from rights.sql_resources import bundle_hash, context, normalize_body


def verify(conn):
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    for revision in ("0011", "0012"):
        migration = next(Path("migrations/versions").glob(revision + "*.py"))
        tree = ast.parse(migration.read_text())
        pin = next(
            ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RESOURCE_HASH" for t in n.targets)
        )
        if bundle_hash("v" + revision) != pin:
            raise ValueError("SQL revision resource pin differs")
    root = files("rights").joinpath("sql/v0011")
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    vocabulary = json.loads(files("schema").joinpath(manifest["vocabulary_file"]).read_text())
    tokens = context(conn, ns, manifest["literals"], vocabulary)
    for entry in manifest["functions"]:
        definition = conn.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
            {"signature": tokens["policy"] + "." + entry["signature"]},
        )
        if (
            definition is None
            or sha256(normalize_body(definition, tokens).encode()).hexdigest()
            != entry["current_definition_hash"]
        ):
            raise ValueError("Installed SQL definition differs from reviewed revision")
    print(
        f"PASS: two immutable SQL resource bundles; {len(manifest['functions'])} installed definitions, attributes and bodies match"
    )


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
