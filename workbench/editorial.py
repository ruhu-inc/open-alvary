"""Legal events and amendment instructions recorded in the workbench (CPR-02, CPR-03).

Both are editorial assertions about a work, kept beside its text and shared by its language versions,
like its details. Each save is a new revision; nothing is updated in place.

Events: proposed from the work's text as it now stands, by the patterns operations.events uses on
published text, or added by hand. The editor gives the type, the scope (the whole work or chosen
provisions), the date and its precision or a condition, the evidence (the provision that says so and
its words) and the affected work, which defaults to this one. A reviewer may propose and edit; an
approver marks an event reviewed, which records who and when, or rejects it. The reviewed events export
as an operations.events EventFile for ``python -m operations.events publish``.

Instructions: an amending work's consolidation instructions for the work it amends
(workbench.consolidation): operation, target locator, units or words, effective date, and the amending
provision's locator and citation. An editor reviews each; a second editor, someone else, confirms it
unchanged (EDT-04: two editors). Only then does it export, as ``{"instructions": [...]}`` for the
amended work, for ``python -m workbench.consolidation build``.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import get_args

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from operations.events import EVENT_ID, EVENT_TYPES, EventFile, EventIn, propose_from
from workbench import auth, content, publication
from workbench.consolidation import Instruction
from workbench.store import Conflict

# Each status and the role that may record it.
EVENT_STATUSES = {"proposed": "reviewer", "reviewed": "approver", "rejected": "approver"}
INSTRUCTION_STATUSES = {
    "proposed": "reviewer",
    "reviewed": "reviewer",
    "second_reviewed": "approver",
    "rejected": "approver",
}
PRECISIONS = ("day", "month", "year", "unknown")
OPERATIONS = ("substitute", "insert", "repeal", "renumber", "amend_words")
NOT_PROVISIONS = {"document", "contents", "contents_entry"}  # left out of the provision picker
INSTRUCTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
PLACEHOLDER_URL = "https://example.invalid/"  # stands in for a proposal's missing evidence address
JSON_BODY = Body(...)


def histories(rows: list[dict], key: str) -> dict[str, list[dict]]:
    """Each item's revisions, newest first, the items in the order they were first recorded."""
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row[key], []).insert(0, row)
    return out


def head_of(rows: list[dict], key: str, item: str) -> dict | None:
    """An item's latest revision, if it has any."""
    revisions = histories(rows, key).get(item)
    return revisions[0] if revisions else None


def problems(error: ValidationError) -> str:
    """A validation error, one short sentence per problem."""
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc']) + ': ' if e['loc'] else ''}"
        f"{e['msg'].removeprefix('Value error, ')}"
        for e in error.errors()
    )


def _clean(value) -> str | None:
    return " ".join(str(value).split()) or None if value is not None else None


# --- legal events (CPR-02) ---------------------------------------------------------------------------


def event_fields(given: dict, work: str, url: str | None) -> dict:
    """An event as the editor gave it, in operations.events' shape less its id and review. The affected
    and asserting works default to this work, the evidence's address to the document's source."""
    if not isinstance(given, dict):
        raise ValueError("the event must be an object")
    evidence = given.get("evidence") or {}
    locators = given.get("locators") or []
    if not isinstance(evidence, dict) or not isinstance(locators, list):
        raise ValueError("evidence must be an object and locators a list")
    date_from = _clean(given.get("date_from"))
    return {
        "type": given.get("type"),
        "affected_work": _clean(given.get("affected_work", work)),
        "asserting_work": _clean(given.get("asserting_work", work)),
        "scope": given.get("scope") or "whole",
        "locators": [x for x in (_clean(v) for v in locators) if x],
        "date_from": date_from,
        "date_to": _clean(given.get("date_to")),
        "precision": given.get("precision") or ("day" if date_from else "unknown"),
        "conditions": _clean(given.get("conditions")),
        "evidence": {
            "url": _clean(evidence.get("url")) or url,
            "locator": _clean(evidence.get("locator")),
            "quote": _clean(evidence.get("quote")),
        },
        "supersedes": _clean(given.get("supersedes")),
    }


