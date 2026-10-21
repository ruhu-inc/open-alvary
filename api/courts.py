"""The court hierarchy register (CPR-04): for one jurisdiction, each court and tribunal with its level and
rank, its appeal routes and the dates its role applies; how each court's decisions bear on the other
courts; whether precedent binds there at all; and the reviewed source of every entry. Read from
ingestion/courts/<jurisdiction>.json (see the README there).

A regional or continental body's register (JUR-04: ``aa-ohada``, ``aa-eac``, ``aa-au``) also lists the
states its courts reach and from when, the routes from their courts to its own (cassation in place of the
national court, references of a question of interpretation), and declarations accepting a court's
jurisdiction over cases from individuals. How its decisions bear on a member state's courts is a relation
like any other, limited to the subject matter a source gives: never a rank above national courts.

A relation says how one court's decisions bear on another court, in its source's own terms:
- ``binding``: precedent binds (stare decisis);
- ``prevails_on_matter``: a rule of supremacy or precedence on a subject matter, such as EAC Treaty art.
  33(2), which gives the East African Court of Justice's decisions on the Treaty "precedence over decisions
  of national courts on a similar matter"; not stare decisis, and never more than that matter;
- ``binding_in_referred_case``: the ruling binds the court that referred a question, in that case only;
- ``persuasive`` or ``not_applicable``.
Each keeps the verbatim words it rests on (``rests_on``), with their provision and source.

Nothing is guessed. Whether precedent binds is a recorded rule of the jurisdiction, never assumed from
its legal system. Every entry is reviewed on its own: courts, matters, members, routes and declarations
at the ``structural`` level, relations and the precedent rule at the ``doctrinal`` level, which states
the reviewer's legal qualification (operations.courts). A review covers the entry as it was reviewed:
one changed since (its ``reviewed_sha256`` no longer matching) is unreviewed again, on its own. A court,
relation or precedent rule the register does not give, or one not yet reviewed, answers ``unknown``.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import Field, HttpUrl, PrivateAttr, model_validator

from api.reach import country
from schema.public import (
    COURT_BEARINGS,
    CourtDeclarationView,
    CourtEvidence,
    CourtLevelView,
    CourtMatterView,
    CourtMemberView,
    CourtPrecedentView,
    CourtQuote,
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
Bearing = Literal[*COURT_BEARINGS]
Answer = Literal[*COURT_BEARINGS, "unknown"]
Level = Literal["structural", "doctrinal"]
# Relations that say how precedent works in the jurisdiction: they answer only once its precedent rule is
# reviewed too. A rule of precedence on a matter, or a ruling's force in the case referred, rests on its
# own provision instead; and that a court sits in another hierarchy (``not_applicable``) is a structural
# fact, answered once the relation and the courts it names are reviewed.
PRECEDENTIAL = frozenset({"binding", "persuasive"})


class Source(RecordModel):
    """Where an entry is confirmed: a constitution, an Act, a treaty, a practice direction, a court's own
    record, a reported decision."""

    id: Key
    title: Short
    url: HttpUrl
    pinpoint: Short | None = None  # "s. 287(1)", "art. 143"


class Quote(RecordModel):
    """The words of a source an entry rests on, verbatim, and the provision they are in."""

    source: Key
    provision: Short  # "art. 33(2)", "paras 38-39"
    quote: Text


class Review(RecordModel):
    """Who reviewed an entry, in what role and at what level, on which day, and the SHA-256 of the entry as
    they reviewed it (``digest``)."""

    review_level: Level
    reviewed_by: Short
    reviewer_role: Short  # "owner", "Kenyan advocate", "community-law academic"
    # "Advocate of the High Court of Kenya": a doctrinal review states one, whatever the role
    reviewer_qualification: Annotated[str, Field(max_length=300)] | None = None
    reviewed_on: date
    reviewed_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def qualified(self):
        if self.review_level == "doctrinal" and not self.reviewer_qualification:
            raise ValueError(
                f"{self.reviewed_by}: a doctrinal review states the reviewer's legal qualification"
            )
        return self


class Entry(RecordModel):
    """What every entry of a register carries: its own review, at its kind's level."""

    LEVEL: ClassVar[Level] = "structural"
    review: Review | None = None


