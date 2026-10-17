"""The API reference's example requests and answers (web/src/docs/data/examples.json): each request
is sent to the real REST v1 routes, in memory, over the Nigerian catalogue (ingestion/catalogue),
the records the live catalogue holds, and the answer is recorded, shortened where a list is long. Change-feed routes
need PostgreSQL, so their requests are listed without answers.

    python -m scripts.docs_examples
"""

import json
from urllib.parse import quote, urlencode

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from accounts.store import TIERS
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
    "resolve_citation": ({"citation": "FOI Act 2011"}, []),
    "list_jurisdictions": ({}, []),
    "get_coverage": ({}, []),
    "list_releases": ({}, []),
    "list_plans": ({}, []),
    "get_quota_policy": ({}, []),
    "list_changes": ({}, [("jurisdiction", "ng"), ("limit", "10")]),
    "list_recent_changes": ({}, [("jurisdiction", "ng")]),
    "get_atom_feed": ({}, [("jurisdiction", "ng")]),
    "get_rss_feed": ({}, [("jurisdiction", "ng")]),
}
RECORDED = {"search", "list_sources", "get_source", "get_versions", "get_rights", "resolve_citation",
            "list_jurisdictions", "get_coverage", "list_releases", "list_plans", "get_quota_policy"}  # fmt: skip


def shorten(operation: str, answer):
    if operation == "list_plans":
        return {"tiers": answer["tiers"][:2]}
    if operation == "list_releases":
        return [
            {**r, "files": dict(list(r["files"].items())[:2])} if isinstance(r.get("files"), dict) else r
            for r in answer
        ]
    return answer


def main() -> None:
    corpus = Corpus.model_validate(json.loads(CATALOGUE.read_text()))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    store = Store(engine)
    store.save(corpus)
    plans = [t for t in json.loads(TIERS.read_text()) if t.get("listing", {}).get("action")]
    spec = json.loads((DATA / "openapi.json").read_text())
    paths = {methods["get"]["operationId"]: path for path, methods in spec["paths"].items()}
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
        examples[operation] = example
    examples["mcp:search_catalogue"] = {"arguments": {"query": "freedom of information", "limit": 3}}
    assert {t["name"] for t in TOOLS} >= {"search_catalogue"}
    (DATA / "examples.json").write_text(json.dumps(examples, indent=2, ensure_ascii=False) + "\n")
    print("Recorded", ", ".join(sorted(k for k, v in examples.items() if "answer" in v)))


if __name__ == "__main__":
    main()