def check_event(event_id: str, fields: dict, *, complete: bool, work: str, provisions: set[str]) -> EventIn:
    """The event as publishing will read it (raises ValueError). A proposal may still lack its affected
    work and its evidence's address; a reviewed event may not. Provisions named in this work must be in
    its text as it now stands; another work's are checked when the events are published."""
    probe = {**fields, "id": event_id, "reviewed_by": "-", "reviewed_at": datetime.now(UTC)}
    if not complete:
        probe["affected_work"] = probe["affected_work"] or "-"
        probe["evidence"] = {**probe["evidence"], "url": probe["evidence"]["url"] or PLACEHOLDER_URL}
    event = EventIn.model_validate(probe)
    unknown = [x for x in fields["locators"] if fields["affected_work"] == work and x not in provisions]
    anchor = fields["evidence"]["locator"]
    if anchor and fields["asserting_work"] == work and anchor not in provisions:
        unknown.append(anchor)
    if unknown:
        raise ValueError(f"No provision {', '.join(unknown)} in this work's text")
    return event


def event_file(items: dict[str, list[dict]], names: dict[str, str]) -> EventFile:
    """The reviewed events as ``python -m operations.events publish`` reads them, each reviewed by the
    approver who marked it so (by name), at that time."""
    events = [
        {
            **head["event"],
            "id": event_id,
            "review_state": "reviewed",
            "reviewed_by": names.get(head["author"]) or head["author"],
            "reviewed_at": head["created_at"].isoformat(),
        }
        for event_id, (head, *_) in items.items()
        if head["status"] == "reviewed"
    ]
    return EventFile.model_validate({"events": events})


def proposed(text: publication.Text, work: str, url: str | None, held: set[str]) -> list[dict]:
    """New proposals from the text: each event's fields, what it was proposed from (its type, provision
    and the title of the law it names) and a note, less those already recorded, even if rejected."""
    out = []
    for found in propose_from(text.serialized.text.encode(), text.structure.canonical(), work, url or ""):
        title = found.get("affected_title") or ""
        key = f"{found['type']}:{found['evidence']['locator']}:{title}"[:400]
        if key in held:
            continue
        held.add(key)
        note = f"Proposed from the text{f': it repeals “{title}”' if title else ''}"
        out.append({"key": key, "fields": event_fields(found, work, url), "comment": note})
    return out


# --- amendment instructions (CPR-03) -----------------------------------------------------------------


def instruction_of(instruction_id: str, given: dict, work: str) -> Instruction:
    """The instruction as workbench.consolidation applies it (raises ValueError), amending from this
    work. Fields left empty are left out."""
    if not isinstance(given, dict) or not isinstance(given.get("amending") or {}, dict):
        raise ValueError("the instruction and its amending provision must be objects")
    amending = given.get("amending") or {}
    values = {k: v for k, v in given.items() if k not in ("id", "amending") and v not in (None, "", [])}
    by = {
        "work": work,
        "locator": _clean(amending.get("locator")) or "",
        "citation": _clean(amending.get("citation")) or "",
    }
    return Instruction.model_validate({**values, "id": instruction_id, "amending": by})


def second_review_refused(
    head: dict | None, who: str, target: str, instruction: dict
) -> tuple[int, str] | None:
    """Why a second review cannot be recorded (a status code and the reason), or None. It confirms,
    unchanged, an instruction that another editor reviewed (EDT-04)."""
    if not head or head["status"] != "reviewed":
        return 409, "Only an instruction an editor has reviewed can have its second review"
    if head["author"] == who:
        return 403, "The second review must be by an editor other than the one who reviewed it"
    if head["instruction"] != instruction or head["target_work"] != target:
        return 409, (
            "A second review confirms the instruction exactly as reviewed: to change it, save it as "
            "reviewed and ask another editor for the second review"
        )
    return None


def instructions_for(target: str, items: dict[str, list[dict]]) -> dict:
    """The second-reviewed instructions amending ``target``, from any amending work, in the order the
    applier takes them, as ``python -m workbench.consolidation build`` reads them."""
    found = [
        Instruction.model_validate({**head["instruction"], "id": instruction_id})
        for instruction_id, (head, *_) in items.items()
        if head["status"] == "second_reviewed" and head["target_work"] == target
    ]
    found.sort(key=lambda i: (i.effective_date, i.id))
    return {"instructions": [i.model_dump(mode="json", exclude_none=True) for i in found]}


# --- routes ------------------------------------------------------------------------------------------


