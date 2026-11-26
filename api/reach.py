"""How far an organisation's plan reaches (owner decision, 2026-10-16).

A plan names countries, or covers them all; to users they are the plan's "jurisdictions", because
"country" in the console is where an organisation is based. A country includes everything under it: Nigeria
includes Lagos State (AKN ``ng-la``). The continent's and the regional bodies' law (AKN ``aa-…``:
the African Union, the EAC, ECOWAS, OHADA and the others) is within every plan, because national
law cannot be read without it.

On a paid plan nothing is refused for lying outside the plan. Such a request also draws on an
allowance of its own at the free Starter rate, so a paid plan buys speed and volume.

Starter covers one country (owner decision, 2026-11-25): the plan's default
(``allowance.default_jurisdiction``) until an owner changes it in the console. A Starter key that asks
about another country is refused (``jurisdiction_not_included``), as is any country when the plan has
no default and none is chosen; the website still shows every country to everyone.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache

from schema.jurisdictions import registry as jurisdictions_registry

OUTSIDE = (
    "Outside your plan's jurisdictions, requests run at the free Starter rate, and that is used up for now. "
    "Try again shortly, or change your plan's jurisdictions in the console."
)


NOT_INCLUDED = (
    "Starter covers one jurisdiction (currently {jurisdictions}) plus continental and regional instruments "
    "(aa-*), and this request is about another jurisdiction. An owner can change the jurisdiction or plan "
    "under Plan and limits at https://open.alvary.ai/console/access."
)
NOT_CHOSEN = (
    "Starter covers one jurisdiction, and none is chosen yet. An owner chooses it under Plan and limits at "
    "https://open.alvary.ai/console/access; then this key reads that jurisdiction's law."
)


@lru_cache(maxsize=1)
def _jurisdiction_names() -> dict[str, str]:
    return {code.lower(): profile.name for code, profile in jurisdictions_registry().items()}


def format_jurisdictions(codes: Iterable[str]) -> str:
    """Format jurisdiction codes for user-facing notices and refusals."""
    names = _jurisdiction_names()
    unique = sorted({code.lower() for code in codes})
    return "; ".join(f"{names[code]}, {code}" if code in names else code.upper() for code in unique)


class OutsidePlanLimited(Exception):
    """The Starter-rate allowance for requests outside the plan is used up for now."""

    def __init__(self, retry_after: int):
        super().__init__(OUTSIDE)
        self.retry_after = retry_after


class OutsidePlan(Exception):
    """A Starter key asked about a jurisdiction that is not its own, or before one was chosen."""


def starter_only(scope: dict | None) -> Callable[[], None]:
    """What a Starter key's reach does with a request outside its jurisdiction: refuse, saying which case."""
    jurisdictions = (scope or {}).get("jurisdictions")
    message = (
        NOT_INCLUDED.format(jurisdictions=format_jurisdictions(jurisdictions))
        if jurisdictions
        else NOT_CHOSEN
    )

    def refuse() -> None:
        raise OutsidePlan(message)

    return refuse


def country(jurisdiction_id: str) -> str | None:
    """The country a jurisdiction belongs to; None for the continent and the regional bodies."""
    j = jurisdiction_id.lower()
    return None if j == "aa" or j.startswith("aa-") else j.split("-", 1)[0]


def _always() -> None:
    return None


@dataclass(frozen=True)
class Reach:
    countries: frozenset[str] | None = None  # None: every country at the plan's own limits
    # Called for a request outside the countries: raises OutsidePlanLimited (a paid plan, once the
    # Starter-rate allowance is used up) or OutsidePlan (Starter, always).
    outside: Callable[[], None] = field(default=_always, compare=False)

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

    def active_jurisdictions(self) -> tuple[str, ...] | None:
        return None if self.countries is None else tuple(sorted(self.countries))

    def within(self, jurisdiction_ids: Iterable[str]) -> frozenset[str] | None:
        """What a search or listing without a jurisdiction covers: None for every jurisdiction."""
        if self.countries is None:
            return None
        if not self.countries:
            self.outside()  # Starter before its country is chosen: there is no country to search
        return frozenset(j for j in jurisdiction_ids if self.covers(j))


EVERYWHERE = Reach()
