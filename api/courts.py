"""The court hierarchy register (CPR-04): for one jurisdiction, each court and tribunal with its level and
rank, its appeal routes and the dates its role applies; how each court's decisions bear on the other
courts (binding, persuasive or not applicable); whether precedent binds there at all; and the reviewed
source of every entry. Read from ingestion/courts/<jurisdiction>.json (see the README there).

A regional or continental body's register (JUR-04: ``aa-ohada``, ``aa-eac``, ``aa-au``) also lists the
states its courts reach and from when, the routes from their courts to its own (cassation in place of the
national court, references of a question of interpretation), and declarations accepting a court's
jurisdiction over cases from individuals. How its decisions bear on a member state's courts is a relation
like any other, limited to the subject matter a source gives: never a rank above national courts.

Nothing is guessed. Whether precedent binds is a recorded rule of the jurisdiction, never assumed from
its legal system; a court, relation or precedent rule the register does not give, or a register not
yet reviewed, answers ``unknown``. A review covers the register as it was reviewed: one changed since
(its ``reviewed_sha256`` no longer matching) is refused until it is reviewed again (operations.courts).
"""

import hashlib
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, model_validator

from api.reach import country
from schema.public import (
    CourtDeclarationView,
    CourtEvidence,
    CourtLevelView,
    CourtMatterView,
    CourtMemberView,
    CourtPrecedentView,
    CourtRegisterSummary,
    CourtRegisterView,
    CourtRelationView,
    CourtRouteView,
    EvidenceBearing,
    EvidenceCourt,
)
from schema.record import COURT_LEVELS, PlaceCode, RecordModel, Short, Text

COURTS = Path(__file__).resolve().parents[1] / "ingestion" / "courts"
Key = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,59}$")]
Bearing = Literal["binding", "persuasive", "not_applicable"]
Answer = Literal["binding", "persuasive", "not_applicable", "unknown"]
REVIEW = frozenset({"review_state", "reviewed_by", "reviewed_on", "reviewed_sha256"})


class Source(RecordModel):
    """Where an entry is confirmed: a constitution, an Act, a treaty, a practice direction, a court's own
    record, a reported decision."""

    id: Key
    title: Short
    url: HttpUrl
    pinpoint: Short | None = None  # "s. 287(1)", "art. 143"


class Dated(RecordModel):
    valid_from: date | None = None  # None: since before any date the register knows
    valid_until: date | None = None  # None: still

    @model_validator(mode="after")
    def ordered(self):
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError(f"valid_until {self.valid_until} is not after valid_from {self.valid_from}")
        return self

    def applies(self, on: date) -> bool:
        return (self.valid_from or date.min) <= on < (self.valid_until or date.max)

    def overlaps(self, other: "Dated") -> bool:
        start = max(self.valid_from or date.min, other.valid_from or date.min)
        return start < min(self.valid_until or date.max, other.valid_until or date.max)


class Court(Dated):
    id: Key
    name: Short
    kind: Literal["court", "tribunal", "quasi_judicial"] = "court"  # quasi_judicial: the African Commission
    level: Literal[*COURT_LEVELS]
    rank: int | None = Field(default=None, ge=1)  # 1 the highest; courts of equal standing share one
    jurisdiction: PlaceCode | None = None  # another jurisdiction's court the register lists: its code
    appeals_to: tuple[Key, ...] = ()
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class Relation(Dated):
    """How decisions of ``court`` bear on ``bound`` (a court of the register), or on the jurisdiction's
    courts generally when ``bound`` is None; with ``members``, on the courts of each state the register
    lists in ``members`` while it is one. ``matter`` limits it to one of the register's ``matters``."""

    court: Key
    bound: Key | None = None
    members: bool = False
    matter: Key | None = None
    relation: Bearing
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class PrecedentRule(RecordModel):
    """Whether decisions bind later courts in this jurisdiction at all (stare decisis), as its law says."""

    binds: bool
    note: Text  # "Decisions of the Supreme Court bind all other courts (Constitution, s. 287)."
    sources: tuple[Key, ...] = Field(min_length=1)


