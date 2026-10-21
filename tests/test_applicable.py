"""Applicable law (JUR-03, JUR-04): each label of a record's relation to a country's law on a date, what it
rests on, and the scope that widens a search to it only when asked (RSV-03). Reviewed facts only: a
membership, a legal effect or a ratification that is not reviewed is unknown, never assumed. The records,
memberships and dates here are synthetic fixtures, not research."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from api import applicable
from api.catalogue import bodies_on
from api.reach import country
from api.text_index import Filters
from retrieval.engine import Engine
from schema.models import PartyStatus, RegionalEffect, Source, StatedDate, StatedEvidence, TreatyStatus
from schema.public import LinkedAct, MembershipBasis, MembershipView

DAY = date(2026, 1, 1)


def source(id_, jurisdiction, *, regional=None, treaty=None):
    return Source(
        id=id_, jurisdiction_id=jurisdiction, authority_id=f"{jurisdiction}-body", title=id_, document_type="other",
        canonical_url="https://www.example.org/fixture.pdf", citation=id_, regional=regional, treaty=treaty,
    )  # fmt: skip


BASIS = {"kind": "entry_into_force", "instrument": "Treaty", "pinpoint": "art. 1",
         "evidence_url": "https://www.example.org/treaty"}  # fmt: skip


def membership(member, body, start, end=None, n=1):
    return MembershipView(
        id=f"{member}:{body}:{n}", member_id=member, member_name=member, body_id=body, body_name=body,
        valid_from=start, valid_to=end, basis=BASIS, evidence_url="https://www.example.org/members",
        note="Fixture.", reviewed_by="Test Reviewer", reviewed_at=datetime(2026, 10, 21, tzinfo=UTC),
    )  # fmt: skip


MEMBERSHIPS = [
    membership("sn", "aa-au", date(2001, 5, 26)),
    membership("sn", "aa-ecowas", date(1975, 5, 28)),
    membership("sn", "aa-ohada", date(1995, 9, 18)),
    membership("sn", "aa-uemoa", date(1994, 1, 10), date(2000, 1, 1)),
    membership("ml", "aa-ecowas", date(1975, 5, 28), date(2025, 1, 28)),
    membership("ng", "aa-ecowas", date(1975, 5, 28)),
]
TRANSPOSING = LinkedAct(
    source_id="sn-transposing-act", jurisdiction_id="sn", title="Loi", citation="Loi n° 1"
)
ACTS = {
    ("ecowas-directive", "transposed_by"): (
        TRANSPOSING,
        LinkedAct(source_id="ml-transposing-act", jurisdiction_id="ml", title="Loi", citation="Loi malienne"),
    ),
    ("au-charter", "domesticated_by"): (
        LinkedAct(source_id="ke-domesticating-act", jurisdiction_id="ke", title="Act", citation="Act No. 3"),
    ),
}


def acts(source_id, kind):
    return ACTS.get((source_id, kind), ())


def labeller(member="sn", day=DAY, held=MEMBERSHIPS):
    """As the catalogue makes it: from the memberships of the country (for a place, the one it is within)."""
    return applicable.Labeller(member, day, bodies_on(held, country(member), day), acts)


def regional(effect="directly_applicable", **fields):
    return RegionalEffect(organ="Council of Ministers", legal_effect=effect, **fields)


def on(value, precision="day"):
    return StatedDate(value=value, precision=precision)


IN_FORCE = StatedDate(value=date(2005, 1, 1))


STATUS_LIST = StatedEvidence(source="external", url="https://www.example.org/status-list.pdf")


def charter(*parties, entered=IN_FORCE, complete=None):
    """A treaty's status; ``complete``: the date its party list is complete as of, by a status list."""
    return TreatyStatus(
        adopted_by="Assembly of the Union", entered_into_force=entered, parties=list(parties),
        parties_complete_as_of=complete, parties_complete_evidence=STATUS_LIST if complete else None,
    )  # fmt: skip


