"""Dates known to a day, a month, a year or a window of days: an ISO 8601-2 (EDTF level 1) subset.

A legal date is often known only to a month or a year, or its sources disagree by a few weeks. Such a
date is still one day, unknown which, between its earliest and latest bounds (both inclusive):

  ``2001-05-26``             day: that day
  ``2000-12``                month: a day of December 2000
  ``1976``                   year: a day of 1976
  ``2016-08-15/2016-09-05``  range: a day from 15 August to 5 September 2016, unknown which

The range's slash names a window holding one day, not a period lasting from one day to the other.
Nothing is filled in: compared with a day, an uncertain date answers True, False, or None (unknown) when
the day falls inside its window. Qualified (``?``, ``~``), open (``..``) and unspecified (``X``) forms
are not in the subset. Serialised as the EDTF string; ``bounds()`` gives the bounds for API answers.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic_core import core_schema

PRECISIONS = ("day", "month", "year", "range")
Precision = Literal[*PRECISIONS]
_PART = r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?"
PATTERN = rf"^{_PART}(?:/{_PART})?$"


def _part(text: str) -> tuple[date, date, Precision]:
    year, *rest = (int(number) for number in text.split("-"))
    if not rest:
        return date(year, 1, 1), date(year, 12, 31), "year"
    if len(rest) == 1:
        return date(year, rest[0], 1), date(year, rest[0], calendar.monthrange(year, rest[0])[1]), "month"
    day = date(year, *rest)
    return day, day, "day"


def _bounds(text: str) -> tuple[date, date, Precision]:
    if not isinstance(text, str) or not re.fullmatch(PATTERN, text):
        raise ValueError(
            f"{text!r} is not a date of the subset: YYYY-MM-DD, YYYY-MM, YYYY, or a window FIRST/LAST of them"
        )
    first, _, last = text.partition("/")
    try:
        start = _part(first)
        end = _part(last) if last else None
    except ValueError:
        raise ValueError(f"{text!r} is not a calendar date") from None
    if end is None:
        return start
    if not start[1] < end[0]:
        raise ValueError(f"{text!r}: a window's first date precedes its last")
    return start[0], end[1], "range"


class DateBounds(BaseModel):
    """An uncertain date's bounds in an answer: the earliest and the latest day it can be (inclusive), and
    its precision (``day``, ``month``, ``year`` or ``range``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    earliest: date
    latest: date
    precision: Precision


@dataclass(frozen=True)
class UncertainDate:
    """One day, known to a day, a month, a year or a window (module docstring). Build it from its EDTF
    string, or with ``parse`` from a string, a ``date`` (a day) or another UncertainDate."""

    edtf: str
    earliest: date = field(init=False)
    latest: date = field(init=False)
    precision: Precision = field(init=False)

    def __post_init__(self):
        for name, value in zip(("earliest", "latest", "precision"), _bounds(self.edtf), strict=True):
            object.__setattr__(self, name, value)

    @classmethod
    def parse(cls, value: Any) -> "UncertainDate":
        if isinstance(value, cls):
            return value
        if isinstance(value, date) and not isinstance(value, datetime):
            return cls(value.isoformat())
        return cls(value)

    def on_or_before(self, day: date) -> bool | None:
        """Whether this date is on or before ``day``; None when ``day`` falls inside its window."""
        if self.latest <= day:
            return True
        return False if self.earliest > day else None

    def on_or_after(self, day: date) -> bool | None:
        """Whether this date is on or after ``day``; None when ``day`` falls inside its window."""
        if self.earliest >= day:
            return True
        return False if self.latest < day else None

    def bounds(self) -> DateBounds:
        return DateBounds(earliest=self.earliest, latest=self.latest, precision=self.precision)

    def __str__(self) -> str:
        return self.edtf

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls.parse, serialization=core_schema.plain_serializer_function_ser_schema(str)
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler) -> dict:
        return {"type": "string", "pattern": PATTERN}


def within(day: date, start: UncertainDate, end: UncertainDate | None) -> bool | None:
    """Whether ``day`` falls in the inclusive period from ``start`` to ``end`` (None: not ended): True,
    False, or None when it falls inside the window of an uncertain start or end."""
    began = start.on_or_before(day)
    lasted = True if end is None else end.on_or_after(day)
    if began is False or lasted is False:
        return False
    return True if began and lasted else None
