"""What preparing the collections again would do to the review work already done (owner, 2026-10-30):
before the workbench is switched to a newly prepared copy of its collections, every document a
reviewer has worked on is looked up in the new copy and its work compared, nothing changed:

- a page checked: is its machine reading the same (its check stands), changed (to check again), or no
  longer in the document;
- a document no longer in the new copy (renamed, split or merged by a corrected reader): the documents
  of the same source and language that hold its pages now, where its reviewer would continue;
- an approval: does it still cover the document exactly in the new copy, or lapse;
- details saved for a work: does the work still exist.

python -m workbench.admin impact <place>[:<place>…] (ALVARY_REVIEW_COLLECTIONS for the new copy, the
current one from the environment) prints the summary and a line per document affected.
"""

import os

from sqlalchemy import select

from workbench import content
from workbench.review import Review
from workbench.store import approval, detail_revision, page_revision


def reviewed(store) -> dict[str, set[str]]:
    """Every document (or work) with review work saved: pages, details, approvals."""
    with store.engine.connect() as db:
        return {
            "pages": {row.act_id for row in db.execute(select(page_revision.c.act_id).distinct())},
            "details": {row.act_id for row in db.execute(select(detail_revision.c.act_id).distinct())},
            "approvals": {row.act_id for row in db.execute(select(approval.c.act_id).distinct())},
        }


def successors(old: dict, new_library: content.Library, pages: set[int]) -> list[str]:
    """The new copy's documents of the same source and language that hold any of these pages, or of
    the same language that list the old source among their other sources (the same instrument found in
    another file, its pages numbered there)."""
    out = []
    for document in new_library.documents():
        if document.get("language") != old.get("language"):
            continue
        if document.get("url") == old.get("url"):
            span = document.get("pages") or []
            if len(span) == 2 and any(span[0] <= page <= span[1] for page in pages):
                out.append(document["id"])
        elif old.get("url") and any(s.get("url") == old["url"] for s in document.get("other_sources") or []):
            out.append(document["id"])
    return sorted(out)


def report(store, current: content.Library, new: content.Library, profiles) -> dict:
    work = reviewed(store)
    review = Review(store, new, profiles)
    out: dict = {
        "documents_with_pages": 0,
        "pages_unchanged": 0,
        "pages_changed": 0,
        "pages_gone": 0,
        "documents_gone": [],
        "approvals_kept": [],
        "approvals_lapsing": [],
        "details_orphaned": [],
        "changed": [],
    }
    for act_id in sorted(work["pages"]):
        latest = store.latest_pages(act_id)
        if not new.has(act_id):
            old = current.listed(act_id) if current.has(act_id) else {"id": act_id}
            out["documents_gone"].append(
                {"id": act_id, "pages_checked": sorted(latest), "now_in": successors(old, new, set(latest))}
            )
            continue
        out["documents_with_pages"] += 1
        pages = {page["page"]: page for page in new.reading(act_id)["pages"]}
        changed, gone = [], []
        for number, revision in latest.items():
            page = pages.get(number)
            if page is None:
                gone.append(number)
            elif content.reading_sha256(page) != revision["reading_sha256"]:
                changed.append(number)
        out["pages_unchanged"] += len(latest) - len(changed) - len(gone)
        out["pages_changed"] += len(changed)
        out["pages_gone"] += len(gone)
        if changed or gone:
            out["changed"].append(
                {"id": act_id, "pages_to_check_again": changed, "pages_no_longer_here": gone}
            )
    for act_id in sorted(work["approvals"]):
        if not new.has(act_id):
            out["approvals_lapsing"].append({"id": act_id, "why": "the document is not in the new copy"})
            continue
        state = review.state(new.act(act_id))
        (out["approvals_kept"] if state.get("approved") else out["approvals_lapsing"]).append(
            {"id": act_id, **({} if state.get("approved") else {"why": "what it approved has changed"})}
        )
    works = {d.get("work") or d["id"] for d in new.documents()} | {d["id"] for d in new.documents()}
    out["details_orphaned"] = sorted(key for key in work["details"] if key not in works)
    out["summary"] = {
        "documents with checked pages": out["documents_with_pages"] + len(out["documents_gone"]),
        "pages unchanged (their check stands)": out["pages_unchanged"],
        "pages changed (check again)": out["pages_changed"],
        "pages no longer in their document": out["pages_gone"],
        "documents no longer in the new copy": len(out["documents_gone"]),
        "approvals kept": len(out["approvals_kept"]),
        "approvals lapsing": len(out["approvals_lapsing"]),
        "details whose work is gone": len(out["details_orphaned"]),
    }
    return out


def libraries(places: str) -> tuple[content.Library, content.Library]:
    """The library as configured now, and one over the new copy's places."""
    current = content.Library()
    saved = os.environ.get("ALVARY_REVIEW_COLLECTIONS")
    os.environ["ALVARY_REVIEW_COLLECTIONS"] = places
    try:
        return current, content.Library()
    finally:
        if saved is None:
            os.environ.pop("ALVARY_REVIEW_COLLECTIONS", None)
        else:
            os.environ["ALVARY_REVIEW_COLLECTIONS"] = saved