def label(record, **kwargs):
    return labeller(**kwargs).label(record)


def test_a_countrys_own_law_is_national():
    found = label(source("sn-code", "sn"))
    assert (found.relation, found.binds, found.jurisdiction_id, found.as_of) == (
        "national",
        "bound",
        "sn",
        DAY,
    )


def test_a_directly_applicable_instrument_of_a_body_the_country_belonged_to_is_regional_direct():
    found = label(source("ohada-uniform-act", "aa-ohada", regional=regional()))
    assert (found.relation, found.binds) == ("regional_direct", "bound")
    # Its basis: the reviewed membership that held on the date, and the instrument's profile.
    assert [(m.id, m.held) for m in found.memberships] == [("sn:aa-ohada:1", "held")]
    assert (
        found.regional.organ == "Council of Ministers"
        and found.regional.legal_effect == "directly_applicable"
    )


def test_a_directive_requires_transposition_with_its_deadline_and_the_countrys_transposing_act():
    directive = source(
        "ecowas-directive", "aa-ecowas",
        regional=regional("requires_transposition", transposition_deadline=date(2010, 12, 31)),
    )  # fmt: skip
    found = label(directive)
    assert (found.relation, found.binds) == ("regional_requires_transposition", "bound")
    assert found.regional.transposition_deadline == date(2010, 12, 31)
    assert found.transposed_by == (TRANSPOSING,)  # Senegal's act, never Mali's
    assert "Transposition was due by 2010-12-31." in found.reasons
    # Without a recorded transposing act, it says so.
    unlinked = label(source("other-directive", "aa-ecowas", regional=regional("requires_transposition")))
    assert unlinked.transposed_by == () and "No act of sn transposing it is recorded." in unlinked.reasons


def test_an_instrument_binding_on_its_addressees_binds_the_country_when_it_is_one():
    named = label(
        source("decision-sn", "aa-ecowas", regional=regional("binding_on_addressees", member_states=["sn"]))
    )
    assert (named.relation, named.binds) == ("regional_binding_on_addressees", "bound")
    every = label(source("decision-all", "aa-ecowas", regional=regional("binding_on_addressees")))
    assert every.relation == "regional_binding_on_addressees" and "every member" in every.reasons[0]


def test_a_non_binding_instrument_is_non_binding():
    found = label(source("recommendation", "aa-ecowas", regional=regional("non_binding")))
    assert (found.relation, found.binds) == ("non_binding", "not_bound")


def test_another_jurisdictions_law_is_comparative():
    assert label(source("gh-act", "gh")).relation == "comparative"
    # Addressed to other member states only.
    others = label(
        source("decision-ml", "aa-ecowas", regional=regional("binding_on_addressees", member_states=["ml"]))
    )
    assert (others.relation, others.binds) == ("comparative", "not_bound") and "ml only" in others.reasons[0]
    # A body the country had left by the date, by its reviewed membership.
    left = label(source("uemoa-regulation", "aa-uemoa", regional=regional()))
    assert left.relation == "comparative" and [(m.id, m.held) for m in left.memberships] == [
        ("sn:aa-uemoa:1", "not_held")
    ]
    # Mali left ECOWAS on 28 January 2025: bound on its last day, comparative after it.
    act = source("ecowas-act", "aa-ecowas", regional=regional())
    assert label(act, member="ml", day=date(2025, 1, 28)).relation == "regional_direct"
    assert label(act, member="ml", day=date(2025, 1, 29)).relation == "comparative"


