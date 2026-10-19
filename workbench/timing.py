"""How long review takes: minutes per page and per document, from when reviewers saved pages.

    python -m workbench.timing [--prefix rw-] [--gap 20] [--by-route]

Each reviewer's saves are put in time order; the time between one save and the next is the time
spent on the next page, unless it is longer than --gap minutes (a break, or the end of a session),
in which case that page is left out of the timing rather than counted as one long page. Only pages
saved as checked count. With --by-route, minutes per page are also given for each confidence route
(CPR-01, workbench.routing), from the documents' readings: the review time that routing could save.
Nothing is written; this reads the review database named by ALVARY_REVIEW_DATABASE_URL.
"""

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

from sqlalchemy import select

from workbench.store import Store, connect, page_revision


def timings(rows: list[dict], gap_minutes: float = 20.0, routes: dict | None = None) -> dict:
    """Minutes spent per checked page, and per document, from page saves; with ``routes``
    ((document, page) → route), per route too."""
    by_route: dict[str, list[float]] = defaultdict(list)
    by_author: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_author[row["author"]].append(row)
    per_page: list[float] = []
    per_document: dict[str, float] = defaultdict(float)
    pages_by_document: dict[str, set] = defaultdict(set)
    for saves in by_author.values():
        saves.sort(key=lambda r: r["created_at"])
        for before, after in zip(saves, saves[1:], strict=False):
            minutes = (after["created_at"] - before["created_at"]).total_seconds() / 60
            if after["status"] != "checked" or minutes > gap_minutes or minutes <= 0:
                continue
            per_page.append(minutes)
            if routes is not None:
                by_route[routes.get((after["act_id"], after["page"]), "unrouted")].append(minutes)
            per_document[after["act_id"]] += minutes
            pages_by_document[after["act_id"]].add(after["page"])
    found = {
        "pages_timed": len(per_page),
        "minutes_per_page": {
            "median": round(statistics.median(per_page), 2) if per_page else None,
            "mean": round(statistics.mean(per_page), 2) if per_page else None,
        },
        "documents": {
            act: {"pages_timed": len(pages_by_document[act]), "minutes": round(total, 1)}
            for act, total in sorted(per_document.items())
        },
    }
    if routes is not None:
        found["by_route"] = {
            route: {"pages_timed": len(values), "median": round(statistics.median(values), 2),
                    "minutes": round(sum(values), 1)}
            for route, values in sorted(by_route.items())
        }  # fmt: skip
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m workbench.timing", description=__doc__)
    parser.add_argument("--prefix", default="", help="only documents whose id starts so, e.g. rw-")
    parser.add_argument("--gap", type=float, default=20.0, help="minutes beyond which a pause is a break")
    parser.add_argument("--by-route", action="store_true", help="also give minutes per confidence route")
    args = parser.parse_args(argv)
    store = Store(connect(os.environ["ALVARY_REVIEW_DATABASE_URL"]))
    with store.engine.connect() as db:
        rows = [
            dict(r._mapping)
            for r in db.execute(
                select(
                    page_revision.c.act_id,
                    page_revision.c.page,
                    page_revision.c.status,
                    page_revision.c.author,
                    page_revision.c.created_at,
                ).where(page_revision.c.act_id.startswith(args.prefix))
            )
        ]

    routes = None
    if args.by_route:
        from workbench import content, routing

        library, routes = content.Library(), {}
        for act in sorted({r["act_id"] for r in rows}):
            try:
                found = routing.routes(library.reading(act))
            except (FileNotFoundError, KeyError):
                continue
            routes |= {(act, page): name for page, (name, _) in found.items()}
    print(json.dumps(timings(rows, args.gap, routes), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
