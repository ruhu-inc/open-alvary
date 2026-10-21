"""Dates known to a day, a month, a year or a window (an EDTF subset): each is one day between its bounds,
never filled in, and compared with a day it answers True, False or unknown (None). A window is written
[A..B], EDTF's one of a set; A/B, an EDTF interval, is refused."""

import re
from datetime import date

import pytest
from pydantic import BaseModel

from schema.dates import UncertainDate, within


def test_each_form_has_its_precision_and_inclusive_bounds():
    for text, earliest, latest, precision in [
        ("2001-05-26", date(2001, 5, 26), date(2001, 5, 26), "day"),
        ("2000-12", date(2000, 12, 1), date(2000, 12, 31), "month"),
        ("2000-02", date(2000, 2, 1), date(2000, 2, 29), "month"),
        ("1976", date(1976, 1, 1), date(1976, 12, 31), "year"),
        ("[1976..1977]", date(1976, 1, 1), date(1977, 12, 31), "range"),
        ("[2000-11..2000-12]", date(2000, 11, 1), date(2000, 12, 31), "range"),
        ("[2011-07-26..2011-07-27]", date(2011, 7, 26), date(2011, 7, 27), "range"),
        ("[2016-08-15..2016-09-05]", date(2016, 8, 15), date(2016, 9, 5), "range"),
    ]:
        value = UncertainDate(text)
        assert (value.earliest, value.latest, value.precision) == (earliest, latest, precision)
        assert str(value) == text
    assert UncertainDate.parse(date(2001, 5, 26)) == UncertainDate("2001-05-26")


@pytest.mark.parametrize(
    "text",
    ["2000-13", "2001-02-29", "2001-5-26", "01-05-26", "2001-05-26T00:00", "2000-12?", "2000~", "2000-XX",
     "[..2000]", "[2000..]", "[2001..2000]", "[2016-08-15..2016-08-15]", "[1976,1977]", "{1976..1977}",
     "[1976..1977", "1976..1977", "[2016-08..2016-08-15]", "[1976..1977-06]", ""],
)  # fmt: skip
def test_forms_outside_the_subset_are_refused(text):
    with pytest.raises(ValueError):
        UncertainDate(text)


@pytest.mark.parametrize("text", ["1976/1977", "2016-08-15/2016-09-05", "../2000", "2000/.."])
def test_a_slash_interval_is_refused_with_the_window_form_to_write_instead(text):
    """In EDTF ``A/B`` is an interval, every day from A to B: never read as one day within a window."""
    with pytest.raises(
        ValueError, match=re.escape("A/B is an EDTF interval; write one day within a window as [A..B]")
    ):
        UncertainDate(text)


def test_comparisons_are_unknown_inside_the_window_and_definite_outside_it():
    december = UncertainDate("2000-12")
    assert december.on_or_before(date(2000, 12, 31)) is True
    assert december.on_or_before(date(2001, 1, 1)) is True
    assert december.on_or_before(date(2000, 11, 30)) is False
    assert december.on_or_before(date(2000, 12, 15)) is None
    assert december.on_or_after(date(2000, 12, 1)) is True and december.on_or_after(date(2001, 1, 1)) is False
    assert december.on_or_after(date(2000, 12, 2)) is None
    day = UncertainDate("2001-05-26")
    assert day.on_or_before(date(2001, 5, 26)) is True and day.on_or_after(date(2001, 5, 26)) is True


def test_a_day_within_a_period_is_three_valued():
    start, end = UncertainDate("1975-05-28"), UncertainDate("2000-12")
    assert within(date(1990, 1, 1), start, end) is True
    assert within(date(2000, 12, 15), start, end) is None  # inside the uncertain end
    assert within(date(2001, 1, 1), start, end) is False
    assert within(date(1975, 5, 27), start, end) is False
    assert within(date(2026, 1, 1), start, None) is True  # not ended
    ranged = UncertainDate("[2011-07-27..2011-08-15]")
    assert within(date(2011, 8, 1), ranged, None) is None
    assert within(date(2011, 8, 15), ranged, None) is True
    assert within(date(2011, 7, 26), ranged, None) is False
    years = UncertainDate("[1976..1977]")  # one day of 1976 or 1977, as an end too
    assert [
        within(day, start, years) for day in (date(1975, 12, 31), date(1977, 6, 1), date(1978, 1, 1))
    ] == [
        True,
        None,
        False,
    ]


def test_models_read_and_write_the_edtf_string():
    class Row(BaseModel):
        on: UncertainDate | None = None

    window = Row.model_validate({"on": "[2016-08-15..2016-09-05]"})
    assert window.model_dump(mode="json") == {"on": "[2016-08-15..2016-09-05]"}
    assert window.on.bounds().model_dump(mode="json") == {
        "earliest": "2016-08-15",
        "latest": "2016-09-05",
        "precision": "range",
    }
    row = Row.model_validate({"on": "2000-12"})
    assert row.on.precision == "month" and row.model_dump(mode="json") == {"on": "2000-12"}
    assert Row.model_validate(row.model_dump()) == row
    assert row.on.bounds().model_dump(mode="json") == {
        "earliest": "2000-12-01",
        "latest": "2000-12-31",
        "precision": "month",
    }
    schema = Row.model_json_schema()["properties"]["on"]["anyOf"][0]
    assert schema["type"] == "string"
    pattern = re.compile(schema["pattern"])
    assert all(pattern.match(t) for t in ("1976", "2000-12", "2001-05-26", "[1976..1977]"))
    assert not pattern.match("1976/1977")