def test_a_treaty_is_in_force_for_a_state_that_ratified_it_with_its_domesticating_act():
    treaty = source(
        "au-charter", "aa-au",
        treaty=charter(PartyStatus(party="Kenya", place="ke", signed=on(date(2003, 1, 1)), ratified=on(date(2005, 3, 1)))),
    )  # fmt: skip
    found = label(treaty, member="ke", held=[])
    assert (found.relation, found.binds) == ("treaty_in_force_for_state", "bound")
    assert found.party.place == "ke" and found.party.ratified.value == date(2005, 3, 1)
    assert [a.source_id for a in found.party.domesticated_by] == ["ke-domesticating-act"]
    assert found.entered_into_force.value == date(2005, 1, 1) and found.memberships == ()
    # An accession binds as a ratification does.
    acceded = source(
        "au-protocol",
        "aa-au",
        treaty=charter(PartyStatus(party="Kenya", place="ke", acceded=on(date(2010, 6, 1)))),
    )
    assert label(acceded, member="ke", held=[]).relation == "treaty_in_force_for_state"


def test_a_treaty_is_not_in_force_for_a_state_without_its_own_ratification_on_the_date():
    def relation(*parties, entered=IN_FORCE, member="sn", complete=None):
        treaty = charter(*parties, entered=entered, complete=complete)
        return label(source("au-charter", "aa-au", treaty=treaty), member=member)

    kenya = PartyStatus(party="Kenya", place="ke", ratified=on(date(2005, 3, 1)))
    # Senegal missing from a party list complete on the date: not a party. Membership of the AU never
    # applies the treaty (JUR-04).
    found = relation(kenya, complete=date(2026, 6, 1))
    assert (found.relation, found.binds) == ("treaty_not_in_force_for_state", "not_bound")
    assert (
        "no ratification or accession by sn" in found.reasons[0]
        and "complete as of 2026-06-01" in found.reasons[0]
    )
    assert relation(kenya, complete=DAY).relation == "treaty_not_in_force_for_state"
    # Missing from a list not known to be complete, or complete only before the date: unknown, never "not a
    # party", as the list may leave the state out.
    for complete in (None, date(2025, 12, 31)):
        absent = relation(kenya, complete=complete)
        assert (absent.relation, absent.binds, absent.party) == ("unknown", "unknown", None)
        assert "the party list is not known to be complete on 2026-01-01" in absent.reasons[0]
    assert "complete as of 2025-12-31 only" in relation(kenya, complete=date(2025, 12, 31)).reasons[0]
    # Listed, the state's own entry decides, complete or not.
    signed = relation(PartyStatus(party="Senegal", place="sn", signed=on(date(2004, 1, 1))))
    assert signed.relation == "treaty_not_in_force_for_state" and "signature alone" in signed.reasons[0]
    later = relation(PartyStatus(party="Senegal", place="sn", ratified=on(date(2026, 6, 1))))
    assert later.relation == "treaty_not_in_force_for_state" and "after 2026-01-01" in later.reasons[0]
    denounced = relation(
        PartyStatus(
            party="Senegal", place="sn", ratified=on(date(2006, 1, 1)), denounced=on(date(2020, 1, 1))
        )
    )
    assert denounced.relation == "treaty_not_in_force_for_state" and "denounced" in denounced.reasons[0]
    early = relation(
        PartyStatus(party="Senegal", place="sn", ratified=on(date(2006, 1, 1))), entered=on(date(2027, 1, 1))
    )
    assert (
        early.relation == "treaty_not_in_force_for_state"
        and "entered into force on 2027-01-01" in early.reasons[0]
    )