class Dated(Entry):
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
    lists in ``members`` while it is one. ``matter`` limits it to one of the register's ``matters``, and a
    rule of precedence (``prevails_on_matter``) always names one. ``rests_on`` quotes its sources."""

    LEVEL: ClassVar[Level] = "doctrinal"
    id: Key
    court: Key
    bound: Key | None = None
    members: bool = False
    matter: Key | None = None
    relation: Bearing
    rests_on: tuple[Quote, ...] = Field(min_length=1)
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class PrecedentRule(Entry):
    """Whether decisions bind later courts in this jurisdiction at all (stare decisis), as its law says."""

    LEVEL: ClassVar[Level] = "doctrinal"
    id: Literal["precedent"] = "precedent"
    binds: bool
    note: Text  # "Decisions of the Supreme Court bind all other courts (Constitution, s. 287)."
    sources: tuple[Key, ...] = Field(min_length=1)


class Matter(Entry):
    """A subject matter a relation or route is limited to, as its source defines it: "the Uniform Acts and
    regulations, save decisions applying criminal sanctions" (OHADA Treaty, art. 14)."""

    id: Key
    name: Short
    sources: tuple[Key, ...] = Field(min_length=1)


class Member(Dated):
    """A state the register's courts reach: a member of (or party to) the body's treaty, or with ``court`` a
    party to that court's own instrument only; from the instrument's entry into force for the state to
    the first day its withdrawal no longer bound it."""

    id: Key
    state: PlaceCode
    court: Key | None = None
    withdrawal_notified: date | None = None
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class MemberRoute(Dated):
    """A route from the courts of each member state to a court of the register: ``cassation`` in place of
    the national court of last resort, ``appeal``, or a ``reference`` of a question of interpretation."""

    id: Key
    court: Key
    kind: Literal["cassation", "appeal", "reference"]
    matter: Key | None = None
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class Declaration(Dated):
    """A state's declaration accepting a court's jurisdiction over cases from individuals and NGOs (AfCHPR
    Protocol, art. 34(6)): deposited on ``valid_from``; withdrawn, deposited on ``withdrawal_deposited``
    and no longer applying from ``valid_until``."""

    id: Key
    state: PlaceCode
    court: Key
    withdrawal_deposited: date | None = None
    note: Text | None = None
    sources: tuple[Key, ...] = Field(min_length=1)


class CourtRegister(RecordModel):
    jurisdiction: PlaceCode
    precedent: PrecedentRule | None = None
    courts: tuple[Court, ...] = ()
    relations: tuple[Relation, ...] = ()
    matters: tuple[Matter, ...] = ()
    members: tuple[Member, ...] = ()
    member_routes: tuple[MemberRoute, ...] = ()
    declarations: tuple[Declaration, ...] = ()
    sources: tuple[Source, ...] = ()
    _reviewed: frozenset[str] = PrivateAttr(default=frozenset())  # the entries whose review still covers them

    @model_validator(mode="after")
    def consistent(self):
        where = self.jurisdiction
        courts = {c.id for c in self.courts}
        sources = {s.id for s in self.sources}
        matters = {m.id for m in self.matters}
        if (len(courts), len(sources), len(matters)) != tuple(
            map(len, (self.courts, self.sources, self.matters))
        ):
            raise ValueError(f"{where}: court, source and matter ids must be unique")
        entries = self.entries()
        if len({e.id for e in entries}) != len(entries):
            raise ValueError(f"{where}: entry ids must be unique across the register")
        for entry in entries:
            if unknown := set(entry.sources) - sources:
                raise ValueError(f"{where}: an entry cites unknown sources {sorted(unknown)}")
            if entry.review is not None and entry.review.review_level != entry.LEVEL:
                raise ValueError(f"{where}: {entry.id} is a {entry.LEVEL} entry, reviewed at another level")
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
            if relation.relation == "prevails_on_matter" and relation.matter is None:
                raise ValueError(f"{where}: {relation.id} prevails on a matter, yet names none")
            if unquoted := {q.source for q in relation.rests_on} - set(relation.sources):
                raise ValueError(f"{where}: {relation.id} quotes sources it does not cite {sorted(unquoted)}")
            # One relation with general effect and one binding only in a referred case may stand together.
            key = (relation.court, relation.bound, relation.members, relation.matter, case(relation))
            for other in self.relations[n + 1 :]:
                if (other.court, other.bound, other.members, other.matter, case(other)) == key and (
                    relation.overlaps(other)
                ):
                    bound = relation.bound or ("member states' courts" if relation.members else "its courts")
                    raise ValueError(f"{where}: two relations of {relation.court} to {bound} apply at once")
        for kind, items in (("membership", self.members), ("declaration", self.declarations)):
            for n, item in enumerate(items):
                for other in items[n + 1 :]:
                    if (other.state, other.court) == (item.state, item.court) and item.overlaps(other):
                        raise ValueError(f"{where}: two {kind}s of {item.state} apply at once")
        self._reviewed = frozenset(
            e.id for e in entries if e.review is not None and e.review.reviewed_sha256 == digest(self, e)
        )
        return self

    def entries(self) -> list[Entry]:
        """Every entry, each reviewed on its own: the precedent rule, then each list in the file's order."""
        return [
            *([self.precedent] if self.precedent else []), *self.courts, *self.relations, *self.matters,
            *self.members, *self.member_routes, *self.declarations,
        ]  # fmt: skip

    def reviewed(self, entry: Entry) -> bool:
        """Whether ``entry`` has a review that covers it as it is now."""
        return entry.id in self._reviewed

    def changed(self) -> list[Entry]:
        """Entries edited after their review: served as unreviewed until reviewed again or withdrawn."""
        return [e for e in self.entries() if e.review is not None and not self.reviewed(e)]

    def reaches(self, jurisdiction: str, court_id: str, on: date) -> bool:
        """Whether ``jurisdiction`` (or the country it is part of) is, on ``on``, a member state whose
        courts ``court_id``'s relations to member states' courts cover, by a reviewed membership."""
        state = country(jurisdiction)
        return any(
            m.state == state and m.court in (None, court_id) and m.applies(on) and self.reviewed(m)
            for m in self.members
        )