class Matter(RecordModel):
    """A subject matter a relation or route is limited to, as its source defines it: "the Uniform Acts and
    regulations, save decisions applying criminal sanctions" (OHADA Treaty, art. 14)."""

    id: Key
    name: Short
    sources: tuple[Key, ...] = Field(min_length=1)


class Member(Dated):
    """A state the register's courts reach: a member of (or party to) the body's treaty, or with ``court`` a
    party to that court's own instrument only; from the instrument's entry into force for the state to
    the first day its withdrawal no longer bound it."""

    state: PlaceCode
    court: Key | None = None
    withdrawal_notified: date | None = None
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class MemberRoute(Dated):
    """A route from the courts of each member state to a court of the register: ``cassation`` in place of
    the national court of last resort, ``appeal``, or a ``reference`` of a question of interpretation."""

    court: Key
    kind: Literal["cassation", "appeal", "reference"]
    matter: Key | None = None
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class Declaration(Dated):
    """A state's declaration accepting a court's jurisdiction over cases from individuals and NGOs (AfCHPR
    Protocol, art. 34(6)): deposited on ``valid_from``; withdrawn, deposited on ``withdrawal_deposited``
    and no longer applying from ``valid_until``."""

    state: PlaceCode
    court: Key
    withdrawal_deposited: date | None = None
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class CourtRegister(RecordModel):
    jurisdiction: PlaceCode
    review_state: Literal["unreviewed", "reviewed"] = "unreviewed"
    reviewed_by: Short | None = None
    reviewed_on: date | None = None
    reviewed_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None  # what was reviewed
    precedent: PrecedentRule | None = None
    courts: tuple[Court, ...] = ()
    relations: tuple[Relation, ...] = ()
    matters: tuple[Matter, ...] = ()
    members: tuple[Member, ...] = ()
    member_routes: tuple[MemberRoute, ...] = ()
    declarations: tuple[Declaration, ...] = ()
    sources: tuple[Source, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        where = self.jurisdiction
        if self.review_state == "reviewed":
            if not (self.reviewed_by and self.reviewed_on and self.reviewed_sha256):
                raise ValueError(
                    f"{where}: a reviewed register names its reviewer, date and what was reviewed"
                )
            if self.reviewed_sha256 != digest(self):
                raise ValueError(f"{where}: the register changed after its review; review it again")
        courts = {c.id for c in self.courts}
        sources = {s.id for s in self.sources}
        matters = {m.id for m in self.matters}
        if (len(courts), len(sources), len(matters)) != tuple(
            map(len, (self.courts, self.sources, self.matters))
        ):
            raise ValueError(f"{where}: court, source and matter ids must be unique")
        entries = [
            *self.courts, *self.relations, *self.matters, *self.members, *self.member_routes,
            *self.declarations, *([self.precedent] if self.precedent else []),
        ]  # fmt: skip
        for entry in entries:
            if unknown := set(entry.sources) - sources:
                raise ValueError(f"{where}: an entry cites unknown sources {sorted(unknown)}")
        for court in self.courts:
            if unknown := set(court.appeals_to) - courts:
                raise ValueError(f"{where}: {court.id} appeals to unknown courts {sorted(unknown)}")
        named = [
            *(r.court for r in self.member_routes),
            *(d.court for d in self.declarations),
            *(m.court for m in self.members if m.court is not None),
        ]
        if unknown := set(named) - courts:
            raise ValueError(f"{where}: an entry names courts the register does not list {sorted(unknown)}")
        scoped = [*self.relations, *self.member_routes]
        if unknown := {e.matter for e in scoped if e.matter is not None} - matters:
            raise ValueError(f"{where}: an entry names matters the register does not list {sorted(unknown)}")
        for n, relation in enumerate(self.relations):
            if relation.court not in courts or (relation.bound is not None and relation.bound not in courts):
                raise ValueError(f"{where}: a relation names a court the register does not list")
            if relation.members and (relation.bound is not None or not self.members):
                raise ValueError(f"{where}: a relation to member states' courts names no court of its own")
            if relation.relation == "binding" and self.precedent is not None and not self.precedent.binds:
                raise ValueError(f"{where}: precedent does not bind here, yet {relation.court} binds")
            key = (relation.court, relation.bound, relation.members, relation.matter)
            for other in self.relations[n + 1 :]:
                if (other.court, other.bound, other.members, other.matter) == key and relation.overlaps(
                    other
                ):
                    bound = relation.bound or ("member states' courts" if relation.members else "its courts")
                    raise ValueError(f"{where}: two relations of {relation.court} to {bound} apply at once")
        for kind, items in (("membership", self.members), ("declaration", self.declarations)):
            for n, item in enumerate(items):
                for other in items[n + 1 :]:
                    if (other.state, other.court) == (item.state, item.court) and item.overlaps(other):
                        raise ValueError(f"{where}: two {kind}s of {item.state} apply at once")
        return self

    def reaches(self, jurisdiction: str, court_id: str, on: date) -> bool:
        """Whether ``jurisdiction`` (or the country it is part of) is, on ``on``, a member state whose
        courts ``court_id``'s relations to member states' courts cover."""
        state = country(jurisdiction)
        return any(m.state == state and m.court in (None, court_id) and m.applies(on) for m in self.members)


def digest(register: CourtRegister) -> str:
    """The SHA-256 of what a review covers: every entry and source, not the review itself. Fields left at
    their defaults are not part of it, so a field added to the format later leaves reviews standing."""
    body = register.model_dump(mode="json", exclude=set(REVIEW), exclude_defaults=True)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load(jurisdiction: str, root: Path | None = None) -> CourtRegister:
    """A jurisdiction's register; an empty, unreviewed one where none is written."""
    jurisdiction = CourtRegister(jurisdiction=jurisdiction).jurisdiction  # a place code, never a path
    path = (root or COURTS) / f"{jurisdiction}.json"
    if not path.is_file():
        return CourtRegister(jurisdiction=jurisdiction)
    register = CourtRegister.model_validate(json.loads(path.read_text()))
    if register.jurisdiction != jurisdiction:
        raise ValueError(f"{path.name} is the register of {register.jurisdiction}")
    return register


def written(root: Path | None = None) -> list[CourtRegister]:
    """Every register written, by jurisdiction."""
    return [load(path.stem, root) for path in sorted((root or COURTS).glob("*.json"))]


def answers(register: CourtRegister) -> bool:
    """Whether the register's relations answer at all: once reviewed, and with a precedent rule."""
    return register.review_state == "reviewed" and register.precedent is not None


def answering(
    court_id: str, decided_in: str, asked: str, registers: Callable[[str], CourtRegister | None], on: date
) -> CourtRegister | None:
    """The register that says how a decision of ``court_id``, a court of ``decided_in``, bears on a question
    of ``asked``'s law: ``asked``'s own where it lists the court; else, for a court of a regional or
    continental body, that body's where ``asked`` is a member state its court reaches on ``on``."""
    for code in dict.fromkeys((asked, decided_in)):
        register = registers(code)
        if register is None or court_id not in {c.id for c in register.courts}:
            continue
        if code == asked or register.reaches(asked, court_id, on):
            return register
    return None


def relation(
    register: CourtRegister,
    court_id: str,
    queried_jurisdiction: str,
    *,
    before: str | None = None,
    matter: str | None = None,
    on: date | None = None,
) -> Answer:
    """How a decision of ``court_id`` bears on a question of ``queried_jurisdiction``'s law: in the
    court ``before`` it is cited in, else on that jurisdiction's courts generally, on a question of
    ``matter`` where one is known, on the date ``on`` (today when not given).

    The register answers for its own jurisdiction, and a regional or continental register for the courts
    of a state it lists as a member on that date (``before``, a court of that state, is then covered by
    the relation to member states' courts). A relation limited to a matter answers only a question of
    that matter, and before any relation not limited. ``unknown`` where the register is unreviewed,
    records no precedent rule, lists no such court or relation, or the court ``before`` has no role on
    that date."""
    on = on or date.today()
    courts = {c.id: c for c in register.courts}
    if not answers(register) or court_id not in courts:
        return "unknown"
    if register.jurisdiction == queried_jurisdiction:
        if before is not None and (before not in courts or not courts[before].applies(on)):
            return "unknown"
        found = [
            r for r in register.relations
            if r.court == court_id and not r.members and r.bound == before and r.applies(on)
        ]  # fmt: skip
    elif register.reaches(queried_jurisdiction, court_id, on):
        found = [r for r in register.relations if r.court == court_id and r.members and r.applies(on)]
    else:
        return "unknown"
    scoped = [r for r in found if matter is not None and r.matter == matter]
    found = scoped or [r for r in found if r.matter is None]
    return found[0].relation if len(found) == 1 else "unknown"


def bearings(register: CourtRegister, court_id: str, on: date) -> tuple[Relation, ...]:
    """Every relation of ``court_id``'s decisions to other courts that applies on ``on``, as a reviewed
    register records it; none from a register that does not answer."""
    if not answers(register):
        return ()
    return tuple(r for r in register.relations if r.court == court_id and r.applies(on))


def public(register: CourtRegister) -> CourtRegisterView:
    """The register as REST v1 serves it: each entry with its review state and sources, and each relation
    with what is answered for it, ``unknown`` until the register is reviewed."""
    sources = {s.id: s for s in register.sources}
    state = register.review_state

    def entry(item, *, drop=()) -> dict:
        evidence = [
            CourtEvidence(title=sources[i].title, url=str(sources[i].url), pinpoint=sources[i].pinpoint)
            for i in item.sources
        ]
        return item.model_dump(exclude={"sources", *drop}) | {"review_state": state, "evidence": evidence}

    precedent = register.precedent
    return CourtRegisterView(
        jurisdiction=register.jurisdiction,
        review_state=state,
        reviewed_by=register.reviewed_by,
        reviewed_on=register.reviewed_on,
        precedent=CourtPrecedentView(**entry(precedent)) if precedent else None,
        courts=[CourtLevelView(**entry(c)) for c in register.courts],
        relations=[
            CourtRelationView(
                **entry(r, drop={"relation"}),
                recorded=r.relation,
                relation=r.relation if answers(register) else "unknown",
            )
            for r in register.relations
        ],
        matters=[CourtMatterView(**entry(m)) for m in register.matters],
        members=[CourtMemberView(**entry(m)) for m in register.members],
        member_routes=[CourtRouteView(**entry(r)) for r in register.member_routes],
        declarations=[CourtDeclarationView(**entry(d)) for d in register.declarations],
    )


def summary(register: CourtRegister) -> CourtRegisterSummary:
    return CourtRegisterSummary(
        jurisdiction=register.jurisdiction,
        review_state=register.review_state,
        reviewed_on=register.reviewed_on,
        courts=len(register.courts),
        relations=len(register.relations),
    )


def evidence_court(register: CourtRegister, court_id: str, on: date) -> EvidenceCourt:
    """A decision's court as an evidence unit gives it, from the register that answers for the question:
    with every relation of its decisions to other courts that applies on ``on``, once reviewed."""
    court = next(c for c in register.courts if c.id == court_id)
    urls = {s.id: str(s.url) for s in register.sources}
    return EvidenceCourt(
        id=court.id,
        name=court.name,
        jurisdiction=register.jurisdiction,
        review_state=register.review_state,
        reviewed_on=register.reviewed_on,
        bears_on=[
            EvidenceBearing(
                court=r.bound,
                members=r.members or None,
                matter=r.matter,
                relation=r.relation,
                valid_from=r.valid_from,
                valid_until=r.valid_until,
                evidence=[urls[i] for i in r.sources],
            )
            for r in bearings(register, court_id, on)
        ],
    )