def test_what_is_not_reviewed_is_unknown():
    # No reviewed membership of the body: unknown, never "not a member".
    cemac = label(source("cemac-regulation", "aa-cemac", regional=regional()))
    assert (cemac.relation, cemac.binds) == (
        "unknown",
        "unknown",
    ) and "No membership of sn in aa-cemac" in cemac.reasons[0]
    assert label(source("ohada-act", "aa-ohada", regional=regional()), member="cm").relation == "unknown"
    # A body's record that states no legal effect.
    assert label(source("ecowas-court-judgment", "aa-ecowas")).relation == "unknown"
    # A treaty whose record lists no parties, or no entry into force, or a date that cannot be compared.
    assert label(source("treaty", "aa-au", treaty=charter())).relation == "unknown"
    # A list complete on the date with no parties at all: none is a party.
    none = label(source("treaty", "aa-au", treaty=charter(complete=date(2026, 6, 1))))
    assert none.relation == "treaty_not_in_force_for_state" and "lists no parties" in none.reasons[0]
    ratified = PartyStatus(party="Senegal", place="sn", ratified=on(date(2006, 1, 1)))
    assert label(source("treaty", "aa-au", treaty=charter(ratified, entered=None))).relation == "unknown"
    vague = PartyStatus(party="Senegal", place="sn", ratified=on(date(2026, 1, 1), "year"))
    assert label(source("treaty", "aa-au", treaty=charter(vague))).relation == "unknown"
    printed = PartyStatus(party="Senegal", place="sn", ratified=StatedDate(printed="le 12 mai 2006"))
    assert label(source("treaty", "aa-au", treaty=charter(printed))).relation == "unknown"
    # A membership whose dates cannot place the date asked about.
    uncertain = applicable.Labeller(
        "sn", DAY, {"aa-ecowas": (MembershipBasis(id="sn:aa-ecowas:1", body_id="aa-ecowas", held="unknown"),)},
        acts,
    )  # fmt: skip
    assert uncertain.label(source("ecowas-act", "aa-ecowas", regional=regional())).relation == "unknown"
    # A place's own law, asked of its country: not assessed here.
    assert label(source("ng-la-law", "ng-la"), member="ng").relation == "unknown"


def test_an_uncertain_membership_date_gives_the_unknown_label():
    # A membership that ended in December 2000, its last day unknown (schema.dates): inside that month the
    # body's instruments are unknown, never assumed either way; outside it, definite.
    left = [membership("mr", "aa-ecowas", "1975-05-28", "2000-12")]
    act = source("ecowas-act", "aa-ecowas", regional=regional())
    inside = label(act, member="mr", day=date(2000, 12, 15), held=left)
    assert (inside.relation, inside.binds) == ("unknown", "unknown")
    assert [(m.id, m.held) for m in inside.memberships] == [("mr:aa-ecowas:1", "unknown")]
    assert label(act, member="mr", day=date(2000, 11, 30), held=left).relation == "regional_direct"
    assert label(act, member="mr", day=date(2001, 1, 1), held=left).relation == "comparative"
    # A start known only to a window: the scope keeps the body's record, labelled unknown, and says why.
    joined = [membership("ss", "aa-eac", "2016-08-15/2016-09-05")]
    eac = source("eac-act", "aa-eac", regional=regional())
    scope = applicable.scope(labeller("ss", date(2016, 8, 20), joined), [eac], date_basis="as_of")
    assert scope.label(eac).relation == "unknown" and scope.admitted == ("eac-act",)
    assert [(b.body_id, b.held) for b in scope.statement.bodies] == [("aa-eac", "unknown")]
    assert any("cannot be told from the reviewed dates" in n for n in scope.statement.notices)
    assert labeller("ss", date(2016, 9, 6), joined).label(eac).relation == "regional_direct"


