"""The court hierarchy register (CPR-04): for one jurisdiction, each court and tribunal with its level and
rank, its appeal routes and the dates its role applies; how each court's decisions bear on the other
courts (binding, persuasive or not applicable); whether precedent binds there at all; and the reviewed
source of every entry. Read from ingestion/courts/<jurisdiction>.json (see the README there).

Nothing is guessed. Whether precedent binds is a recorded rule of the jurisdiction, never assumed from
its legal system; a court, relation or precedent rule the register does not give, or a register not
yet reviewed, answers ``unknown``.
"""

import json
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, model_validator

from schema.record import COURT_LEVELS, PlaceCode, RecordModel, Short, Text

COURTS = Path(__file__).resolve().parents[1] / "ingestion" / "courts"
Key = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,59}$")]
Bearing = Literal["binding", "persuasive", "not_applicable"]
Answer = Literal["binding", "persuasive", "not_applicable", "unknown"]


class Source(RecordModel):
    """Where an entry is confirmed: a constitution, an Act, a practice direction, a reported decision."""

    id: Key
    title: Short
    url: HttpUrl
    pinpoint: Short | None = None  # "s. 287(1)", "art. 143"


class Dated(RecordModel):
    valid_from: date | None = None  # None: since before any date the register knows
    valid_until: date | None = None  # None: still

    def applies(self, on: date) -> bool:
        return (self.valid_from or date.min) <= on < (self.valid_until or date.max)

    def overlaps(self, other: "Dated") -> bool:
        start = max(self.valid_from or date.min, other.valid_from or date.min)
        return start < min(self.valid_until or date.max, other.valid_until or date.max)


class Court(Dated):
    id: Key
    name: Short
    kind: Literal["court", "tribunal"] = "court"
    level: Literal[*COURT_LEVELS]
    rank: int | None = Field(default=None, ge=1)  # 1 the highest; courts of equal standing share one
    jurisdiction: PlaceCode | None = None  # another jurisdiction's court the register lists: its code
    appeals_to: tuple[Key, ...] = ()
    sources: tuple[Key, ...] = Field(min_length=1)


class Relation(Dated):
    """How decisions of ``court`` bear on ``bound`` (a court of the register), or on the jurisdiction's
    courts generally when ``bound`` is None."""

    court: Key
    bound: Key | None = None
    relation: Bearing
    sources: tuple[Key, ...] = Field(min_length=1)


class PrecedentRule(RecordModel):
    """Whether decisions bind later courts in this jurisdiction at all (stare decisis), as its law says."""

    binds: bool
    note: Text  # "Decisions of the Supreme Court bind all other courts (Constitution, s. 287)."
    sources: tuple[Key, ...] = Field(min_length=1)


class CourtRegister(RecordModel):
    jurisdiction: PlaceCode
    review_state: Literal["unreviewed", "reviewed"] = "unreviewed"
    reviewed_by: Short | None = None
    reviewed_on: date | None = None
    precedent: PrecedentRule | None = None
    courts: tuple[Court, ...] = ()
    relations: tuple[Relation, ...] = ()
    sources: tuple[Source, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if self.review_state == "reviewed" and not (self.reviewed_by and self.reviewed_on):
            raise ValueError(f"{self.jurisdiction}: a reviewed register names its reviewer and date")
        courts = {c.id for c in self.courts}
        sources = {s.id for s in self.sources}
        if len(courts) != len(self.courts) or len(sources) != len(self.sources):
            raise ValueError(f"{self.jurisdiction}: court and source ids must be unique")
        entries = [*self.courts, *self.relations, *([self.precedent] if self.precedent else [])]
        for entry in entries:
            if unknown := set(entry.sources) - sources:
                raise ValueError(f"{self.jurisdiction}: an entry cites unknown sources {sorted(unknown)}")
        for court in self.courts:
            if unknown := set(court.appeals_to) - courts:
                raise ValueError(
                    f"{self.jurisdiction}: {court.id} appeals to unknown courts {sorted(unknown)}"
                )
        for n, relation in enumerate(self.relations):
            if relation.court not in courts or (relation.bound is not None and relation.bound not in courts):
                raise ValueError(f"{self.jurisdiction}: a relation names a court the register does not list")
            if relation.relation == "binding" and self.precedent is not None and not self.precedent.binds:
                raise ValueError(
                    f"{self.jurisdiction}: precedent does not bind here, yet {relation.court} binds"
                )
            for other in self.relations[n + 1 :]:
                same = (other.court, other.bound) == (relation.court, relation.bound)
                if same and relation.overlaps(other):
                    raise ValueError(
                        f"{self.jurisdiction}: two relations of {relation.court} to "
                        f"{relation.bound or 'its courts'} apply at once"
                    )
        return self


def load(jurisdiction: str, root: Path = COURTS) -> CourtRegister:
    """A jurisdiction's register; an empty, unreviewed one where none is written."""
    jurisdiction = CourtRegister(jurisdiction=jurisdiction).jurisdiction  # a place code, never a path
    path = root / f"{jurisdiction}.json"
    if not path.is_file():
        return CourtRegister(jurisdiction=jurisdiction)
    register = CourtRegister.model_validate(json.loads(path.read_text()))
    if register.jurisdiction != jurisdiction:
        raise ValueError(f"{path.name} is the register of {register.jurisdiction}")
    return register


def relation(
    register: CourtRegister,
    court_id: str,
    queried_jurisdiction: str,
    *,
    before: str | None = None,
    on: date | None = None,
) -> Answer:
    """How a decision of ``court_id`` bears on a question of ``queried_jurisdiction``'s law: in the
    court ``before`` it is cited in, else on that jurisdiction's courts generally, on the date ``on``
    (today when not given). The register answers for its own jurisdiction only; ``unknown`` where it is
    unreviewed, records no precedent rule, lists no such court or relation, or the court ``before``
    has no role on that date."""
    on = on or date.today()
    courts = {c.id: c for c in register.courts}
    if (
        register.review_state != "reviewed"
        or register.precedent is None
        or register.jurisdiction != queried_jurisdiction
        or court_id not in courts
        or (before is not None and (before not in courts or not courts[before].applies(on)))
    ):
        return "unknown"
    found = [
        r.relation for r in register.relations if r.court == court_id and r.bound == before and r.applies(on)
    ]
    return found[0] if len(found) == 1 else "unknown"
