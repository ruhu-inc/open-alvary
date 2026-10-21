"""Applicable law (JUR-03, JUR-04): how a record relates to the law of a country on a date, and the opt-in
scope that widens a search to the regional and treaty law that binds there (``scope=applicable``).

Nothing is decided that a reviewed record or a reviewed membership does not state:

* a country's own law is ``national``; another country's is ``comparative``;
* a treaty binds a state through that state's own ratification or accession on or before the date, not
  denounced by then, once the treaty itself is in force (``treaty_in_force_for_state``). A party list
  with the state's signature alone, a later ratification or a denunciation by then is
  ``treaty_not_in_force_for_state``; so is a list without the state, but only when the record says the
  list is complete as of the date or later (``parties_complete_as_of``, from the depositary's status
  list). Membership of a body never applies a treaty to its members (JUR-04);
* a body's instrument reaches a country only through a reviewed membership held on the date, and then by
  the instrument's own legal effect and the member states it names: ``regional_direct``,
  ``regional_requires_transposition`` (with the country's transposing acts that checked links record),
  ``regional_binding_on_addressees`` or ``non_binding``. One addressed to other states, or of a body the
  country had left or not yet joined, is ``comparative``;
* whatever is not reviewed (no reviewed membership of the body, a record that states no legal effect, a
  party list without the state that is not known to be complete, a date that cannot be compared) is
  ``unknown``, and stays in answers: unknown applicability is reported, never dropped (evaluation-design).

The scope is asked for, never assumed (RSV-03): the answer states the bodies it added, the membership
each rests on and what it left out. A label is never a decision of anyone's governing law (JUR-03).
"""

import calendar
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from api.reach import country
from schema.public import (
    Applicability,
    ApplicableScope,
    AuthorityEvidence,
    LinkedAct,
    RegionalView,
    StatedDateView,
    TreatyPartyView,
    TreatyView,
)

# Whether each relation binds in the jurisdiction asked about. A directive binds its member states as to
# its result: it is bound, and its transposition is a separate fact.
BINDS = {
    "national": "bound",
    "regional_direct": "bound",
    "regional_requires_transposition": "bound",
    "regional_binding_on_addressees": "bound",
    "treaty_in_force_for_state": "bound",
    "treaty_not_in_force_for_state": "not_bound",
    "non_binding": "not_bound",
    "comparative": "not_bound",
    "unknown": "unknown",
}
REGIONAL = {
    "directly_applicable": "regional_direct",
    "requires_transposition": "regional_requires_transposition",
    "binding_on_addressees": "regional_binding_on_addressees",
    "non_binding": "non_binding",
}
# An applicable-law scope examines at most this many records of other jurisdictions (BUD-01).
MAX_CANDIDATES = 10_000

Acts = Callable[[str, str], tuple[LinkedAct, ...]]  # (record, "domesticated_by" | "transposed_by")


def on_or_before(stated, day: date) -> bool | None:
    """Whether a stated date is certainly on or before ``day`` (True), certainly after it (False), or cannot
    be told (None: no normalised value, or ``day`` falls within its month or year)."""
    if stated is None or stated.value is None:
        return None
    first = last = stated.value
    if stated.precision == "year":
        first, last = date(first.year, 1, 1), date(first.year, 12, 31)
    elif stated.precision == "month":
        first, last = first.replace(day=1), first.replace(day=calendar.monthrange(first.year, first.month)[1])
    if last <= day:
        return True
    return False if first > day else None


def _shown(stated) -> str:
    return stated.value.isoformat() if stated.value else stated.printed


def _date(stated) -> StatedDateView | None:
    return StatedDateView.model_validate(stated.model_dump(mode="json")) if stated else None


def _evidence(stated) -> AuthorityEvidence | None:
    return (
        AuthorityEvidence.model_validate(stated.model_dump(mode="json", exclude_none=True))
        if stated
        else None
    )


def regional_view(source) -> RegionalView | None:
    """A regional instrument's legal effect as its record states it; None for any other record."""
    return RegionalView.model_validate(source.regional.model_dump(mode="json")) if source.regional else None