def case(relation: Relation) -> bool:
    """Whether the relation binds only the court that referred a question, in that case."""
    return relation.relation == "binding_in_referred_case"


def digest(register: CourtRegister, entry: Entry) -> str:
    """The SHA-256 of what a review of ``entry`` covers: the entry as written (not its review), with the
    sources it cites and the matter it is limited to, in its register. Fields left at their defaults are
    not part of it, so a field added to the format later leaves reviews standing."""
    sources = {s.id: s for s in register.sources}
    body = {
        "jurisdiction": register.jurisdiction,
        "entry": entry.model_dump(mode="json", exclude={"review"}, exclude_defaults=True),
        "sources": [sources[i].model_dump(mode="json", exclude_defaults=True) for i in entry.sources],
    }
    if (matter := getattr(entry, "matter", None)) is not None:
        found = next(m for m in register.matters if m.id == matter)
        body["matter"] = found.model_dump(mode="json", exclude={"review"}, exclude_defaults=True)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load(jurisdiction: str, root: Path | None = None) -> CourtRegister:
    """A jurisdiction's register; an empty one where none is written."""
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


def answered(register: CourtRegister, relation: Relation) -> Answer:
    """What a recorded relation answers: its value once it has its own doctrinal review and the courts it
    names have their structural ones, and for ``binding`` and ``persuasive`` once the precedent rule is
    reviewed too; ``unknown`` until then."""
    courts = {c.id: c for c in register.courts}
    named = [courts[relation.court], *([courts[relation.bound]] if relation.bound else [])]
    if not (register.reviewed(relation) and all(register.reviewed(c) for c in named)):
        return "unknown"
    rule = register.precedent
    if relation.relation in PRECEDENTIAL and not (rule is not None and register.reviewed(rule)):
        return "unknown"
    return relation.relation