def _shown(revisions: list[dict], key: str, value: str) -> dict:
    head = revisions[0]
    return {
        key: head[key],
        "revision": head["revision"],
        "status": head["status"],
        value: head[value],
        "comment": head["comment"],
        "author": head["author"],
        "at": head["created_at"].isoformat(),
        "document": head["act_id"],
        "history": [
            {"revision": r["revision"], "status": r["status"], "author": r["author"],
             "at": r["created_at"].isoformat(), "comment": r["comment"]}
            for r in revisions
        ],
    }  # fmt: skip


def install(app: FastAPI, store, library, review) -> None:
    """The legal events and amendment instructions of a document's work, beside its pages and details."""

    def document(act_id: str) -> dict:
        try:
            return library.act(act_id)
        except KeyError as error:
            raise HTTPException(404, "No such document") from error

    def text_of(act: dict) -> publication.Text:
        """The work's text as it now stands: what proposals are read from and provisions picked in."""
        return publication.build(
            act, content.final_pages(library.reading(act["id"]), store.latest_pages(act["id"]))
        )

    def provisions(text: publication.Text) -> list[dict]:
        return [
            {
                "locator": n.locator,
                "kind": n.kind,
                "label": n.label,
                "heading": n.heading,
                "page": n.page_start,
            }
            for n in text.structure.nodes
            if n.kind not in NOT_PROVISIONS
        ]

    def source_url(act: dict) -> str | None:
        return library.provenance(act["id"]).get("url") or act.get("url") or None

    def held(work: str) -> str | None:
        """The workbench's document for a work, if it holds one (any language version)."""
        return next((d for d in (work, f"{work}-en", f"{work}-fr", f"{work}-rw") if library.has(d)), None)

    def attachment(name: str) -> dict:
        return {"Content-Disposition": f'attachment; filename="{name}"'}

    # --- legal events ---------------------------------------------------------------------------------

    @app.get("/api/documents/{act_id}/events")
    def legal_events(request: Request, act_id: str):
        auth.current(request, store)
        act = document(act_id)
        work = review.details_key(act)
        items = histories(store.legal_event_revisions(work), "event_id")
        return {
            "work": work,
            "source_url": source_url(act),
            "types": list(get_args(EVENT_TYPES)),
            "precisions": list(PRECISIONS),
            "provisions": provisions(text_of(act)),
            "events": [_shown(revisions, "event_id", "event") for revisions in items.values()],
            "names": store.names({r["author"] for revisions in items.values() for r in revisions}),
        }

    @app.post("/api/documents/{act_id}/events/proposals")
    def propose_events(request: Request, act_id: str):
        """Record, as proposals, the events the work's text states that are not recorded yet."""
        who = auth.current(request, store, write=True, role="reviewer")
        act = document(act_id)
        work = review.details_key(act)
        recorded = {r["proposed_from"] for r in store.legal_event_revisions(work) if r["proposed_from"]}
        added = []
        for found in proposed(text_of(act), work, source_url(act), recorded):
            event_id = f"evt_{uuid.uuid4()}"
            store.save_legal_event(
                event_id,
                0,
                who["email"],
                work=work,
                act_id=act_id,
                event=found["fields"],
                status="proposed",
                proposed_from=found["key"],
                comment=found["comment"],
            )
            added.append(event_id)
        return {"added": added}

    @app.post("/api/documents/{act_id}/events")
    def save_event(request: Request, act_id: str, body: dict = JSON_BODY):
        """A new revision of an event (a new event without an id): proposed by a reviewer, or reviewed
        or rejected by an approver."""
        status = body.get("status")
        who = auth.current(request, store, write=True, role=EVENT_STATUSES.get(status, "reviewer"))
        if status not in EVENT_STATUSES:
            raise HTTPException(422, "status must be proposed, reviewed or rejected")
        act = document(act_id)
        work = review.details_key(act)
        event_id = str(body.get("event_id") or f"evt_{uuid.uuid4()}")
        if not re.fullmatch(EVENT_ID, event_id):
            raise HTTPException(422, "event_id must be evt_ followed by a UUID")
        head = head_of(store.legal_event_revisions(work), "event_id", event_id)
        try:
            if status == "rejected" and head:
                fields = head["event"]  # a rejection keeps the event as it stood
            else:
                fields = event_fields(body.get("event") or {}, work, source_url(act))
                known = {p["locator"] for p in provisions(text_of(act))}
                check_event(event_id, fields, complete=status == "reviewed", work=work, provisions=known)
        except ValidationError as error:
            raise HTTPException(422, problems(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        try:
            revision = store.save_legal_event(
                event_id,
                int(body.get("expected_revision", -1)),
                who["email"],
                work=work,
                act_id=act_id,
                event=fields,
                status=status,
                proposed_from=head["proposed_from"] if head else None,
                comment=str(body.get("comment", ""))[:2000],
            )
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        return {"event_id": event_id, "revision": revision}

    @app.get("/api/documents/{act_id}/events/export")
    def export_events(request: Request, act_id: str):
        """The work's reviewed events, as an events file to publish."""
        auth.current(request, store)
        work = review.details_key(document(act_id))
        items = histories(store.legal_event_revisions(work), "event_id")
        names = store.names({revisions[0]["author"] for revisions in items.values()})
        try:
            found = event_file(items, names)
        except ValidationError as error:
            raise HTTPException(409, f"The reviewed events do not validate: {problems(error)}") from error
        return JSONResponse(
            found.model_dump(mode="json", exclude_none=True), headers=attachment(f"{work}.events.json")
        )

    # --- amendment instructions ---------------------------------------------------------------------

    @app.get("/api/documents/{act_id}/instructions")
    def amendment_instructions(request: Request, act_id: str):
        auth.current(request, store)
        act = document(act_id)
        work = review.details_key(act)
        items = histories(store.instruction_revisions(work), "instruction_id")
        shown = [
            {**_shown(revisions, "instruction_id", "instruction"), "target_work": revisions[0]["target_work"]}
            for revisions in items.values()
        ]
        return {
            "work": work,
            "operations": list(OPERATIONS),
            "provisions": provisions(text_of(act)),
            "instructions": shown,
            "targets": {i["target_work"]: held(i["target_work"]) for i in shown},
            "names": store.names({r["author"] for revisions in items.values() for r in revisions}),
        }

    @app.post("/api/documents/{act_id}/instructions")
    def save_instruction(request: Request, act_id: str, body: dict = JSON_BODY):
        """A new revision of an instruction: proposed or reviewed by an editor; second-reviewed by an
        approver other than the editor who reviewed it, confirming it unchanged; or rejected."""
        status = body.get("status")
        who = auth.current(request, store, write=True, role=INSTRUCTION_STATUSES.get(status, "reviewer"))
        if status not in INSTRUCTION_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(INSTRUCTION_STATUSES)}")
        act = document(act_id)
        work = review.details_key(act)
        instruction_id = str(body.get("instruction_id") or f"ins_{uuid.uuid4().hex[:16]}")
        if not INSTRUCTION_ID.fullmatch(instruction_id):
            raise HTTPException(422, "instruction_id must be up to 80 letters, digits and _ . : -")
        head = head_of(store.instruction_revisions(work), "instruction_id", instruction_id)
        target = _clean(body.get("target_work")) or ""
        if status == "rejected" and head:
            instruction, target = head["instruction"], head["target_work"]  # kept as it was
        else:
            if not 1 <= len(target) <= 160:
                raise HTTPException(422, "Name the work this instruction amends")
            try:
                parsed = instruction_of(instruction_id, body.get("instruction") or {}, work)
            except ValidationError as error:
                raise HTTPException(422, problems(error)) from error
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            if parsed.amending.locator not in {p["locator"] for p in provisions(text_of(act))}:
                raise HTTPException(422, "Choose the amending provision from this work's text")
            if not parsed.amending.citation:
                raise HTTPException(422, "Cite the amending provision")
            instruction = parsed.model_dump(mode="json", exclude={"id"})
        refused = status == "second_reviewed" and second_review_refused(
            head, who["email"], target, instruction
        )
        if refused:
            raise HTTPException(*refused)
        try:
            revision = store.save_instruction(
                instruction_id,
                int(body.get("expected_revision", -1)),
                who["email"],
                work=work,
                act_id=act_id,
                target_work=target,
                instruction=instruction,
                status=status,
                comment=str(body.get("comment", ""))[:2000],
            )
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        return {"instruction_id": instruction_id, "revision": revision}

    @app.get("/api/instructions/export")
    def export_instructions(request: Request, target: str = ""):
        """The second-reviewed instructions amending one work, from every amending work."""
        auth.current(request, store)
        target = _clean(target) or ""
        if not target:
            raise HTTPException(422, "Name the amended work: ?target=<work>")
        found = instructions_for(target, histories(store.instruction_revisions(), "instruction_id"))
        return JSONResponse(found, headers=attachment(f"{target}.instructions.json"))