def _party(party, domesticating: tuple[LinkedAct, ...]) -> TreatyPartyView:
    return TreatyPartyView(
        **party.model_dump(mode="json", exclude_none=True),
        domesticated_by=tuple(
            act for act in domesticating if party.place and country(act.jurisdiction_id) == party.place
        ),
    )


def treaty_view(source, acts: Acts, *, parties: bool = True) -> TreatyView | None:
    """A treaty's status as its record states it, each party with the acts of that state that checked links
    record as domesticating it, and the date the party list is complete as of, with its evidence, when
    recorded; without ``parties`` (an evidence unit), its parties and their completeness are left out."""
    treaty = source.treaty
    if treaty is None:
        return None
    domesticating = acts(source.id, "domesticated_by") if parties and treaty.parties else ()
    return TreatyView(
        adopted_by=treaty.adopted_by,
        depositary=treaty.depositary,
        entered_into_force=_date(treaty.entered_into_force),
        authentic_languages=tuple(treaty.authentic_languages),
        parties=tuple(_party(p, domesticating) for p in treaty.parties) if parties else None,
        parties_complete_as_of=treaty.parties_complete_as_of if parties else None,
        parties_complete_evidence=_evidence(treaty.parties_complete_evidence) if parties else None,
    )


@dataclass
class Labeller:
    """Records' relation to one jurisdiction's law on one date, from whether it was a member of each body on
    that date (``bodies``: api.catalogue.bodies_on, the one reader of membership dates, by body: the
    memberships relied on, each saying ``held``, ``not_held`` or ``unknown``) and the checked links ``acts``
    reads."""

    jurisdiction: str
    as_of: date
    bodies: dict  # body → tuple[schema.public.MembershipBasis, ...]
    acts: Acts
    _labels: dict[str, Applicability] = field(default_factory=dict)

    def label(self, source) -> Applicability:
        if source.id not in self._labels:
            self._labels[source.id] = self._label(source)
        return self._labels[source.id]

    def _answer(self, relation: str, reasons: list[str], *, memberships=(), **basis) -> Applicability:
        return Applicability(
            jurisdiction_id=self.jurisdiction,
            as_of=self.as_of,
            relation=relation,
            binds=BINDS[relation],
            memberships=tuple(memberships),
            reasons=tuple(reasons),
            **basis,
        )

    def _label(self, source) -> Applicability:
        here, day = self.jurisdiction, self.as_of
        if source.treaty is not None:
            return self._treaty(source)
        if source.jurisdiction_id == here:
            return self._answer("national", [f"The law of {here}."])
        place = country(source.jurisdiction_id)
        if place is not None:
            if place == country(here):
                return self._answer(
                    "unknown",
                    [f"{source.jurisdiction_id} and {here} are places in one country: how their law relates is "
                     "not assessed here."],
                )  # fmt: skip
            return self._answer("comparative", [f"The law of {source.jurisdiction_id}, not of {here}."])
        body = source.jurisdiction_id
        basis = self.bodies.get(body, ())
        if not basis:
            return self._answer(
                "unknown",
                [f"No membership of {here} in {body} is reviewed yet: whether {body}'s instruments reach {here} "
                 "is unknown, not none."],
            )  # fmt: skip
        if basis[0].held == "unknown":
            return self._answer(
                "unknown", [f"Whether {here} was a member of {body} on {day} cannot be told from its reviewed "
                            "membership's dates."], memberships=basis,
            )  # fmt: skip
        if basis[0].held == "not_held":
            return self._answer(
                "comparative", [f"{here} was not a member of {body} on {day}, by its reviewed membership."],
                memberships=basis,
            )  # fmt: skip
        member = f"{here} was a member of {body} on {day}"
        regional = regional_view(source)
        if regional is None:
            return self._answer(
                "unknown", [f"{member}, but the record states no legal effect."], memberships=basis
            )
        named = regional.member_states
        if named and here not in named and country(here) not in named:
            return self._answer(
                "comparative", [f"{member}, but the instrument binds {', '.join(named)} only."],
                memberships=basis, regional=regional,
            )  # fmt: skip
        relation = REGIONAL[regional.legal_effect]
        reasons, transposed = [], ()
        if relation == "regional_direct":
            reasons.append(f"{member}; the instrument applies directly in its members.")
        elif relation == "regional_requires_transposition":
            reasons.append(f"{member}; the instrument binds {here} as to its result, through national law.")
            if regional.transposition_deadline:
                reasons.append(f"Transposition was due by {regional.transposition_deadline}.")
            transposed = tuple(
                act
                for act in self.acts(source.id, "transposed_by")
                if country(act.jurisdiction_id) == country(here)
            )
            if not transposed:
                reasons.append(f"No act of {here} transposing it is recorded.")
        elif relation == "regional_binding_on_addressees":
            addressed = f"{here} among them" if named else "and it names none, so every member"
            reasons.append(
                f"{member}; the instrument binds the member states it is addressed to, {addressed}."
            )
        else:
            reasons.append(f"{member}, but the instrument is not binding.")
        return self._answer(relation, reasons, memberships=basis, regional=regional, transposed_by=transposed)

    def _treaty(self, source) -> Applicability:
        """A treaty binds a state by that state's own consent, never by membership of a body (JUR-04)."""
        here, day, treaty = self.jurisdiction, self.as_of, source.treaty
        state, entered = country(here), _date(treaty.entered_into_force)
        found = next((p for p in treaty.parties if p.place == state), None)
        if found is None:
            # A state missing from the list is not a party only where the list is complete on the date.
            listed = (
                f"The treaty's reviewed party list records no ratification or accession by {state}"
                if treaty.parties
                else "The treaty's record lists no parties"
            )
            complete = treaty.parties_complete_as_of
            if complete is not None and complete >= day:
                return self._answer(
                    "treaty_not_in_force_for_state",
                    [f"{listed}, and the list is complete as of {complete} by the depositary's status list."],
                    entered_into_force=entered,
                )  # fmt: skip
            only = f" (it is complete as of {complete} only)" if complete else ""
            return self._answer(
                "unknown",
                [f"{listed}, and the party list is not known to be complete on {day}{only}: whether it binds "
                 f"{state} is unknown."],
                entered_into_force=entered,
            )  # fmt: skip
        party = _party(found, self.acts(source.id, "domesticated_by"))

        def answer(relation, *reasons):
            return self._answer(relation, list(reasons), party=party, entered_into_force=entered)

        if found.denounced is not None:
            denounced = on_or_before(found.denounced, day)
            if denounced:
                return answer(
                    "treaty_not_in_force_for_state",
                    f"{state} denounced it, effective {_shown(found.denounced)}.",
                )
            if denounced is None:
                return answer("unknown", f"The date of {state}'s denunciation cannot be compared with {day}.")
        consents = [
            (word, d) for word, d in (("ratified", found.ratified), ("acceded to", found.acceded)) if d
        ]
        if not consents:
            signed = f"{state} signed it on {_shown(found.signed)}" if found.signed else f"{state} is listed"
            return answer(
                "treaty_not_in_force_for_state", f"{signed}, with no ratification or accession recorded: "
                "signature alone does not bind."
            )  # fmt: skip
        known = [(word, d, on_or_before(d, day)) for word, d in consents]
        given = next(((word, d) for word, d, before in known if before), None)
        if given is None:
            if all(before is False for _, _, before in known):
                word, d = consents[0]
                return answer(
                    "treaty_not_in_force_for_state", f"{state} {word} it on {_shown(d)}, after {day}."
                )
            return answer(
                "unknown", f"The date of {state}'s ratification or accession cannot be compared with {day}."
            )
        if entered is None:
            return answer("unknown", f"{state} {given[0]} it on {_shown(given[1])}; the treaty's entry into force is "
                          "not recorded.")  # fmt: skip
        in_force = on_or_before(treaty.entered_into_force, day)
        if in_force is False:
            return answer(
                "treaty_not_in_force_for_state",
                f"The treaty entered into force on {_shown(entered)}, after {day}.",
            )
        if in_force is None:
            return answer(
                "unknown", f"The date of the treaty's entry into force cannot be compared with {day}."
            )
        domesticated = (
            f"Domesticated by {', '.join(a.citation for a in party.domesticated_by)}."
            if party.domesticated_by
            else f"No act of {state} domesticating it is recorded."
        )
        return answer(
            "treaty_in_force_for_state",
            f"{state} {given[0]} it on {_shown(given[1])}, and it was in force on {day}.",
            domesticated,
        )


