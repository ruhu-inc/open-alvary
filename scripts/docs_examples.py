"""The API reference's example requests and answers (web/src/docs/data/examples.json): each request
is sent to the real REST v1 routes, in memory, over the Nigerian catalogue (ingestion/catalogue),
the records the live catalogue holds, and the answer is recorded, shortened where a list is long.

The change feed needs PostgreSQL. Given a migrated, seeded scratch database, the script makes one
illustrative correction there and records the feed's answers, labelled as examples; never give it a
real database. Without one, the feed's answers recorded before are kept.

    python -m scripts.docs_examples [--changes postgresql+psycopg://.../scratch]
"""

import json
import os
import sys
from urllib.parse import quote, urlencode

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from accounts.store import listed_plans
from api.mcp import TOOLS
from api.store import ROOT, Store, metadata
from api.v1 import create_v1_app
from schema.models import Corpus
from scripts.docs_reference import DATA

CATALOGUE = ROOT / "ingestion" / "catalogue" / "ng.json"

# operation ID → (path values, query). Real records, so every example works against the live API.
REQUESTS: dict[str, tuple[dict[str, str], list[tuple[str, str]]]] = {
    "search": ({}, [("q", "freedom of information"), ("jurisdiction", "ng"), ("limit", "5")]),
    "list_sources": ({}, [("jurisdiction", "ng"), ("limit", "2")]),
    "get_source": ({"source_id": "ng-foi-act-2011"}, []),
    "get_versions": ({"source_id": "ng-foi-act-2011"}, []),
    "get_rights": ({"source_id": "ng-foi-act-2011"}, []),
    "get_relationships": ({"source_id": "ng-isa-2007"}, []),
    "resolve_citation": ({"citation": "FOI Act 2011"}, []),
    "list_jurisdictions": ({}, []),
    "get_coverage": ({}, []),
    "list_releases": ({}, []),
    "get_bulk_file": (
        {"release_id": "2026-10-05", "jurisdiction": "ng", "mode": "corpus", "filename": "manifest.json"},
        [],
    ),
    "list_plans": ({}, []),
    "get_quota_policy": ({}, []),
    "list_changes": ({}, [("jurisdiction", "ng"), ("limit", "10")]),
    "list_recent_changes": ({}, [("jurisdiction", "ng")]),
    "get_atom_feed": ({}, [("jurisdiction", "ng")]),
    "get_rss_feed": ({}, [("jurisdiction", "ng")]),
}
RECORDED = {"search", "list_sources", "get_source", "get_versions", "get_rights", "get_relationships",
            "resolve_citation", "list_jurisdictions", "get_coverage", "list_releases", "list_plans",
            "get_quota_policy"}  # fmt: skip


def shorten(operation: str, answer):
    if operation == "list_plans":
        return {"tiers": answer["tiers"][:2]}
    if operation == "list_releases":
        return [
            {**r, "files": dict(list(r["files"].items())[:2])} if isinstance(r.get("files"), dict) else r
            for r in answer
        ]
    return answer


CHANGE_NOTE = (
    "An example: one catalogue correction, recorded in a scratch copy of the catalogue. Your answers "
    "carry the changes recorded since you started following."
)


def short_token(value):
    return value[:20] + "…" if isinstance(value, str) and len(value) > 24 else value


def change_examples(url: str) -> dict[str, dict]:
    """The feed's answers after one correction, in a scratch PostgreSQL copy (the name must say so)."""
    assert "scratch" in url or "example" in url or "guide" in url, "a scratch database only"
    os.environ |= {"PUBLIC_DOMAIN": "open.alvary.ai", "ALVARY_ACCOUNTS": "1"}
    os.environ.setdefault("ALVARY_API_ORIGIN", "https://api.open.alvary.ai")
    engine = create_engine(url)
    store = Store(engine)
    corpus = store.load()
    record = next(s for s in corpus.sources if s.id == "ng-foi-act-2011")
    summary = (record.summary or "").replace("Gives everyone", "Gives every person")
    if summary == record.summary:
        summary = summary.replace("Gives every person", "Gives everyone")
    corpus.sources[corpus.sources.index(record)] = record.model_copy(update={"summary": summary})
    store.save(corpus)
    client = TestClient(create_v1_app(store, ROOT / "releases", lambda: []))
    page = client.get("/changes?jurisdiction=ng&limit=10")
    page.raise_for_status()
    events = page.json()["events"][-1:]
    answer = page.json() | {"events": events}
    for key in ("resume_token", "next_cursor"):
        if key in answer:
            answer[key] = short_token(answer[key])
    recent = client.get("/changes/recent?jurisdiction=ng").json()
    coverage = client.get("/change-coverage").json()
    engine.dispose()
    return {
        "list_changes": {"answer": answer, "note": CHANGE_NOTE},
        "get_change": {"answer": events[0], "note": CHANGE_NOTE},
        "list_recent_changes": {"answer": {"events": recent["events"][:1]}, "note": CHANGE_NOTE},
        "get_change_coverage": {"answer": coverage},
    }


RELATIONS_NOTE = (
    "An example, with the repeal marked as checked by a reviewer: a link is listed once a reviewer has "
    "checked it against the official text."
)


def main() -> None:
    corpus = Corpus.model_validate(json.loads(CATALOGUE.read_text()))
    # The history example shows a checked link: a link is public only once a reviewer checks it.
    unchecked = any(not a.verified for a in corpus.amendments)
    corpus.amendments = [a.model_copy(update={"verified": True}) for a in corpus.amendments]
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    store = Store(engine)
    store.save(corpus)
    plans = listed_plans()
    spec = json.loads((DATA / "openapi.json").read_text())
    paths = {next(iter(methods.values()))["operationId"]: path for path, methods in spec["paths"].items()}
    client = TestClient(create_v1_app(store, ROOT / "releases", lambda: plans))
    examples: dict[str, dict] = {}
    for operation, (values, query) in REQUESTS.items():
        example: dict = {"path": values, "query": [list(pair) for pair in query]}
        if operation in RECORDED:
            path = paths[operation]
            for name, value in values.items():
                path = path.replace("{" + name + "}", quote(value, safe=""))
            response = client.get(path + ("?" + urlencode(query) if query else ""))
            response.raise_for_status()
            example["answer"] = shorten(operation, response.json())
            if operation == "get_relationships" and unchecked:
                example["note"] = RELATIONS_NOTE
        examples[operation] = example
    if "--changes" in sys.argv:
        for operation, recorded in change_examples(sys.argv[sys.argv.index("--changes") + 1]).items():
            examples[operation] = examples.get(operation, {"path": {}, "query": []}) | recorded
    elif (DATA / "examples.json").exists():  # the feed's answers recorded before are kept
        before = json.loads((DATA / "examples.json").read_text())
        for operation, recorded in before.items():
            if operation in paths and operation not in RECORDED and "answer" in recorded:
                examples[operation] = recorded
    examples["mcp:search_catalogue"] = {"arguments": {"query": "freedom of information", "limit": 3}}
    assert {t["name"] for t in TOOLS} >= {"search_catalogue"}
    (DATA / "examples.json").write_text(json.dumps(examples, indent=2, ensure_ascii=False) + "\n")
    print("Recorded", ", ".join(sorted(k for k, v in examples.items() if "answer" in v)))


if __name__ == "__main__":
    main()
