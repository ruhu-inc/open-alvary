"""One database inventory selects complete, declarative capability grant bundles."""

import json
from importlib.resources import files

from sqlalchemy import text

from rights.sql_resources import render


def selected_grants(conn, namespace):
    names = [namespace.base, *(namespace.schema(k) for k in ("policy", "staging", "corpus"))]
    rows = conn.execute(
        text("""
        SELECT n.nspname,c.relname,NULL::text signature FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(:names)
        UNION ALL SELECT n.nspname,p.proname,p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(:names)
    """),
        {"names": names},
    ).all()
    kinds = dict(zip(names, ("base", "policy", "staging", "corpus"), strict=True))
    inventory = {kinds[schema] + "." + name for schema, name, _ in rows}
    root = files("rights").joinpath("sql", "provision")
    bundles = json.loads(root.joinpath("manifest.json").read_text())
    selected = [b for b in bundles if b["anchor"] is None or b["anchor"] in inventory]
    if any(set(b["requires"]) - inventory for b in selected):
        raise ValueError("Incomplete installed database capability; inspect before provisioning")
    quote = conn.dialect.identifier_preparer.quote_identifier
    tokens = {
        "base": quote(namespace.base),
        **{k: quote(namespace.schema(k)) for k in ("policy", "staging", "corpus")},
        **{
            "role_" + k: quote(namespace.role(k))
            for k in ("guard", "serving", "acquisition", "review", "release")
        },
    }
    grants = [render(root.joinpath(b["file"]).read_text(), tokens) for b in selected]
    functions = [
        signature for schema, _, signature in rows if schema == namespace.schema("policy") and signature
    ]
    return grants, functions