def answering(
    court_id: str, decided_in: str, asked: str, registers: Callable[[str], CourtRegister | None], on: date
) -> CourtRegister | None:
    """The register that says how a decision of ``court_id``, a court of ``decided_in``, bears on a question
    of ``asked``'s law: ``asked``'s own where it lists the court; else, for a court of a regional or
    continental body, that body's where ``asked`` is a member state its court reaches on ``on`` by a
    reviewed membership. Which entries it answers from is each entry's own review (``relation``)."""
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
    of a state it lists as a member on that date by a reviewed membership (``before``, a court of that
    state, is then covered by the relation to member states' courts). A relation limited to a matter
    answers only a question of that matter, and before any relation not limited: so ``prevails_on_matter``
    answers only when the question's matter is known and is its own. A relation with general effect
    answers before one binding only in a referred case. ``unknown`` where the register lists no such court
    or relation, the court ``before`` has no role on that date, or the relation is not reviewed
    (``answered``)."""
    on = on or date.today()
    courts = {c.id: c for c in register.courts}
    if court_id not in courts:
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
    found = [r for r in found if not case(r)] or found
    return answered(register, found[0]) if len(found) == 1 else "unknown"


def bearings(register: CourtRegister, court_id: str, on: date) -> tuple[Relation, ...]:
    """Every relation of ``court_id``'s decisions to other courts that applies on ``on`` and answers what it
    records (``answered``): none that is not reviewed."""
    return tuple(
        r
        for r in register.relations
        if r.court == court_id and r.applies(on) and answered(register, r) != "unknown"
    )


def reviewed_fields(register: CourtRegister, entry: Entry) -> dict:
    """An entry's review as REST v1 serves it: its state and level, and who reviewed it once reviewed."""
    fields = {"review_level": entry.LEVEL, "review_state": "unreviewed"}
    if register.reviewed(entry):
        fields |= entry.review.model_dump(exclude={"review_level", "reviewed_sha256"}, exclude_none=True)
        fields["review_state"] = "reviewed"
    return fields


def quotes(register: CourtRegister, relation: Relation) -> list[CourtQuote]:
    sources = {s.id: s for s in register.sources}
    return [
        CourtQuote(
            quote=q.quote,
            provision=q.provision,
            title=sources[q.source].title,
            url=str(sources[q.source].url),
        )
        for q in relation.rests_on
    ]


def state(register: CourtRegister) -> Literal["unreviewed", "partly_reviewed", "reviewed"]:
    entries = register.entries()
    done = sum(register.reviewed(e) for e in entries)
    return "reviewed" if entries and done == len(entries) else "partly_reviewed" if done else "unreviewed"


def public(register: CourtRegister) -> CourtRegisterView:
    """The register as REST v1 serves it: each entry with its own review and sources, and each relation
    with what is answered for it, ``unknown`` until reviewed, and the words it rests on."""
    sources = {s.id: s for s in register.sources}

    def entry(item: Entry, *, drop=()) -> dict:
        evidence = [
            CourtEvidence(title=sources[i].title, url=str(sources[i].url), pinpoint=sources[i].pinpoint)
            for i in item.sources
        ]
        dumped = item.model_dump(exclude={"sources", "review", *drop})
        return dumped | reviewed_fields(register, item) | {"evidence": evidence}

    precedent = register.precedent
    return CourtRegisterView(
        jurisdiction=register.jurisdiction,
        review_state=state(register),
        precedent=CourtPrecedentView(**entry(precedent)) if precedent else None,
        courts=[CourtLevelView(**entry(c)) for c in register.courts],
        relations=[
            CourtRelationView(
                **entry(r, drop={"relation", "rests_on"}),
                recorded=r.relation,
                relation=answered(register, r),
                rests_on=quotes(register, r),
            )
            for r in register.relations
        ],
        matters=[CourtMatterView(**entry(m)) for m in register.matters],
        members=[CourtMemberView(**entry(m)) for m in register.members],
        member_routes=[CourtRouteView(**entry(r)) for r in register.member_routes],
        declarations=[CourtDeclarationView(**entry(d)) for d in register.declarations],
    )


def summary(register: CourtRegister) -> CourtRegisterSummary:
    entries = register.entries()
    return CourtRegisterSummary(
        jurisdiction=register.jurisdiction,
        review_state=state(register),
        courts=len(register.courts),
        relations=len(register.relations),
        entries=len(entries),
        reviewed_entries=sum(register.reviewed(e) for e in entries),
    )


def evidence_court(register: CourtRegister, court_id: str, on: date) -> EvidenceCourt:
    """A decision's court as an evidence unit gives it, from the register that answers for the question,
    with its own review, and every reviewed relation of its decisions to other courts that applies on
    ``on`` (``bearings``), with the words each rests on."""
    court = next(c for c in register.courts if c.id == court_id)
    urls = {s.id: str(s.url) for s in register.sources}
    return EvidenceCourt(
        id=court.id,
        name=court.name,
        jurisdiction=register.jurisdiction,
        review_state="reviewed" if register.reviewed(court) else "unreviewed",
        reviewed_on=court.review.reviewed_on if register.reviewed(court) else None,
        bears_on=[
            EvidenceBearing(
                id=r.id,
                court=r.bound,
                members=r.members or None,
                matter=r.matter,
                relation=r.relation,
                valid_from=r.valid_from,
                valid_until=r.valid_until,
                evidence=[urls[i] for i in r.sources],
                rests_on=quotes(register, r),
            )
            for r in bearings(register, court_id, on)
        ],
    )
