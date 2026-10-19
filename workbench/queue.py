"""The work queue (CPR-06): the work waiting for people, across documents, as tasks.

  review        pages to review whole against the image: under a routing decision those routed
                ``full``, those sampled for the audit (one list with the full ones: the audit is
                reviewed blind, CPR-01), every page after an escalation, and pages marked as needing
                attention or whose reading changed; without the decision, every page not yet checked
  flags         pages whose flags are to be resolved
  events        legal events proposed and waiting for an approver's review (CPR-02)
  instructions  amendment instructions waiting for an editor's review, or a second editor's (CPR-03)

A task is one document's work of one kind and priority: pages with an open high flag first, then
pages to review whole, then flags to resolve, then proposals. Work in documents already started comes
before documents nobody has opened; within each, the longest waiting first. A task waits from its
pages' latest revisions (a page never saved: from when its document's review began) or its proposals'.
"""

from datetime import UTC, datetime

from workbench.editorial import histories
from workbench.review import to_do

PRIORITIES = ("high", "full", "flags", "proposal")
WAITING = {  # the statuses a proposal waits in, and for what
    "events": ("event_id", {"proposed": "review"}),
    "instructions": ("instruction_id", {"proposed": "review", "reviewed": "second_review"}),
}


def aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)  # SQLite keeps no time zone


def task(document: dict, kind: str, priority: str, count: int, since, **values) -> dict:
    return {
        "id": document["id"],
        "title": document["title"],
        "kind": kind,
        "priority": priority,
        "count": count,
        "since": since,
        "started": True,
        "pages": None,
        "step": None,
        **values,
    }


def page_tasks(
    document: dict, readiness: dict, latest: dict[int, dict], began: datetime | None
) -> list[dict]:
    """One document's pages still to do (workbench.review.to_do), a task per kind and priority."""
    high = {flag["page"] for flag in readiness["open_high_flags"]}
    groups: dict[tuple[str, str], list[int]] = {}
    for page, kind in sorted(to_do(readiness).items()):
        priority = "high" if page in high else "full" if kind == "review" else "flags"
        groups.setdefault((kind, priority), []).append(page)
    tasks = []
    for (kind, priority), pages in sorted(groups.items(), key=lambda g: PRIORITIES.index(g[0][1])):
        times = [aware(t) for t in (latest[p]["created_at"] if p in latest else began for p in pages) if t]
        tasks.append(task(document, kind, priority, len(pages), min(times, default=None), pages=pages))
    return tasks


def not_started(document: dict) -> dict:
    """A document nobody has opened, from its manifest entry alone (a corpus collection lists tens of
    thousands; their readings are not opened): its pages, first if its reading has high flags."""
    priority = "high" if (document.get("flag_counts") or {}).get("high") else "full"
    return task(document, "review", priority, document["page_count"], None, started=False)


def proposal_tasks(events: list[dict], instructions: list[dict], document_of) -> list[dict]:
    """Proposals waiting for review: a task per work, kind and step, shown on the document they were last
    saved on (``document_of`` gives its id and title)."""
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for kind, rows in (("events", events), ("instructions", instructions)):
        key, steps = WAITING[kind]
        for revisions in histories(rows, key).values():
            head = revisions[0]
            if head["status"] in steps:
                groups.setdefault((kind, steps[head["status"]], head["work"]), []).append(head)
    return [
        task(document_of(heads[-1]["act_id"]), kind, "proposal", len(heads),
             min(h["created_at"] for h in heads), step=step)
        for (kind, step, _), heads in groups.items()
    ]  # fmt: skip


def ordered(tasks: list[dict]) -> list[dict]:
    return sorted(
        tasks,
        key=lambda t: (
            not t["started"],
            PRIORITIES.index(t["priority"]),
            t["since"] is None,
            t["since"] or datetime.min,
            t["title"].lower(),
        ),
    )


def shown(found: dict, moment: datetime) -> dict:
    """A task as the queue lists it: since when it waits, and its age in days."""
    since = found["since"]
    age = round((moment - since).total_seconds() / 86400, 2) if since else None
    return {**found, "since": since.isoformat() if since else None, "age_days": age}
