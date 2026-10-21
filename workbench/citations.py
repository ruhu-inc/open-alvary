"""A judgment's citations and how it treats the judgments it cites, reviewed beside its pages and details
(EDT-01, EDT-02).

Proposals come from the judgment's published paragraphs: ``python -m operations.citations propose --work
<judgment>`` reads them with the catalogue's citation resolver and prints a proposals file, which a
reviewer imports here. Each names the citing paragraph, the cited work and, when its pinpoint resolved,
the cited part; it is recorded once, and only if that paragraph, with those words, is in the judgment's
text as it now stands. Nothing proposed is published.

An approver confirms or rejects each, stating their role: whether the paragraph cites that work or part is
a fact anyone can check against the text (a structural review), so no qualification is asked. A confirmed
citation of another judgment may also have how the paragraph treats it recorded (follows, applies,
distinguishes, doubts, overrules): a doctrinal review, which states the reviewer's legal qualification for
the jurisdiction and is refused without one, with the passage that does it, quoted from the paragraph, and
the opinion it is in. A treatment is never inferred: none recorded, none published. Neither is edited once
recorded: a confirmed citation or a treatment is withdrawn, and another confirmed or recorded in its place.

The confirmed citations, the withdrawn ones and their treatments export as a citations file for
``python -m operations.citations publish``.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Literal, get_args

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from operations.citations import CITATION_ID, OPINIONS, TREATMENTS, CitationFile, words_of
from workbench import auth, content, publication, structure
from workbench.editorial import head_of, histories, problems
from workbench.store import Conflict

# Each status and the role that may record it.
STATUSES = {"proposed": "reviewer", "confirmed": "approver", "rejected": "approver", "withdrawn": "approver"}
JSON_BODY = Body(...)


class Proposal(BaseModel):
    """A proposal as operations.citations prints it: what the paragraph cites, as the resolver read it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    citing_locator: str = Field(min_length=1, max_length=300)
    citing_pinpoint: str | None = Field(None, max_length=100)
    cited_work: str = Field(min_length=1, max_length=160)
    cited_title: str | None = Field(None, max_length=1000)
    cited_citation: str | None = Field(None, max_length=500)
    cited_document_type: str | None = Field(None, max_length=40)
    cited_locator: str | None = Field(None, min_length=1, max_length=300)
    cited_pinpoint: str | None = Field(None, max_length=300)
    unresolved_pinpoint: str | None = Field(None, max_length=40)
    printed: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=2000)
    evidence_url: str = Field(pattern=r"^https://", max_length=2000)
    extraction: Literal["citations-1"] = "citations-1"

    def key(self) -> str:
        return f"{self.citing_locator}|{self.cited_work}|{self.cited_locator or ''}"[:400]


def is_judgment(text: publication.Text) -> bool:
    """Whether a document's text is read as a judgment's paragraphs (structure rules judgment-paragraphs-1)."""
    return text.rules_id == structure.JUDGMENT


def paragraphs(text: publication.Text) -> dict[str, dict]:
    """A judgment's numbered paragraphs by locator: printed number, the opinion they are in, page and words."""
    raw = text.serialized.text.encode()
    return {
        node.locator: {
            "locator": node.locator,
            "label": node.label,
            "opinion": node.parent.label if node.parent is not None and node.parent.kind == "opinion" else "",
            "author": getattr(node.parent, "author", "") if node.parent is not None else "",
            "page": node.page_start,
            "words": words_of(raw[node.start : node.end].decode()),
        }
        for node in text.structure.nodes
        if node.kind == "paragraph"
    }


