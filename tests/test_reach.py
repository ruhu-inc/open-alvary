"""A plan's reach: countries include what is under them; regional and continental law is in every plan."""

import pytest

from api.reach import EVERYWHERE, OutsidePlan, OutsidePlanLimited, Reach, country, starter_only


def test_a_country_includes_what_is_under_it_and_regional_law_belongs_to_none():
    assert country("ng") == "ng" and country("ng-la") == "ng" and country("ZA-WC") == "za"
    assert country("aa-eac") is None and country("aa-au") is None and country("aa") is None


def test_a_plan_covers_its_countries_and_all_regional_law():
    plan = Reach.of({"jurisdictions": ["RW", "ng"]})
    assert plan.covers("rw") and plan.covers("ng-la") and plan.covers("aa-ecowas")
    assert not plan.covers("ke")
    assert plan.within(["rw", "ke", "ng-la", "aa-eac"]) == frozenset({"rw", "ng-la", "aa-eac"})
    assert EVERYWHERE.within(["ke"]) is None and Reach.of({"all": True}).covers("ke")


def test_only_requests_outside_the_plan_draw_on_the_starter_allowance():
    drawn = []
    plan = Reach.of({"jurisdictions": ["rw"]}, lambda: drawn.append(1))
    plan.admit("rw", "aa-eac")
    assert drawn == []
    plan.admit("rw", "ke", "ug")
    assert drawn == [1]  # once per request, however many lie outside

    def used_up():
        raise OutsidePlanLimited(7)

    with pytest.raises(OutsidePlanLimited) as limited:
        Reach.of({"jurisdictions": ["rw"]}, used_up).admit("ke")
    assert limited.value.retry_after == 7 and "Starter rate" in str(limited.value)


def test_starter_reads_its_one_country_and_is_refused_any_other():
    scope = {"jurisdictions": ["ng"]}
    plan = Reach.of(scope, starter_only(scope))
    plan.admit("ng", "ng-la", "aa-ecowas")  # its country, what is under it, and regional law
    assert plan.within(["ng", "rw", "aa-au"]) == frozenset({"ng", "aa-au"})
    with pytest.raises(OutsidePlan) as refused:
        plan.admit("ng", "rw")
    assert "about another" in str(refused.value)


def test_starter_before_its_country_is_chosen_reads_regional_law_only_and_says_what_to_do():
    scope = {"jurisdictions": []}
    plan = Reach.of(scope, starter_only(scope))
    plan.admit("aa-eac")
    for ask in (lambda: plan.admit("ng"), lambda: plan.within(["ng", "aa-eac"])):
        with pytest.raises(OutsidePlan) as refused:
            ask()
        assert "none is chosen yet" in str(refused.value)