def test_a_place_within_a_country_has_its_countrys_law_and_reaches_regional_and_treaty_law_through_it():
    def lagos(record):
        return label(record, member="ng-la")

    # National and federal law applies throughout the country's states: national, and the basis says why.
    federal = lagos(source("ng-federal-act", "ng"))
    assert (federal.relation, federal.binds, federal.jurisdiction_id) == ("national", "bound", "ng-la")
    assert federal.reasons == ("ng-la is within ng: the law of ng applies throughout it.",)
    assert lagos(source("ng-la-law", "ng-la")).relation == "national"
    other = lagos(source("ng-ab-law", "ng-ab"))
    assert (other.relation, other.binds) == ("comparative", "not_bound")
    assert "another place within ng" in other.reasons[0]
    assert lagos(source("gh-act", "gh")).relation == "comparative"
    # A regional body's instrument reaches Lagos State through Nigeria's reviewed membership.
    directive = lagos(source("ecowas-directive", "aa-ecowas", regional=regional("requires_transposition")))
    assert (directive.relation, directive.jurisdiction_id) == ("regional_requires_transposition", "ng-la")
    assert [(m.id, m.held) for m in directive.memberships] == [("ng:aa-ecowas:1", "held")]
    assert directive.reasons[:2] == (
        "ng-la is within ng: regional bodies and treaties reach it through ng.",
        "ng was a member of aa-ecowas on 2026-01-01; the instrument binds ng as to its result, through national "
        "law.",
    )
    addressed = regional("binding_on_addressees", member_states=["ng"])
    assert (
        lagos(source("decision-ng", "aa-ecowas", regional=addressed)).relation
        == "regional_binding_on_addressees"
    )
    others = regional("binding_on_addressees", member_states=["sn"])
    assert lagos(source("decision-sn", "aa-ecowas", regional=others)).relation == "comparative"
    unreviewed = lagos(source("ohada-act", "aa-ohada", regional=regional()))
    assert unreviewed.relation == "unknown" and "No membership of ng in aa-ohada" in unreviewed.reasons[1]
    # A treaty binds it by Nigeria's own ratification.
    ratified = PartyStatus(party="Nigeria", place="ng", ratified=on(date(2005, 3, 1)))
    treaty = lagos(source("au-charter", "aa-au", treaty=charter(ratified)))
    assert (treaty.relation, treaty.party.place) == ("treaty_in_force_for_state", "ng")
    assert treaty.reasons[0] == "ng-la is within ng: regional bodies and treaties reach it through ng."
    # A country's own labels are as before: nothing says it is within anything.
    assert not any(
        "within" in r for r in label(source("ecowas-act", "aa-ecowas", regional=regional())).reasons
    )


def test_membership_on_a_date_is_read_by_one_function():
    found = bodies_on(MEMBERSHIPS, "sn", DAY)
    assert {body: basis[0].held for body, basis in found.items()} == {
        "aa-au": "held", "aa-ecowas": "held", "aa-ohada": "held", "aa-uemoa": "not_held",
    }  # fmt: skip
    # Of two periods, the one that held is the basis; before the first, neither held.
    periods = [membership("ml", "aa-ecowas", date(1975, 5, 28), date(2025, 1, 28)),
               membership("ml", "aa-ecowas", date(2030, 1, 1), n=2)]  # fmt: skip
    assert [b.id for b in bodies_on(periods, "ml", date(2031, 1, 1))["aa-ecowas"]] == ["ml:aa-ecowas:2"]
    assert [b.held for b in bodies_on(periods, "ml", date(2026, 1, 1))["aa-ecowas"]] == [
        "not_held",
        "not_held",
    ]
    # A suspension from participation never ends membership: only membership rows count.
    suspended = SimpleNamespace(
        **membership("sn", "aa-ecowas", DAY).model_dump(), kind="suspended_from_participation"
    )
    assert bodies_on([suspended], "sn", DAY) == {}
    assert bodies_on(MEMBERSHIPS, "cm", DAY) == {}


def test_dates_are_compared_only_where_their_precision_allows():
    assert applicable.on_or_before(on(date(2025, 12, 31)), DAY) is True
    assert applicable.on_or_before(on(date(2026, 1, 2)), DAY) is False
    assert applicable.on_or_before(on(date(2026, 1, 1), "month"), DAY) is None
    assert applicable.on_or_before(on(date(2025, 3, 1), "year"), DAY) is True
    assert applicable.on_or_before(StatedDate(printed="mai 2006"), DAY) is None