def citation_file(items: dict[str, list[dict]], names: dict[str, str], work: str) -> CitationFile:
    """The confirmed and withdrawn citations of a judgment, with their treatments, as ``python -m
    operations.citations publish`` reads them: each reviewed by the approver who confirmed it (by name),
    each treatment by the one who recorded it, with their roles and the treatment's qualification."""
    found = []
    for citation_id, (head, *_) in items.items():
        held, review = head["citation"], head["citation"].get("review")
        if head["status"] not in ("confirmed", "withdrawn") or review is None:
            continue
        found.append(
            {
                "id": citation_id,
                "citing_work": work,
                "citing_locator": held["citing_locator"],
                "cited_work": held["cited_work"],
                "cited_locator": held.get("cited_locator"),
                "printed": held["printed"],
                "quote": held["quote"],
                "evidence_url": held["evidence_url"],
                "extraction": held.get("extraction", "citations-1"),
                "review_state": "reviewed" if head["status"] == "confirmed" else "withdrawn",
                "reviewed_by": names.get(review["reviewed_by"]) or review["reviewed_by"],
                "reviewer_role": review["reviewer_role"],
                "reviewed_at": review["reviewed_at"],
                "treatments": [
                    {
                        **{
                            k: t[k]
                            for k in ("id", "kind", "opinion", "quote", "reviewer_role", "review_state")
                        },
                        "reviewer_qualification": t["reviewer_qualification"],
                        "reviewed_by": names.get(t["reviewed_by"]) or t["reviewed_by"],
                        "reviewed_at": t["reviewed_at"],
                    }
                    for t in held.get("treatments", [])
                ],
            }
        )
    return CitationFile.model_validate({"citations": found})


