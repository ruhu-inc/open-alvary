"""Currentness of a unit at a date (RSV-08, IDT-03).

Until reviewed provision events exist for a work (CPR-02), the only evidence is the record's legal
status and the text version's effective dates: ``basis`` is then ``record`` and the state speaks for
the whole text, never for one provision. With no date, the record's status now decides; with a date,
the version's effective interval does, and a text with no effective date is ``unknown`` then.
"""

from datetime import date

from schema.public import Currentness

RECORD_ALGORITHM = "record-status-1"
_BY_STATUS = {"current": "in_force", "repealed": "not_in_force", "superseded": "not_in_force",
              "historical": "not_in_force"}  # fmt: skip


def record_currentness(legal_status: str, version, as_of: date | None, today: date) -> Currentness:
    reasons: list[str] = []
    if as_of is None:
        state = _BY_STATUS.get(legal_status, "unknown")
        if state == "unknown":
            reasons.append("The record's legal status is unknown.")
        if version.effective_until is not None and today >= version.effective_until and state == "in_force":
            state = "not_in_force"
            reasons.append("The text's effective period has ended.")
    elif version.effective_from is None:
        state = "unknown"
        reasons.append("The text has no effective date to compare with the date asked.")
    elif as_of < version.effective_from:
        state = "not_in_force"
        reasons.append("The date asked is before the text's effective date.")
    elif version.effective_until is not None and as_of >= version.effective_until:
        state = "not_in_force"
        reasons.append("The date asked is after the text's effective period.")
    else:
        state = "in_force"
    reasons.append("Assessed for the whole text from the record; no provision-level events are recorded.")
    return Currentness(
        state=state,
        basis="record",
        assessed_at=as_of or today,
        algorithm=RECORD_ALGORITHM,
        reasons=tuple(reasons),
    )
