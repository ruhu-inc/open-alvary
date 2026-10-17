"""The developer documentation's API reference data: REST v1's OpenAPI document, the MCP tools and the
plans, copied from the code into web/src/docs/data so the reference pages are built from what the API
actually serves. Run after changing a route, a response model or a tool; the tests fail until the
copies match.

    python -m scripts.docs_reference          write the copies
    python -m scripts.docs_reference --check  exit 1 if they differ
"""

import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine

from accounts.store import TIERS
from api.mcp import TOOLS
from api.store import ROOT, Store
from api.v1 import create_v1_app

DATA = ROOT / "web" / "src" / "docs" / "data"


def documents() -> dict[str, str]:
    # The document is generated from the routes alone: the engine is never connected (PostgreSQL,
    # so the change routes are included as in production).
    engine = create_engine("postgresql+psycopg://reference@127.0.0.1:1/none")
    with tempfile.TemporaryDirectory() as releases:
        spec = create_v1_app(Store(engine), Path(releases), lambda: []).openapi()
    engine.dispose()
    return {
        "openapi.json": json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
        "mcp-tools.json": json.dumps(TOOLS, indent=2, ensure_ascii=False) + "\n",
        # The plans the console lists (those with a listing action), as /tiers answers them.
        "plans.json": json.dumps(
            [t for t in json.loads(TIERS.read_text()) if t.get("listing", {}).get("action")],
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    }


def main(argv: list[str]) -> int:
    stale = [
        name
        for name, text in documents().items()
        if not (DATA / name).is_file() or (DATA / name).read_text() != text
    ]
    if "--check" in argv:
        for name in stale:
            print(f"web/src/docs/data/{name} is out of date: run python -m scripts.docs_reference")
        return 1 if stale else 0
    DATA.mkdir(parents=True, exist_ok=True)
    for name, text in documents().items():
        (DATA / name).write_text(text)
    print("Wrote", ", ".join(sorted(documents())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
