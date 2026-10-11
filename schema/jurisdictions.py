"""Jurisdiction profiles: how law is made, published, numbered and cited in each place.

A profile is data, not code: adding a country, a state or a regional community is adding its entry
to jurisdictions.json. A profile says which legislature makes law there and through which steps
(each step becomes an expected event on a document record), which gazettes publish it, how
documents are numbered and cited, which languages are official and which courts decide cases.

Only facts confirmed from cited sources are recorded: an entry is ``researched`` once its
legislature, procedures, gazettes and courts have been confirmed; until then it carries only its
code, name and region, and documents from it are reviewed against the common standard alone.
"""

import json
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, model_validator

from schema.record import (
    COURT_LEVELS,
    EVENTS,
    PUBLICATION_KINDS,
    LanguageTag,
    PlaceCode,
    RecordModel,
    Short,
)

Key = Annotated[str, Field(pattern=r"^[a-z0-9_:-]{1,60}$")]
LEGAL_SYSTEMS = ("common_law", "civil_law", "islamic_law", "customary_law", "roman_dutch_law")


class Chamber(RecordModel):
    id: Key
    name: Short


class Legislature(RecordModel):
    name: Short
    chambers: tuple[Chamber, ...] = Field(min_length=1)
    note: Short | None = None  # e.g. a transitional legislature, with the date it was confirmed


class Step(RecordModel):
    """One step a document is expected to go through, fulfilled by an event with this step id."""

    id: Key  # "passed:senate", "certified", "assented"
    label: Short  # "Passed by the Senate"
    event: Literal[*EVENTS]
    chamber: Key | None = None
    signed_by: Short | None = None  # the office that signs at this step: "Clerk to the National Assembly"


class Procedure(RecordModel):
    applies_to: tuple[Key, ...] = Field(min_length=1)  # document types: "act", "constitutional_amendment"
    steps: tuple[Step, ...] = Field(min_length=1)
    authority: Short | None = None  # the law that sets the procedure: "Constitution, ss. 58 and 59"


class Gazette(RecordModel):
    name: Short
    kind: Literal[*PUBLICATION_KINDS]
    publisher: Short | None = None


class Numbering(RecordModel):
    applies_to: tuple[Key, ...] = Field(min_length=1)
    printed: Short  # how the number is printed: "Act No. {number}"
    citation: Short  # how the document is cited: "{title}, No. {number}, {year}"


class CourtEntry(RecordModel):
    name: Short
    level: Literal[*COURT_LEVELS]
    code: Annotated[str, Field(pattern=r"^[a-z0-9]{2,16}$")] | None = None  # neutral-citation court code


class JurisdictionProfile(RecordModel):
    code: PlaceCode
    name: Short
    names: dict[LanguageTag, Short] = {}  # the name in each official language
    level: Literal["continental", "regional", "national", "subnational", "local"]
    parent: PlaceCode | None = None
    au_region: Literal["central", "eastern", "northern", "southern", "western"] | None = None
    member_of: tuple[PlaceCode, ...] = ()
    researched: bool = False
    sources: tuple[HttpUrl, ...] = ()
    legal_systems: tuple[Literal[*LEGAL_SYSTEMS], ...] = ()
    official_languages: tuple[LanguageTag, ...] = ()
    calendars: tuple[Literal["gregorian", "ethiopian", "islamic"], ...] = ("gregorian",)
    legislature: Legislature | None = None
    procedures: tuple[Procedure, ...] = ()
    gazettes: tuple[Gazette, ...] = ()
    revised_editions: tuple[Short, ...] = ()  # "Laws of the Federation of Nigeria 2004"
    numbering: tuple[Numbering, ...] = ()
    courts: tuple[CourtEntry, ...] = ()
    note: Short | None = None

    @model_validator(mode="after")
    def sourced(self):
        if self.researched and not self.sources:
            raise ValueError(f"{self.code}: a researched profile cites its sources")
        chambers = {c.id for c in self.legislature.chambers} if self.legislature else set()
        for procedure in self.procedures:
            for step in procedure.steps:
                if step.chamber and step.chamber not in chambers:
                    raise ValueError(f"{self.code}: step {step.id} names an unknown chamber {step.chamber}")
        return self


def registry() -> dict[str, JurisdictionProfile]:
    data = json.loads(files("schema").joinpath("jurisdictions.json").read_text())
    profiles = [JurisdictionProfile.model_validate(entry) for entry in data["jurisdictions"]]
    out = {p.code: p for p in profiles}
    if len(out) != len(profiles):
        raise ValueError("jurisdiction codes must be unique")
    for p in profiles:
        for code in (p.parent, *p.member_of):
            if code and code not in out:
                raise ValueError(f"{p.code} refers to unknown jurisdiction {code}")
    return out


def expected_steps(profile: JurisdictionProfile | None, document_type: str) -> tuple[str, ...]:
    """The step ids a document of this type is expected to show in this jurisdiction."""
    if not profile:
        return ()
    for procedure in profile.procedures:
        if document_type in procedure.applies_to:
            return tuple(step.id for step in procedure.steps)
    return ()
