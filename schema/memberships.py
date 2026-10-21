"""Membership of the continental and regional bodies: dated, evidenced, reviewed assertions (JUR-01, JUR-04).

Each row is one relationship of the scope register's design (continental-scope.md, Relationship, kind
``member_of``): the member, the body (``target_id``), the dates it held from and until, the evidence
and a note. Dates are inclusive; ``valid_to`` null means it has not ended. Research records a row as a
``proposal``: dates it could not evidence stay null, with a note saying so. Only the reviewer's approval
(``python -m operations.memberships review``) makes a row ``reviewed``, recording who and when, and a
reviewed row is always dated. Only reviewed rows are published to the catalogue and served; a proposal
is never a fact, so a jurisdiction with no reviewed membership answers ``unknown``.

Membership is one relationship among several (signature, ratification, accession, withdrawal …). It
never applies a body's treaties to its members, and it is not a parent edge: a member is not inside the
body. Membership is data, not code: schema/memberships.json, validated against membership.schema.json.
"""

import json
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from schema.jurisdictions import JurisdictionProfile, registry
from schema.record import PlaceCode, RecordModel, Short

STANDARD = "memberships-1"
BODY_LEVELS = ("continental", "regional")
# The member, the body and the period's number among their memberships: "ml:aa-ecowas:1".
MembershipID = Annotated[
    str, Field(pattern=r"^[a-z]{2}(?:-[a-z0-9]{1,12})?:aa-[a-z0-9]{1,12}:[1-9][0-9]{0,2}$")
]
Evidence = Annotated[str, Field(pattern=r"^https://", max_length=2000)]
Note = Annotated[str, Field(min_length=1, max_length=2000)]


class Membership(RecordModel):
    id: MembershipID
    subject_id: PlaceCode  # the member
    target_id: PlaceCode  # the body
    kind: Literal["member_of"] = "member_of"
    status: Literal["proposal", "reviewed"]
    valid_from: date | None
    valid_to: date | None
    evidence_url: Evidence
    note: Note
    reviewed_by: Short | None = None
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent(self):
        if not self.id.startswith(f"{self.subject_id}:{self.target_id}:"):
            raise ValueError(f"{self.id}: the ID starts with the member and the body")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError(f"{self.id}: valid_to precedes valid_from")
        if self.status == "reviewed":
            if not (self.reviewed_by and self.reviewed_at):
                raise ValueError(f"{self.id}: a reviewed membership names who reviewed it and when")
            if self.valid_from is None:
                raise ValueError(f"{self.id}: a reviewed membership is dated; review it with its start date")
        elif self.reviewed_by or self.reviewed_at:
            raise ValueError(f"{self.id}: a proposal has not been reviewed")
        return self

    def holds_on(self, day: date) -> bool:
        """Whether a reviewed membership held on this day (inclusive bounds)."""
        return (
            self.valid_from is not None
            and self.valid_from <= day
            and (self.valid_to is None or day <= self.valid_to)
        )


class MembershipRegister(RecordModel):
    standard: Literal["memberships-1"] = STANDARD
    description: Short
    memberships: tuple[Membership, ...] = ()

    def check(self, profiles: dict[str, JurisdictionProfile]) -> "MembershipRegister":
        """Cross-row rules: unique IDs, known places, a body as the target, and no two reviewed periods
        of the same membership overlapping."""
        seen: set[str] = set()
        for row in self.memberships:
            if row.id in seen:
                raise ValueError(f"{row.id}: membership IDs must be unique")
            seen.add(row.id)
            for code in (row.subject_id, row.target_id):
                if code not in profiles:
                    raise ValueError(f"{row.id} refers to unknown jurisdiction {code}")
            if profiles[row.target_id].level not in BODY_LEVELS:
                raise ValueError(f"{row.id}: {row.target_id} is not a continental or regional body")
            if profiles[row.subject_id].level in BODY_LEVELS:
                raise ValueError(f"{row.id}: a body's relation to another body is not membership")
        reviewed = sorted(
            (r for r in self.memberships if r.status == "reviewed"),
            key=lambda r: (r.subject_id, r.target_id, r.valid_from),
        )
        for before, after in zip(reviewed, reviewed[1:], strict=False):
            same = (before.subject_id, before.target_id) == (after.subject_id, after.target_id)
            if same and (before.valid_to is None or after.valid_from <= before.valid_to):
                raise ValueError(f"{before.id} and {after.id}: reviewed periods overlap")
        return self

    def reviewed(self) -> tuple[Membership, ...]:
        return tuple(r for r in self.memberships if r.status == "reviewed")


def path() -> Path:
    return Path(str(files("schema").joinpath("memberships.json")))


def register(source: Path | None = None) -> MembershipRegister:
    """The membership register, checked against the jurisdiction profiles."""
    data = json.loads((source or path()).read_text())
    return MembershipRegister.model_validate(data).check(registry())


def write(value: MembershipRegister, target: Path | None = None) -> None:
    value.check(registry())
    text = json.dumps(value.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    (target or path()).write_text(text)