def test_the_scope_admits_what_binds_or_is_unknown_and_states_what_it_left_out():
    candidates = [
        source("ohada-uniform-act", "aa-ohada", regional=regional()),
        source("ecowas-recommendation", "aa-ecowas", regional=regional("non_binding")),
        source("decision-ml", "aa-ecowas", regional=regional("binding_on_addressees", member_states=["ml"])),
        source("ecowas-court-judgment", "aa-ecowas"),
        source(
            "au-charter",
            "aa-au",
            treaty=charter(PartyStatus(party="Senegal", place="sn", signed=on(date(2004, 1, 1)))),
        ),
        source("sn-code", "sn"),
    ]
    scope = applicable.scope(labeller(), candidates, date_basis="as_of")
    assert scope.admitted == ("ecowas-court-judgment", "ohada-uniform-act")
    assert scope.jurisdictions == {"aa-ecowas", "aa-ohada"}
    statement = scope.statement
    assert statement.membership == "reviewed" and statement.admitted == 2
    assert statement.left_out == {"comparative": 1, "non_binding": 1, "treaty_not_in_force_for_state": 1}
    assert [(b.body_id, b.held) for b in statement.bodies] == [
        ("aa-au", "held"), ("aa-ecowas", "held"), ("aa-ohada", "held"), ("aa-uemoa", "not_held"),
    ]  # fmt: skip
    assert any("3 records" in n for n in statement.notices) and any(
        "labelled unknown" in n for n in statement.notices
    )
    # A record the request names is answered whatever its relation, and the answer says so.
    named = applicable.scope(labeller(), candidates, date_basis="today", named=candidates[4])
    assert "au-charter" in named.admitted and any("named by source_id" in n for n in named.statement.notices)
    assert named.statement.date_basis == "today" and any("today's date" in n for n in named.statement.notices)
    # A place within a country: the country's law is added whole, and its bodies and treaties assessed.
    lagos = applicable.scope(
        labeller("ng-la"),
        [source("ecowas-directive", "aa-ecowas", regional=regional("requires_transposition")),
         source("ecowas-recommendation", "aa-ecowas", regional=regional("non_binding"))],
        date_basis="as_of",
    )  # fmt: skip
    assert (lagos.within, lagos.statement.within, lagos.admitted) == ("ng", "ng", ("ecowas-directive",))
    assert lagos.jurisdictions == {"ng", "aa-ecowas"} and lagos.statement.left_out == {"non_binding": 1}
    assert [(b.body_id, b.held) for b in lagos.statement.bodies] == [("aa-ecowas", "held")]
    assert lagos.statement.notices[0].startswith("ng-la is within ng: the law of ng applies throughout it")
    widened = lagos.widen(Filters(jurisdictions=("ng-la",)))
    assert widened.jurisdictions == ("ng-la", "ng") and widened.admitted == ("ecowas-directive",)
    assert widened.admits("ng", "ng-federal-act") and not widened.admits("ng-ab", "ng-ab-law")
    assert scope.widen(Filters(jurisdictions=("sn",))).jurisdictions == ("sn",) and statement.within is None
    # No reviewed membership: no body's law is added, and that is never "none".
    unreviewed = applicable.scope(labeller("cm"), [], date_basis="as_of").statement
    assert (
        unreviewed.membership == "unknown"
        and unreviewed.bodies == ()
        and "never means cm" in unreviewed.notices[0]
    )


def test_the_scope_widens_the_search_filters_only_when_asked():
    plain = Filters(jurisdictions=("sn",))
    assert plain.where() == (
        "true AND u.jurisdiction_id = ANY(:f_jurisdictions)",
        {"f_jurisdictions": ["sn"]},
    )
    assert Engine.where(plain) == '(jurisdiction_id contains "sn")'
    widened = Filters(jurisdictions=("sn",), admitted=("ohada-uniform-act",))
    where, params = widened.where()
    assert "(u.jurisdiction_id = ANY(:f_jurisdictions) OR u.work_id = ANY(:f_admitted))" in where
    assert params["f_admitted"] == ["ohada-uniform-act"]
    assert Engine.where(widened) == '(jurisdiction_id contains "sn" or work_id contains "ohada-uniform-act")'
    assert widened.admits("aa-ohada", "ohada-uniform-act") and not widened.admits("aa-ohada", "other")
    assert not plain.admits("aa-ohada", "ohada-uniform-act") and Filters().admits("gh", "anything")