def _shown(revisions: list[dict]) -> dict:
    head = revisions[0]
    return {
        "citation_id": head["citation_id"],
        "revision": head["revision"],
        "status": head["status"],
        "citation": head["citation"],
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
    """A judgment's citations and their treatment, beside its pages and details."""

    def document(act_id: str) -> dict:
        try:
            return library.act(act_id)
        except KeyError as error:
            raise HTTPException(404, "No such document") from error

    def text_of(act: dict) -> publication.Text:
        """The judgment's text as it now stands: what proposals and passages are checked against."""
        return publication.build(
            act, content.final_pages(library.reading(act["id"]), store.latest_pages(act["id"]))
        )

    def save(citation_id, body, who, act_id, work, citation, status, proposed_from=None):
        try:
            return store.save_citation(
                citation_id,
                int(body.get("expected_revision", -1)),
                who["email"],
                work=work,
                act_id=act_id,
                citation=citation,
                status=status,
                proposed_from=proposed_from,
                comment=str(body.get("comment", ""))[:2000],
            )
        except Conflict as error:
            raise HTTPException(409, str(error)) from error

    def head(work: str, citation_id: str) -> dict:
        if not re.fullmatch(CITATION_ID, citation_id):
            raise HTTPException(422, "citation_id must be cit_ followed by a UUID")
        found = head_of(store.citation_revisions(work), "citation_id", citation_id)
        if found is None:
            raise HTTPException(404, "No such citation of this judgment")
        return found

    @app.get("/api/documents/{act_id}/citations")
    def citations(request: Request, act_id: str):
        auth.current(request, store)
        act = document(act_id)
        work = review.details_key(act)
        text = text_of(act)
        items = histories(store.citation_revisions(work), "citation_id")
        people = {r["author"] for revisions in items.values() for r in revisions}
        for revisions in items.values():
            held = revisions[0]["citation"]
            people |= {held["review"]["reviewed_by"]} if held.get("review") else set()
            people |= {t["reviewed_by"] for t in held.get("treatments", [])}
        return {
            "work": work,
            "judgment": is_judgment(text),
            "jurisdiction": act.get("jurisdiction"),
            "paragraphs": [{k: v for k, v in p.items() if k != "words"} for p in paragraphs(text).values()],
            "treatments": list(get_args(TREATMENTS)),
            "opinions": list(get_args(OPINIONS)),
            "citations": [_shown(revisions) for revisions in items.values()],
            "names": store.names(people),
        }

    @app.post("/api/documents/{act_id}/citations/proposals")
    def import_proposals(request: Request, act_id: str, body: dict = JSON_BODY):
        """Record, as proposals, the citations a proposals file (operations.citations propose) names that
        this judgment's text holds as it now stands and that are not recorded yet, even if rejected."""
        who = auth.current(request, store, write=True, role="reviewer")
        act = document(act_id)
        work = review.details_key(act)
        text = text_of(act)
        if not is_judgment(text):
            raise HTTPException(
                422, "Citations are read from a judgment's paragraphs; this is not a judgment"
            )
        if body.get("work") != work:
            raise HTTPException(
                422, f"These proposals are for {body.get('work')!r}, not this judgment ({work})"
            )
        if not isinstance(body.get("proposals"), list):
            raise HTTPException(422, "A proposals file lists its proposals")
        held = paragraphs(text)
        recorded = {r["proposed_from"] for r in store.citation_revisions(work) if r["proposed_from"]}
        added, skipped = [], []
        for given in body["proposals"][:1000]:
            try:
                proposal = Proposal.model_validate(given)
            except ValidationError as error:
                key = given.get("key", "") if isinstance(given, dict) else ""
                skipped.append({"key": str(key)[:400], "reason": problems(error)})
                continue
            paragraph, key = held.get(proposal.citing_locator), proposal.key()
            reason = (
                "already recorded"
                if key in recorded
                else f"no paragraph {proposal.citing_locator} in this text"
                if paragraph is None
                else "its words are not this paragraph's as the text now stands"
                if words_of(proposal.quote) not in paragraph["words"]
                else None
            )
            if reason:
                skipped.append({"key": key, "reason": reason})
                continue
            citation_id = f"cit_{uuid.uuid4()}"
            citation = proposal.model_dump() | {"review": None, "treatments": []}
            body_of = {
                "expected_revision": 0,
                "comment": f"Proposed from the published text: “{proposal.printed}”",
            }
            save(citation_id, body_of, who, act_id, work, citation, "proposed", key)
            recorded.add(key)
            added.append(citation_id)
        return {"added": added, "skipped": skipped}

    @app.post("/api/documents/{act_id}/citations")
    def save_citation(request: Request, act_id: str, body: dict = JSON_BODY):
        """A new revision of a proposed citation: confirmed by an approver who states their role, or
        rejected; or a confirmed one withdrawn, with its treatment."""
        status = body.get("status")
        who = auth.current(request, store, write=True, role=STATUSES.get(status, "approver"))
        if status not in ("confirmed", "rejected", "withdrawn"):
            raise HTTPException(422, "status must be confirmed, rejected or withdrawn")
        act = document(act_id)
        work = review.details_key(act)
        now = head(work, str(body.get("citation_id") or ""))
        citation = dict(now["citation"])
        if status in ("confirmed", "rejected") and now["status"] != "proposed":
            raise HTTPException(409, f"Only a proposed citation is {status}; this one is {now['status']}")
        if status == "withdrawn" and now["status"] != "confirmed":
            raise HTTPException(409, "Only a confirmed citation is withdrawn")
        if status == "confirmed":
            role = words_of(str(body.get("reviewer_role") or ""))
            if not role or len(role) > 300:
                raise HTTPException(422, "State your role, such as owner or Nigerian advocate")
            citation["review"] = {"reviewer_role": role, "reviewed_by": who["email"], "reviewed_at": _now()}
        if status == "withdrawn":
            citation["treatments"] = [
                t | {"review_state": "withdrawn"} for t in citation.get("treatments", [])
            ]
        revision = save(now["citation_id"], body, who, act_id, work, citation, status, now["proposed_from"])
        return {"citation_id": now["citation_id"], "revision": revision}

    @app.post("/api/documents/{act_id}/citations/{citation_id}/treatment")
    def save_treatment(request: Request, act_id: str, citation_id: str, body: dict = JSON_BODY):
        """How a confirmed citation's paragraph treats the judgment it cites, recorded by a doctrinal review
        that states the reviewer's legal qualification; or its treatment withdrawn."""
        who = auth.current(request, store, write=True, role="approver")
        act = document(act_id)
        work = review.details_key(act)
        now = head(work, citation_id)
        citation = dict(now["citation"])
        treatments = list(citation.get("treatments", []))
        live = [t for t in treatments if t["review_state"] == "reviewed"]
        if now["status"] != "confirmed":
            raise HTTPException(409, "Only a confirmed citation has its treatment recorded or withdrawn")
        if body.get("action") == "withdraw":
            if not live:
                raise HTTPException(409, "This citation has no treatment to withdraw")
            citation["treatments"] = [
                t | {"review_state": "withdrawn"} if t in live else t for t in treatments
            ]
        elif body.get("action") == "record":
            if citation.get("cited_document_type") != "judgment":
                raise HTTPException(
                    422, "Judicial treatment is of a judgment; this citation is of another record"
                )
            if live:
                raise HTTPException(
                    409, "This citation has a treatment: withdraw it first, then record another"
                )
            if body.get("review_level") != "doctrinal":
                raise HTTPException(
                    422, "A treatment is recorded by a doctrinal review (review_level doctrinal)"
                )
            qualification = words_of(str(body.get("reviewer_qualification") or ""))
            if not qualification or len(qualification) > 300:
                raise HTTPException(
                    422,
                    "A treatment is a doctrinal review: state your legal qualification for "
                    f"{act.get('jurisdiction') or 'this jurisdiction'} (reviewer_qualification)",
                )
            role = words_of(str(body.get("reviewer_role") or ""))
            if not role or len(role) > 300:
                raise HTTPException(422, "State your role, such as Nigerian advocate")
            kind, opinion = body.get("kind"), body.get("opinion") or "unknown"
            if kind not in get_args(TREATMENTS) or opinion not in get_args(OPINIONS):
                raise HTTPException(422, f"kind must be one of {', '.join(get_args(TREATMENTS))}, and opinion "
                                         f"one of {', '.join(get_args(OPINIONS))}")  # fmt: skip
            quote = words_of(str(body.get("quote") or ""))
            paragraph = paragraphs(text_of(act)).get(citation["citing_locator"])
            if not quote or len(quote) > 2000 or paragraph is None or quote not in paragraph["words"]:
                raise HTTPException(422, "Quote the passage that treats it, as the paragraph's own words")
            citation["treatments"] = [
                *treatments,
                {
                    "id": f"trt_{uuid.uuid4()}", "kind": kind, "opinion": opinion, "quote": quote,
                    "review_level": "doctrinal", "reviewer_role": role, "reviewer_qualification": qualification,
                    "reviewed_by": who["email"], "reviewed_at": _now(), "review_state": "reviewed",
                },
            ]  # fmt: skip
        else:
            raise HTTPException(422, "action must be record or withdraw")
        revision = save(citation_id, body, who, act_id, work, citation, "confirmed", now["proposed_from"])
        return {"citation_id": citation_id, "revision": revision}

    @app.get("/api/documents/{act_id}/citations/export")
    def export_citations(request: Request, act_id: str):
        """The judgment's confirmed and withdrawn citations and their treatments, as a citations file."""
        auth.current(request, store)
        work = review.details_key(document(act_id))
        items = histories(store.citation_revisions(work), "citation_id")
        people = set()
        for revisions in items.values():
            held = revisions[0]["citation"]
            people |= {held["review"]["reviewed_by"]} if held.get("review") else set()
            people |= {t["reviewed_by"] for t in held.get("treatments", [])}
        try:
            found = citation_file(items, store.names(people), work)
        except ValidationError as error:
            raise HTTPException(409, f"The confirmed citations do not validate: {problems(error)}") from error
        return JSONResponse(
            found.model_dump(mode="json", exclude_none=True),
            headers={"Content-Disposition": f'attachment; filename="{work}.citations.json"'},
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()
