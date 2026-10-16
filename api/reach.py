"""How far an organisation's plan reaches (owner decision, 2026-10-16).

A plan names countries, or covers them all. A country includes everything under it: Nigeria
includes Lagos State (AKN ``ng-la``). The continent's and the regional bodies' law (AKN ``aa-…``:
the African Union, the EAC, ECOWAS, OHADA and the others) is within every plan, because national
law cannot be read without it.

Nothing is refused for lying outside the plan. Such a request also draws on an allowance of its own
at the free Starter rate, so a plan buys speed and volume, never permission to read the law.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

OUTSIDE = (
    "Outside your plan's countries, requests run at the free Starter rate, and that is used up for now. "
    "Try again shortly, or change your plan's countries in the console."
)


class OutsidePlanLimited(Exception):
    """The Starter-rate allowance for requests outside the plan is used up for now."""

    def __init__(self, retry_after: int):
        super().__init__(OUTSIDE)
        self.retry_after = retry_after


def country(jurisdiction_id: str) -> str | None:
    """The country a jurisdiction belongs to; None for the continent and the regional bodies."""
    j = jurisdiction_id.lower()
    return None if j == "aa" or j.startswith("aa-") else j.split("-", 1)[0]


def _always() -> None:
    return None


@dataclass(frozen=True)
class Reach:
    countries: frozenset[str] | None = None  # None: every country at the plan's own limits
    outside: Callable[[], None] = field(default=_always, compare=False)  # raises OutsidePlanLimited

    @classmethod
    def of(cls, scope: dict | None, outside: Callable[[], None] = _always) -> "Reach":
        if not scope or scope.get("all"):
            return cls(None, outside)
        return cls(frozenset(c.lower() for c in scope.get("jurisdictions") or ()), outside)

    def covers(self, jurisdiction_id: str) -> bool:
        if self.countries is None:
            return True
        found = country(jurisdiction_id)
        return found is None or found in self.countries

    def admit(self, *jurisdiction_ids: str) -> None:
        """Draw once on the outside allowance when any of these lies outside the plan."""
        if any(not self.covers(j) for j in jurisdiction_ids):
            self.outside()

    def within(self, jurisdiction_ids: Iterable[str]) -> frozenset[str] | None:
        """What a search or listing without a jurisdiction covers: None for every jurisdiction."""
        if self.countries is None:
            return None
        return frozenset(j for j in jurisdiction_ids if self.covers(j))


EVERYWHERE = Reach()
