"""The plans' seats and listings in accounts/tiers.json (docs: teams-and-seats §2). No price is shown until the
owner's pre-charge gates are cleared (SUS-02, SUS-06): the paid plans stay "Coming soon". The seat rules
themselves are tested against PostgreSQL in tests/test_postgres_seats.py."""

import json
from pathlib import Path

TIERS = json.loads((Path(__file__).resolve().parents[1] / "accounts" / "tiers.json").read_text())
BY_ID = {tier["id"]: tier for tier in TIERS}


def test_each_plan_names_its_seats():
    assert {tier["id"]: tier["allowance"]["members"] for tier in TIERS} == {
        "starter": 1,
        "developer": 1,
        "team": 5,
        "enterprise": 10,
        "public-interest": 5,
        "pilot": 5,
        "partner": 10,
        "sovereign": 10,
        "law-check": 1,
    }


def test_developer_keeps_its_id_and_is_shown_as_pro():
    assert BY_ID["developer"]["name"] == "Pro" and BY_ID["team"]["name"] == "Team"


def test_no_price_is_advertised_before_the_owner_clears_the_gates():
    assert BY_ID["developer"]["listing"]["price"] == "Coming soon"
    assert BY_ID["team"]["listing"]["price"] == "Coming soon"
    listings = json.dumps([tier.get("listing", {}) for tier in TIERS])
    assert "$" not in listings and "annual_price" not in listings


def test_starter_and_pro_are_one_country_and_team_five_and_the_rest_cover_every_country():
    # The owner's decisions of 2026-11-24 (Pro was 2) and 2026-11-25 (Starter read every country).
    # Public interest keeps every country for an operator's grant; it is not listed (owner, 2026-11-25).
    assert {
        tier["id"]: tier["allowance"]["jurisdictions"]
        for tier in TIERS
        if "jurisdictions" in tier["allowance"]
    } == {
        "starter": 1,
        "developer": 1,
        "team": 5,
    }
    # Starter reads a default jurisdiction until an owner changes it (owner, 2026-11-25); no other plan has one.
    assert {
        tier["id"]: tier["allowance"]["default_jurisdiction"]
        for tier in TIERS
        if "default_jurisdiction" in tier["allowance"]
    } == {"starter": "ng"}


def test_public_interest_is_left_out_of_the_listed_plans_for_now():
    # Owner, 2026-11-25: not offered or listed; an operator can still grant it, as with the agreed plans.
    assert BY_ID["public-interest"]["published"] is False
    assert "action" not in BY_ID["public-interest"]["listing"]
