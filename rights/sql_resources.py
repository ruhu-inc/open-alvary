"""Closed SQL tokens, PostgreSQL literal quoting, and immutable revision bundles."""

import json
import re
from hashlib import sha256
from importlib.resources import files

from sqlalchemy import text

from rights.authority import assert_guard_boundary
from schema.vocabularies import VOCABULARY

TOKEN = re.compile(r"\{\{([a-z0-9_]+)\}\}")


def bundle_hash(revision):
    root = files("rights").joinpath("sql", revision)
    digest = sha256()
    for resource in sorted(root.iterdir(), key=lambda p: p.name):
        if resource.name.endswith((".sql", ".json")):
            digest.update(resource.name.encode() + b"\0" + resource.read_bytes() + b"\0")
    return digest.hexdigest()


def context(conn, namespace, literals=None, vocabulary=VOCABULARY):
    quote = conn.dialect.identifier_preparer.quote_identifier
    names = {"base": namespace.base, **{k: namespace.schema(k) for k in ("policy", "staging", "corpus")}}
    identifiers = {k: quote(v) for k, v in names.items()}
    identifiers.update(
        {
            "role_" + k: quote(namespace.role(k))
            for k in ("guard", "serving", "acquisition", "review", "release")
        }
    )
    values = {k + "_literal": v for k, v in names.items()}
    prefix = namespace.role("guard").removesuffix("_guard")
    values.update({k: prefix + suffix for k, suffix in (literals or {}).items()})
    for group in ("permissions", "materials"):
        values.update({"vocabulary_" + word: word for word in vocabulary[group]})
    # SQL literal quoting belongs to PostgreSQL, not Python repr or string escaping.
    quoted = dict(
        conn.execute(
            text("SELECT key,quote_literal(value) FROM jsonb_each_text(CAST(:values AS jsonb))"),
            {"values": json.dumps(values)},
        ).all()
    )
    return identifiers | quoted


def render(source, tokens):
    requested = set(TOKEN.findall(source))
    if requested - tokens.keys():
        raise ValueError("Unknown SQL resource token")
    result = TOKEN.sub(lambda m: tokens[m[1]], source)
    if "{{" in result or "}}" in result:
        raise ValueError("Invalid SQL resource token")
    return result


def normalize_body(body, tokens):
    # Normalize qualified identifiers (including pg_get_functiondef's unquoted headers)
    # and complete SQL literals. No input-derived values enter function definitions.
    for key in ("policy", "staging", "corpus", "base"):
        name = tokens[key]
        body = body.replace(name + ".", "{{" + key + "}}.")
        bare = name[1:-1].replace('""', '"')
        body = body.replace(bare + ".", "{{" + key + "}}.")
    for key, value in sorted(tokens.items(), key=lambda item: len(item[1]), reverse=True):
        if key.endswith("_literal") or key.startswith(("role_literal_", "vocabulary_")):
            body = body.replace(value, "{{" + key + "}}")
    return body


def install(conn, namespace, revision, expected_hash, *, downgrade=False):
    if bundle_hash(revision) != expected_hash:
        raise RuntimeError("Applied SQL revision resources changed; use a new revision")
    root = files("rights").joinpath("sql", revision)
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    vocabulary_bytes = files("schema").joinpath(manifest["vocabulary_file"]).read_bytes()
    vocabulary = json.loads(vocabulary_bytes)
    if (
        sha256(vocabulary_bytes).hexdigest() != manifest["vocabulary_hash"]
        or manifest["vocabulary"] != vocabulary["version"]
    ):
        raise RuntimeError("Pinned SQL vocabulary differs")
    tokens = context(conn, namespace, manifest["literals"], vocabulary)
    assert_guard_boundary(conn, namespace)
    policy = tokens["policy"]
    guard = conn.scalar(
        text("SELECT oid FROM pg_roles WHERE rolname=:name"), {"name": namespace.role("guard")}
    )
    # Check every body before replacing anything; unknown edits require a reviewed repair.
    for entry in manifest["functions"]:
        row = conn.execute(
            text(
                "SELECT oid,prosrc,proowner,proacl,pg_get_functiondef(oid) definition FROM pg_proc WHERE oid=to_regprocedure(:signature)"
            ),
            {"signature": policy + "." + entry["signature"]},
        ).one_or_none()
        expected = entry["current_body_hash"] if downgrade else entry["prior_body_hash"]
        definition_expected = (
            entry["current_definition_hash"] if downgrade else entry["prior_definition_hash"]
        )
        if (
            row is None
            or sha256(normalize_body(row.prosrc, tokens).encode()).hexdigest() != expected
            or sha256(normalize_body(row.definition, tokens).encode()).hexdigest() != definition_expected
            or (guard is not None and row.proowner != guard)
        ):
            raise RuntimeError("Installed SQL authority requires boundary review")
    identities = {
        entry["signature"]: conn.execute(
            text("SELECT oid,proowner,proacl FROM pg_proc WHERE oid=to_regprocedure(:signature)"),
            {"signature": policy + "." + entry["signature"]},
        ).one()
        for entry in manifest["functions"]
    }
    conn.exec_driver_sql("SET LOCAL check_function_bodies=on")
    for entry in manifest["functions"]:
        filename = entry.get("prior_file", entry["file"]) if downgrade else entry["file"]
        conn.exec_driver_sql(
            render(root.joinpath(filename).read_text(), tokens), execution_options={"no_parameters": True}
        )

    for entry in manifest["functions"]:
        row = conn.execute(
            text(
                "SELECT oid,proowner,proacl,pg_get_functiondef(oid) definition FROM pg_proc WHERE oid=to_regprocedure(:signature)"
            ),
            {"signature": policy + "." + entry["signature"]},
        ).one()
        expected = entry["prior_definition_hash"] if downgrade else entry["current_definition_hash"]
        if (
            tuple(row[:3]) != tuple(identities[entry["signature"]])
            or sha256(normalize_body(row.definition, tokens).encode()).hexdigest() != expected
        ):
            raise RuntimeError("SQL resource replacement changed authority or identity")
