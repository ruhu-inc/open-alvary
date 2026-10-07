"""Safe immutable release IDs, preserving legacy date-only links."""

import re
from datetime import date

RELEASE_ID = re.compile(r"(?P<day>\d{4}-\d{2}-\d{2})(?:\.(?P<revision>[1-9]\d{0,8}))?\Z")


def validate_release_id(value):
    match = RELEASE_ID.fullmatch(value)
    if match is None:
        raise ValueError("Invalid release ID")
    date.fromisoformat(match["day"])
    return value
