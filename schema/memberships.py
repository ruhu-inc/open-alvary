"""Membership of the continental and regional bodies: dated, evidenced, reviewed assertions (JUR-01, JUR-04).

Each row is one relationship of the scope register's design (continental-scope.md, Relationship, kind
``member_of``): the member, the body (``target_id``), the dates it held from and until, why it starts
when it does (``basis``), the evidence and a note. Dates are inclusive and may be uncertain
(schema.dates: a day, a month, a year or a window); ``valid_to`` null means it has not ended. Research
records a row as a ``proposal``: dates or a basis it could not evidence stay null, with a note saying
so. Only the reviewer's approval (``python -m operations.memberships review``) makes a row ``reviewed``,
recording who and when, and a reviewed row is always dated and states its basis. Only reviewed rows are
published to the catalogue and served; a proposal is never a fact, so a jurisdiction with no reviewed
membership answers ``unknown``.

A suspension from participation (kind ``suspended_from_participation``) is a dated relationship overlaid
on one membership, with its legal basis and evidence. It never ends the membership and never changes
whether it is held: it is served beside the membership, as context.

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

from schema.dates import UncertainDate, within
from schema.jurisdictions import JurisdictionProfile, registry
from schema.record import PlaceCode, RecordModel, Short

STANDARD = "memberships-2"
BODY_LEVELS = ("continental", "regional")
# The member, the body and the period's number among their memberships: "ml:aa-ecowas:1".
MEMBERSHIP = r"[a-z]{2}(?:-[a-z0-9]{1,12})?:aa-[a-z0-9]{1,12}:[1-9][0-9]{0,2}"
MembershipID = Annotated[str, Field(pattern=rf"^{MEMBERSHIP}$")]
# A suspension's membership and its number among that membership's suspensions: "ml:aa-au:1:s2".
SuspensionID = Annotated[str, Field(pattern=rf"^{MEMBERSHIP}:s[1-9][0-9]{{0,2}}$")]
Evidence = Annotated[str, Field(pattern=r"^https://", max_length=2000)]
Note = Annotated[str, Field(min_length=1, max_length=2000)]
Quote = Annotated[str, Field(min_length=1, max_length=400)]
# Why a membership starts when it does: the instrument's entry into force for the state, the state's
# deposit of its instrument, the body's decision admitting it, the instrument's provisional application
# from signature, or signature itself (only where the instrument makes signature binding).
BASES = (
    "entry_into_force",
    "deposit",
    "admission_decision",
    "provisional_application_on_signature",
    "signature",
)
# Why a suspension's legal basis cites an article: the decision cites it as the basis it acts on (its
# evidence is the decision, its quote the decision's words), or it is the treaty's rule the suspension
# applies (its evidence is the treaty, its quote the article's words), whether or not the decision names it.
ROLES = ("decision_basis", "treaty_rule")


class Basis(RecordModel):
    """Why a membership starts when it does: the kind of event, the article of the instrument that makes
    it the start (``pinpoint``, such as ``art. 28``), where that article was read and, optionally, its
    words."""

    kind: Literal[*BASES]
    instrument: Short
    pinpoint: Short
    evidence_url: Evidence
    quote: Quote | None = None


class Provision(RecordModel):
    """An article a suspension rests on, as cited: the instrument, the article (``pinpoint``), why it is
    cited (``role``: ``decision_basis`` or ``treaty_rule``), where it was read and, optionally, the words
    read there. A proposal may leave the role unstated, as registers written before roles do; a reviewed
    suspension states each."""

    instrument: Short
    pinpoint: Short
    role: Literal[*ROLES] | None = None
    evidence_url: Evidence
    quote: Quote | None = None


class Membership(RecordModel):
    id: MembershipID
    subject_id: PlaceCode  # the member
    target_id: PlaceCode  # the body
    kind: Literal["member_of"] = "member_of"
    status: Literal["proposal", "reviewed"]
    valid_from: UncertainDate | None
    valid_to: UncertainDate | None
    basis: Basis | None
    evidence_url: Evidence
    note: Note
    reviewed_by: Short | None = None
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent(self):
        if not self.id.startswith(f"{self.subject_id}:{self.target_id}:"):
            raise ValueError(f"{self.id}: the ID starts with the member and the body")
        if self.valid_from and self.valid_to and self.valid_to.latest < self.valid_from.earliest:
            raise ValueError(f"{self.id}: valid_to precedes valid_from")
        if self.status == "reviewed":
            if not (self.reviewed_by and self.reviewed_at):
                raise ValueError(f"{self.id}: a reviewed membership names who reviewed it and when")
            if self.valid_from is None:
                raise ValueError(f"{self.id}: a reviewed membership is dated; review it with its start date")
            if self.basis is None:
                raise ValueError(f"{self.id}: a reviewed membership states its basis, with its article")
        elif self.reviewed_by or self.reviewed_at:
            raise ValueError(f"{self.id}: a proposal has not been reviewed")
        return self

    def holds_on(self, day: date) -> bool | None:
        """Whether the membership held on this day (inclusive bounds): None when the day falls inside an
        uncertain start or end, or the membership has no start yet."""
        return None if self.valid_from is None else within(day, self.valid_from, self.valid_to)


class Suspension(RecordModel):
    """A member suspended from participating in a body's activities, overlaid on one membership: its first
    and last day (``valid_to`` null: not lifted), its legal basis (the article the decision cites and,
    where there is one, the treaty's rule it applies), the decision that suspended it and the one that
    lifted it. It never ends the membership."""

    id: SuspensionID
    membership_id: MembershipID
    kind: Literal["suspended_from_participation"] = "suspended_from_participation"
    status: Literal["proposal", "reviewed"]
    valid_from: UncertainDate | None
    valid_to: UncertainDate | None
    legal_basis: tuple[Provision, ...] = Field(max_length=5)
    evidence_url: Evidence  # the decision that suspended it
    lifted_evidence_url: Evidence | None = None  # the decision that lifted it
    note: Note
    reviewed_by: Short | None = None
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent(self):
        if not self.id.startswith(f"{self.membership_id}:s"):
            raise ValueError(f"{self.id}: the ID starts with its membership's")
        if self.valid_from and self.valid_to and self.valid_to.latest < self.valid_from.earliest:
            raise ValueError(f"{self.id}: valid_to precedes valid_from")
        if self.status == "reviewed":
            if not (self.reviewed_by and self.reviewed_at):
                raise ValueError(f"{self.id}: a reviewed suspension names who reviewed it and when")
            if self.valid_from is None or not self.legal_basis:
                raise ValueError(f"{self.id}: a reviewed suspension is dated and states its legal basis")
            if any(p.role is None for p in self.legal_basis):
                raise ValueError(f"{self.id}: a reviewed suspension states why it cites each article (role)")
            if not any(p.role == "decision_basis" for p in self.legal_basis):
                raise ValueError(f"{self.id}: a reviewed suspension cites the article its decision rests on")
            if self.valid_to is not None and self.lifted_evidence_url is None:
                raise ValueError(f"{self.id}: a lifted suspension cites the decision that lifted it")
        elif self.reviewed_by or self.reviewed_at:
            raise ValueError(f"{self.id}: a proposal has not been reviewed")
        return self

    @property
    def member_id(self) -> str:
        return self.membership_id.split(":")[0]

    @property
    def body_id(self) -> str:
        return self.membership_id.split(":")[1]


def _may_overlap(rows) -> tuple | None:
    """The first two periods (rows with valid_from, valid_to) that may overlap, in order of start."""
    ordered = sorted(rows, key=lambda r: (r.valid_from.earliest, r.valid_from.latest))
    for before, after in zip(ordered, ordered[1:], strict=False):
        if before.valid_to is None or after.valid_from.earliest <= before.valid_to.latest:
            return before, after
    return None


class MembershipRegister(RecordModel):
    standard: Literal["memberships-2"] = STANDARD
    description: Short
    memberships: tuple[Membership, ...] = ()
    suspensions: tuple[Suspension, ...] = ()

    def check(self, profiles: dict[str, JurisdictionProfile]) -> "MembershipRegister":
        """Cross-row rules: unique IDs, known places, a body as the target, no two reviewed periods of the
        same membership that may overlap, and each suspension on a membership of the register (a reviewed
        one on a reviewed membership, not certainly outside it)."""
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
        pairs: dict[tuple[str, str], list[Membership]] = {}
        for row in self.reviewed():
            pairs.setdefault((row.subject_id, row.target_id), []).append(row)
        for rows in pairs.values():
            if found := _may_overlap(rows):
                raise ValueError(f"{found[0].id} and {found[1].id}: reviewed periods overlap")
        memberships = {row.id: row for row in self.memberships}
        suspended: dict[str, list[Suspension]] = {}
        for row in self.suspensions:
            if row.id in seen:
                raise ValueError(f"{row.id}: suspension IDs must be unique")
            seen.add(row.id)
            membership = memberships.get(row.membership_id)
            if membership is None:
                raise ValueError(f"{row.id}: there is no membership {row.membership_id} in the register")
            if row.status != "reviewed":
                continue
            if membership.status != "reviewed":
                raise ValueError(f"{row.id}: a reviewed suspension is of a reviewed membership")
            if row.valid_from.latest < membership.valid_from.earliest or (
                membership.valid_to and membership.valid_to.latest < row.valid_from.earliest
            ):
                raise ValueError(f"{row.id}: the suspension begins outside its membership")
            suspended.setdefault(row.membership_id, []).append(row)
        for rows in suspended.values():
            if found := _may_overlap(rows):
                raise ValueError(f"{found[0].id} and {found[1].id}: reviewed suspensions overlap")
        return self

    def reviewed(self) -> tuple[Membership, ...]:
        return tuple(r for r in self.memberships if r.status == "reviewed")

    def reviewed_suspensions(self) -> tuple[Suspension, ...]:
        return tuple(r for r in self.suspensions if r.status == "reviewed")


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