@dataclass(frozen=True)
class Scope:
    """An applicable-law scope (``scope=applicable``): what it states, the records of other jurisdictions it
    admits, their jurisdictions (for the plan's admission) and the labeller of every record it answers."""

    statement: ApplicableScope
    admitted: tuple[str, ...]
    jurisdictions: frozenset[str]
    labeller: Labeller

    def label(self, source) -> Applicability:
        return self.labeller.label(source)


def scope(labeller: Labeller, candidates, *, date_basis: str, named=None) -> Scope:
    """Widen a search of the labeller's jurisdiction to the records of other jurisdictions that bind there or
    whose relation is unknown. ``candidates``: the public records of the bodies it belonged to on the date
    (reviewed memberships) and the public treaties whose party list names it. ``named``: the record the
    request names (source_id), answered whatever its relation."""
    here, day = labeller.jurisdiction, labeller.as_of
    admitted, places, left_out, unknown = set(), set(), Counter(), 0
    for source in [*candidates, *([named] if named is not None else [])]:
        if source.jurisdiction_id == here or source.id in admitted:
            continue
        label = labeller.label(source)
        if label.binds == "not_bound" and (named is None or source.id != named.id):
            left_out[label.relation] += 1
            continue
        admitted.add(source.id)
        places.add(source.jurisdiction_id)
        unknown += label.binds == "unknown"
    if labeller.bodies:
        notices = [
            f"Regional law was added through reviewed memberships only: a body whose membership of {here} is "
            f"not reviewed yet was not added, which never means {here} is not a member."
        ]
        uncertain = [body for body, basis in labeller.bodies.items() if basis[0].held == "unknown"]
        if uncertain:
            notices.append(
                f"Whether {here} was a member of {', '.join(uncertain)} on {day} cannot be told from the reviewed "
                "dates, so their law was not added."
            )
    else:
        notices = [
            f"No membership of {here} is reviewed yet, so no regional body's law was added; that never means "
            f"{here} belongs to none."
        ]
    notices.append(
        f"Treaties were added where the treaty's reviewed party list records {country(here)}'s ratification or "
        "accession: membership of a body never applies its treaties."
    )
    if date_basis == "today":
        notices.append(
            f"No as_of was given: memberships and treaties are assessed at today's date, {day}. Texts are not "
            "limited by date."
        )
    if left_out:
        notices.append(
            f"{sum(left_out.values())} records of these bodies, or treaties naming {here}, were left out: they do "
            f"not bind {here} on {day} (left_out gives them by relation)."
        )
    if unknown:
        notices.append(f"{unknown} records whose relation to {here} is unknown were kept, labelled unknown.")
    if named is not None and named.jurisdiction_id != here:
        notices.append(f"{named.id}, named by source_id, is answered whatever its relation to {here}.")
    return Scope(
        statement=ApplicableScope(
            jurisdiction_id=here,
            as_of=day,
            date_basis=date_basis,
            membership="reviewed" if labeller.bodies else "unknown",
            bodies=tuple(basis for body in sorted(labeller.bodies) for basis in labeller.bodies[body]),
            admitted=len(admitted),
            left_out=dict(sorted(left_out.items())),
            notices=tuple(notices),
        ),
        admitted=tuple(sorted(admitted)),
        jurisdictions=frozenset(places),
        labeller=labeller,
    )
