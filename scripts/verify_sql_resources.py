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
    for revision in ("0011", "0012", "0013"):
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
    # Later immutable bundles override only the reviewed signatures they replace.
    installed = {}
    for revision in ("v0011", "v0013"):
        root = files("rights").joinpath("sql", revision)
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
    print(
        f"PASS: three immutable SQL resource bundles; {len(installed)} installed definitions, attributes and bodies match"
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
