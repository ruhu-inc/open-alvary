"""Regional and treaty law (JUR-03, JUR-04): a regional instrument's legal effect and a treaty's status as
their reviewed records state them, as the catalogue serves them on a record, its anchors and its evidence
units. Nothing is inferred: a treaty's party list without a state never says the state is bound, and
membership of a body never applies a treaty to its members (JUR-04)."""

from collections.abc import Callable

from api.reach import country
from schema.public import LinkedAct, RegionalView, StatedDateView, TreatyPartyView, TreatyView

Acts = Callable[[str, str], tuple[LinkedAct, ...]]  # (record, "domesticated_by" | "transposed_by")


def _date(stated) -> StatedDateView | None:
    return StatedDateView.model_validate(stated.model_dump(mode="json")) if stated else None


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
    record as domesticating it; without ``parties`` (an evidence unit), its parties are left out."""
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
    )
